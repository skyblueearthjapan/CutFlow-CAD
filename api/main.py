"""CutFlow•CAD API — Phase 1.

Adds:
* ``POST /api/upload``                              — multi-file DXF upload
* ``GET  /api/session/{sid}``                       — session info
* ``DELETE /api/session/{sid}``                     — clear a session
* ``GET  /api/session/{sid}/file/{fid}``            — entities + delete candidates
* ``POST /api/session/{sid}/file/{fid}/delete``     — reserve entities for removal
* ``GET  /api/session/{sid}/file/{fid}/export``     — stream cleaned DXF

The legacy ``/`` and ``/api/health`` endpoints are kept for compose/uptime checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from routers import files as files_router
from routers import jobs as jobs_router
from routers import nest as nest_router
from routers import session as session_router
from routers import sessions as sessions_router
from routers import templates as templates_router
from services import metrics as metrics_mod
from services.discord_notify import notify_error
from services.job_queue import get_queue
from storage import get_store

API_VERSION = "0.2.0"
SERVICE_NAME = "cutflow-cad-api"

# CORS origins are configurable via the ``CUTFLOW_CORS_ORIGINS`` env var
# (comma-separated). Defaults cover the Vite dev server and the alt port the
# docker-compose stack uses. Phase 1 has no Cookie/Auth flow so credentials
# are disabled — this also unlocks the wildcard scheme if a deployer wants it.
_DEFAULT_ORIGINS = "http://localhost:5173,http://localhost:3000"
ALLOWED_ORIGINS: list[str] = [
    o.strip()
    for o in os.environ.get("CUTFLOW_CORS_ORIGINS", _DEFAULT_ORIGINS).split(",")
    if o.strip()
]

# Background session sweep interval.
_PURGE_INTERVAL_SEC = 60 * 15  # 15 minutes
# Job result retention (C7) — auto-purge job records older than this.
_JOB_TTL_HOURS = float(os.environ.get("CUTFLOW_JOB_TTL_HOURS", "168"))  # 7 days

log = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start/stop background tasks: session purge + job queue workers."""

    stop = asyncio.Event()

    async def _sweep() -> None:
        store = get_store()
        queue = get_queue()
        # Run an initial sweep on boot so a fresh process trims stale data.
        try:
            store.purge_expired()
        except Exception:  # noqa: BLE001
            log.exception("initial purge failed")
        # C7: 古いジョブ結果も初期パージ
        try:
            queue.purge_old(ttl_hours=_JOB_TTL_HOURS)
        except Exception:  # noqa: BLE001
            log.exception("initial job purge failed")
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=_PURGE_INTERVAL_SEC)
            except asyncio.TimeoutError:
                try:
                    store.purge_expired()
                except Exception:  # noqa: BLE001
                    log.exception("periodic purge failed")
                try:
                    queue.purge_old(ttl_hours=_JOB_TTL_HOURS)
                except Exception:  # noqa: BLE001
                    log.exception("periodic job purge failed")

    task = asyncio.create_task(_sweep(), name="session-purge")
    queue = get_queue()
    await queue.start()
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await queue.stop()


app = FastAPI(title="CutFlow•CAD API", version=API_VERSION, lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    # Phase 1 ships no cookies or Authorization headers, so credentials stays
    # off. Leaving it True forces FastAPI to disable the wildcard origin and
    # complicates Cloud Run / proxy deployments — keep it minimal.
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

app.include_router(session_router.router)
app.include_router(files_router.router)
app.include_router(nest_router.router)
app.include_router(jobs_router.router)
app.include_router(sessions_router.router)
app.include_router(templates_router.router)


def _scrub_error_message(raw: str, max_len: int = 200) -> str:
    """H8: Discord 通知に流す前に message を切り詰め + 機密ぽい断片を除去.

    - 改行をスペースに正規化 (1 行 JSON にする)
    - Windows / Unix 風絶対パスを ``<path>`` に置換
    - SQL 風キーワード (SELECT/INSERT/UPDATE/DELETE FROM ...) の後続を伏字化
    - 最終的に max_len で切り詰め
    """

    import re

    s = (raw or "").replace("\r", " ").replace("\n", " ")
    # 絶対パス (Windows: ``C:\...`` or `/abs/...`) を縮める
    s = re.sub(r"[A-Za-z]:[\\\\/][^\s'\"]+", "<path>", s)
    s = re.sub(r"(^|[\s])/[A-Za-z0-9_./-]{4,}", r"\1<path>", s)
    # SQL ぽいキーワード以降を伏字化
    s = re.sub(
        r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE)\b[^\.;]{0,120}",
        r"\1 <redacted>",
        s,
        flags=re.IGNORECASE,
    )
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s


@app.middleware("http")
async def _metrics_middleware(request: Request, call_next):
    """簡易メトリクス計測 — Phase 5."""

    t0 = time.monotonic()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception as exc:  # noqa: BLE001
        # 5xx を Discord にも飛ばす (no-op if disabled)
        # H8: message は truncate + scrub
        try:
            notify_error(scope=request.url.path, message=_scrub_error_message(str(exc)))
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        try:
            metrics_mod.record_request(elapsed_ms, status_code)
        except Exception:  # noqa: BLE001
            pass


@app.get("/")
def root() -> dict[str, str]:
    """Service root; points to interactive docs."""

    return {"message": "CutFlow CAD API", "docs": "/docs"}


@app.get("/api/health")
def health() -> dict[str, str]:
    """Liveness/readiness probe used by docker-compose and uptime checks."""

    return {"status": "ok", "version": API_VERSION, "service": SERVICE_NAME}
