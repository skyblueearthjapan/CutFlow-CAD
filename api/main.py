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
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import files as files_router
from routers import session as session_router
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

log = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start/stop the background session-purge task."""

    stop = asyncio.Event()

    async def _sweep() -> None:
        store = get_store()
        # Run an initial sweep on boot so a fresh process trims stale data.
        try:
            store.purge_expired()
        except Exception:  # noqa: BLE001
            log.exception("initial purge failed")
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=_PURGE_INTERVAL_SEC)
            except asyncio.TimeoutError:
                try:
                    store.purge_expired()
                except Exception:  # noqa: BLE001
                    log.exception("periodic purge failed")

    task = asyncio.create_task(_sweep(), name="session-purge")
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


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


@app.get("/")
def root() -> dict[str, str]:
    """Service root; points to interactive docs."""

    return {"message": "CutFlow CAD API", "docs": "/docs"}


@app.get("/api/health")
def health() -> dict[str, str]:
    """Liveness/readiness probe used by docker-compose and uptime checks."""

    return {"status": "ok", "version": API_VERSION, "service": SERVICE_NAME}
