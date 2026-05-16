"""履歴管理 — セッション保存/呼び出し/メトリクス — Phase 5."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Request

from models import (
    FileMeta,
    MetricsSnapshot,
    SaveSessionRequest,
    SavedSessionList,
    SavedSessionLoadResponse,
    SavedSessionMeta,
)
from services import metrics as metrics_mod
from services.discord_notify import notify_session_created
from services.job_queue import get_queue
from services.session_archive import (
    ArchiveError,
    delete_saved,
    list_saved,
    load_session,
    save_session,
)
from storage import SessionExpired, SessionNotFound, get_store

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["sessions"])

# H6: ``/api/metrics`` をローカルホスト (またはオプトインのAPIキー) のみに制限.
_METRICS_API_KEY_ENV = "CUTFLOW_METRICS_API_KEY"
_METRICS_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


@router.post("/sessions/save", response_model=SavedSessionMeta, status_code=201)
async def save(body: SaveSessionRequest) -> SavedSessionMeta:
    """現在のセッションを ``{name}.tar.gz`` で保存."""

    store = get_store()
    try:
        sess = store.get(body.session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except SessionExpired as exc:
        raise HTTPException(status_code=410, detail="session expired") from exc

    session_dir = store.root / sess.session_id
    try:
        info = save_session(session_dir, body.name)
    except ArchiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return SavedSessionMeta(
        name=info.name,
        size_bytes=info.size_bytes,
        saved_at=info.saved_at,
        file_count=info.file_count,
    )


@router.get("/sessions/saved", response_model=SavedSessionList)
async def list_saved_sessions() -> SavedSessionList:
    """保存済みセッション一覧."""

    items = [
        SavedSessionMeta(
            name=info.name,
            size_bytes=info.size_bytes,
            saved_at=info.saved_at,
            file_count=info.file_count,
        )
        for info in list_saved()
    ]
    return SavedSessionList(saved=items)


@router.post("/sessions/load/{name}", response_model=SavedSessionLoadResponse)
async def load_saved(name: str) -> SavedSessionLoadResponse:
    """保存済みセッションを新セッションIDで復元.

    H3: Frontend ``loadSession`` は ``Session {session_id, files, expires_at}``
    形式を期待する。復元後 ``store.get(new_sid)`` で full SessionInfo を
    引き直してその shape で返却する。
    """

    store = get_store()
    try:
        new_sid, file_count = load_session(
            name,
            sessions_root=store.root,
            ttl_hours=int(store.ttl.total_seconds() // 3600) or 24,
        )
    except ArchiveError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    notify_session_created(new_sid, file_count)

    # H3: 復元したセッションを引いて files / expires_at を含めて返す。
    files: list[FileMeta] = []
    expires_at = None
    try:
        sess = store.get(new_sid)
        files = [
            FileMeta(
                file_id=f.file_id,
                name=f.name,
                size=f.size,
                status="ready" if f.status not in ("ready", "error") else f.status,
                error=f.error,
            )
            for f in sess.files
        ]
        expires_at = sess.expires_at
    except (SessionNotFound, SessionExpired) as exc:
        # 復元直後に store から消える可能性は通常ないが、フォールバックして
        # 最低限の情報を返却
        log.warning("load_saved: cannot read freshly-restored session %s: %s", new_sid, exc)

    # expires_at が読めなかった場合は store.ttl 分後を当てる (FE が string 期待のため)
    if expires_at is None:
        from datetime import datetime, timezone

        expires_at = datetime.now(timezone.utc) + store.ttl

    return SavedSessionLoadResponse(
        session_id=new_sid,
        name=name,
        file_count=file_count,
        files=files,
        expires_at=expires_at,
    )


@router.delete("/sessions/saved/{name}", status_code=204)
async def delete_saved_session(name: str) -> None:
    if not delete_saved(name):
        raise HTTPException(status_code=404, detail="saved session not found")
    return None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@router.get("/metrics", response_model=MetricsSnapshot)
async def get_metrics(request: Request) -> MetricsSnapshot:
    """H6: localhost / ``X-API-Key`` ヘッダ持ち以外は 403.

    Tailscale Funnel 経由公開を想定し、Funnel 越し HTTP リクエスト
    (社内のみアクセス可) でも誤って metrics が漏れない設計に倒す。
    開発・社内運用では ``CUTFLOW_METRICS_API_KEY`` を設定する。
    """

    client_host = request.client.host if request.client else ""
    api_key = os.environ.get(_METRICS_API_KEY_ENV, "").strip()
    provided_key = request.headers.get("x-api-key", "").strip()

    is_local = client_host in _METRICS_LOCAL_HOSTS
    is_authenticated = bool(api_key) and provided_key == api_key

    if not is_local and not is_authenticated:
        raise HTTPException(status_code=403, detail="forbidden")

    snap = metrics_mod.snapshot()
    job_stats = get_queue().stats()
    return MetricsSnapshot(
        uptime_sec=float(snap["uptime_sec"]),
        request_count=int(snap["request_count"]),
        error_count=int(snap["error_count"]),
        avg_response_ms=float(snap["avg_response_ms"]),
        jobs_total=int(job_stats["total"]),
        jobs_completed=int(job_stats["completed"]),
        jobs_failed=int(job_stats["failed"]),
        jobs_running=int(job_stats["running"]),
        counters=dict(snap["counters"]),
    )
