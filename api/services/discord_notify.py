"""Discord 通知 (ファイル経由) — Phase 5.

既存の ``lineworks-x-ops`` Discord Bot が ``/opt/lineworks-x-ops/inbox/``
配下のファイルを監視しているため、CutFlow CAD は単にそこに 1 行ずつ
ペイロードを **追記** するだけで通知が飛ぶ。Bot 側の責務分離 + 失敗
してもアプリのパスはブロックしない設計。

設定:
    CUTFLOW_DISCORD_NOTIFY=true          # 通知を有効化 (デフォルト false)
    CUTFLOW_DISCORD_INBOX=/opt/lineworks-x-ops/inbox/cutflow.txt
                                         # 書き込み先ファイル (デフォルト)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_INBOX = "/opt/lineworks-x-ops/inbox/cutflow.txt"
# M4: append への concurrent 書き込みを直列化 (run_in_executor 並列 job 通知対策)
_LOCK = threading.Lock()


def _enabled() -> bool:
    raw = os.environ.get("CUTFLOW_DISCORD_NOTIFY", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _inbox_path() -> Path:
    return Path(os.environ.get("CUTFLOW_DISCORD_INBOX", _DEFAULT_INBOX))


def _emit(payload: dict) -> bool:
    """1 行 JSON を append する。失敗してもアプリは止めない."""

    if not _enabled():
        return False
    path = _inbox_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": _now_iso(), **payload}, ensure_ascii=False)
        # append-only; bot 側がローテーション
        # M4: 同時 append (job_queue の並列 worker) で行が混ざらないよう lock
        with _LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return True
    except OSError as exc:
        log.warning("discord notify write failed: %s", exc)
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def notify_session_created(session_id: str, file_count: int) -> bool:
    return _emit(
        {
            "event": "session_created",
            "session_id": session_id,
            "file_count": int(file_count),
            "text": f"[CutFlow] セッション作成 ({file_count} ファイル)",
        }
    )


def notify_job_finished(
    kind: str,
    job_id: str,
    status: str,
    error: str | None = None,
) -> bool:
    emoji = "OK" if status == "completed" else "NG"
    text = f"[CutFlow] {kind} job {status} ({job_id[:8]})"
    if error:
        text += f" — {error}"
    return _emit(
        {
            "event": "job_finished",
            "kind": kind,
            "job_id": job_id,
            "status": status,
            "error": error,
            "emoji": emoji,
            "text": text,
        }
    )


def notify_error(scope: str, message: str) -> bool:
    return _emit(
        {
            "event": "error",
            "scope": scope,
            "message": message,
            "text": f"[CutFlow] ERROR ({scope}): {message[:200]}",
        }
    )
