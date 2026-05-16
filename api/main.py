"""CutFlow•CAD API — Phase 0 skeleton."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

API_VERSION = "0.1.0"
SERVICE_NAME = "cutflow-cad-api"

# CORS origins for local development (Vite default :5173, alt :3000).
ALLOWED_ORIGINS: list[str] = [
    "http://localhost:5173",
    "http://localhost:3000",
]

app = FastAPI(title="CutFlow•CAD API", version=API_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],  # TODO (Phase 4): 本番では allow_methods=["GET","POST","OPTIONS"] / 必要なheadersに絞る
    allow_headers=["*"],  # TODO (Phase 4): 本番では allow_headers=["Content-Type","Authorization"] 等、必要なheadersに絞る
)


@app.get("/")
def root() -> dict[str, str]:
    """Service root; points to interactive docs."""
    return {"message": "CutFlow CAD API", "docs": "/docs"}


@app.get("/api/health")
def health() -> dict[str, str]:
    """Liveness/readiness probe used by docker-compose and uptime checks."""
    return {"status": "ok", "version": API_VERSION, "service": SERVICE_NAME}


# ---------------------------------------------------------------------------
# Reserved for later phases (do NOT implement in Phase 0):
#   POST /api/upload                        — multi-file DXF upload, session create
#   GET  /api/session/{sid}                 — session info & file list
#   GET  /api/session/{sid}/file/{fid}      — entities (SVG-ready JSON)
#   POST /api/session/{sid}/file/{fid}/detect-outer
#   POST /api/session/{sid}/file/{fid}/offset
#   POST /api/session/{sid}/file/{fid}/delete
#   POST /api/session/{sid}/export          — DXF/PDF export
#   WS   /ws/{sid}                          — realtime sync
# ---------------------------------------------------------------------------
