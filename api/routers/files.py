"""Per-file endpoints: parse, delete reservation, export."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from models import DeleteRequest, DeleteResponse, FileEntities
from services.dxf_parser import parse_file
from services.dxf_writer import export_clean_dxf
from storage import SessionExpired, SessionNotFound, get_store

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/session/{sid}/file/{fid}", tags=["files"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve(sid: str, fid: str):
    try:
        store = get_store()
        sf = store.get_file(sid, fid)
        return store, sf
    except SessionExpired as exc:
        raise HTTPException(status_code=410, detail="session expired") from exc
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    except FileNotFoundError as exc:
        # The session metadata still references this file but the bytes on
        # disk are gone (e.g. tmp wiped between requests). Treat as 404.
        raise HTTPException(status_code=404, detail="file no longer available") from exc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=FileEntities)
async def get_file_entities(sid: str, fid: str) -> FileEntities:
    """Parse the DXF and return JSON entities + delete candidates."""

    _store, sf = _resolve(sid, fid)
    try:
        payload = parse_file(sf.path, file_id=fid, name=sf.name)
    except Exception as exc:  # noqa: BLE001 - DXF parsing has many failure modes
        log.exception("parse failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}") from exc

    payload.deleted_ids = _store.get_deleted_for_file(sid, fid)
    return payload


@router.post("/delete", response_model=DeleteResponse)
async def post_delete(sid: str, fid: str, body: DeleteRequest) -> DeleteResponse:
    """Reserve the given entity IDs for removal at export time.

    Unknown entity IDs are silently ignored so a stale client cannot get
    stuck in a 4xx loop — we just don't count them. ``deleted_count``
    reflects the size of the *valid* merged set on disk.
    """

    store, sf = _resolve(sid, fid)

    # Parse once: we need the valid-id set for filtering AND the remaining
    # count for the response, so paying the parse cost twice is wasteful.
    try:
        payload = parse_file(sf.path, file_id=fid, name=sf.name)
    except Exception as exc:  # noqa: BLE001
        log.exception("parse failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}") from exc

    valid_ids = {e.id for e in payload.entities}
    filtered = [eid for eid in body.entity_ids if eid in valid_ids]
    if len(filtered) != len(body.entity_ids):
        dropped = set(body.entity_ids) - valid_ids
        log.info("post_delete: dropped %d unknown id(s) for %s/%s: %s",
                 len(dropped), sid, fid, sorted(dropped)[:10])

    merged = store.update_deleted(sid, fid, filtered)
    remaining = max(payload.stats.total - len(merged), 0)
    return DeleteResponse(deleted_count=len(merged), remaining=remaining)


@router.get("/export")
async def export(sid: str, fid: str, format: str = "dxf") -> FileResponse:
    """Stream the cleaned DXF back to the browser as ``<name>_clean.dxf``."""

    if format != "dxf":
        raise HTTPException(status_code=400, detail="only format=dxf is supported in Phase 1")

    store, sf = _resolve(sid, fid)
    deleted = set(store.get_deleted_for_file(sid, fid))

    out_dir = Path(tempfile.mkdtemp(prefix="cutflow-export-"))
    base = Path(sf.name).stem
    dest = out_dir / f"{base}_clean.dxf"
    try:
        export_clean_dxf(sf.path, deleted, dest)
    except Exception as exc:  # noqa: BLE001
        log.exception("export failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"export failed: {exc}") from exc

    return FileResponse(
        path=str(dest),
        filename=f"{base}_clean.dxf",
        media_type="application/dxf",
    )
