"""Session lifecycle endpoints (upload, info, delete)."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from models import FileMeta, SessionInfo
from storage import SessionExpired, SessionNotFound, get_store
from services.dxf_parser import load_document

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["session"])

# Hard limits per PRD.
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_FILES_PER_UPLOAD = 50

# Assembly drawings ("組立図") are out of scope per DESIGN.md §4: CutFlow•CAD
# operates on **single-part** drawings. Folder uploads commonly include the
# parent assembly DXF (e.g. ``25057-P1-0T_昇降軸駆動部組立図.DXF``); silently
# drop those so the user doesn't have to pre-curate the folder.
_ASSEMBLY_NAME_RE = re.compile(r"(組立図|assembly|-0T_)", re.IGNORECASE)


def is_assembly_drawing(filename: str) -> bool:
    """Return True iff the filename looks like an assembly/組立図 DXF."""

    return bool(_ASSEMBLY_NAME_RE.search(filename or ""))


@router.post("/upload", response_model=SessionInfo, status_code=status.HTTP_201_CREATED)
async def upload(files: list[UploadFile] = File(...)) -> SessionInfo:
    """Validate + persist multiple DXF uploads, then create a new session."""

    if not files:
        raise HTTPException(status_code=400, detail="no files provided")
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(
            status_code=400, detail=f"too many files (max {MAX_FILES_PER_UPLOAD})"
        )

    accepted: list[tuple[str, bytes]] = []
    skipped_assembly: list[str] = []
    for f in files:
        name = f.filename or ""
        if not name.lower().endswith(".dxf"):
            raise HTTPException(status_code=400, detail=f"not a .dxf file: {name}")

        # Silently drop assembly drawings — they are out of scope for the
        # single-part workflow and would just confuse the user if rendered.
        if is_assembly_drawing(name):
            log.info("skipping assembly drawing %s", name)
            skipped_assembly.append(name)
            continue

        blob = await f.read()
        if len(blob) == 0:
            raise HTTPException(status_code=400, detail=f"empty file: {name}")
        if len(blob) > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"file exceeds {MAX_FILE_BYTES // (1024 * 1024)}MB: {name}",
            )

        # Smoke-test parseability before accepting.
        try:
            import os
            import tempfile
            from pathlib import Path

            # Windows requires us to close the file handle returned by mkstemp
            # before another process (ezdxf reading) can open it.
            fd, tmp_path = tempfile.mkstemp(suffix=".dxf")
            os.close(fd)
            tmp = Path(tmp_path)
            try:
                tmp.write_bytes(blob)
                load_document(tmp)
            finally:
                tmp.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - ezdxf can throw many flavours
            log.warning("rejecting %s — ezdxf failed: %s", name, exc)
            raise HTTPException(status_code=400, detail=f"not a valid DXF: {name}") from exc

        accepted.append((name, blob))

    if not accepted:
        # All inputs were filtered out (typically all assembly drawings).
        raise HTTPException(
            status_code=400,
            detail=(
                f"no part drawings to upload — {len(skipped_assembly)} assembly "
                f"file(s) were filtered out"
            ),
        )

    sess = get_store().create(accepted)
    if skipped_assembly:
        log.info(
            "session %s: accepted %d files, skipped %d assembly drawing(s): %s",
            sess.session_id,
            len(accepted),
            len(skipped_assembly),
            skipped_assembly,
        )
    # Phase 5 — Discord 通知 (no-op if disabled)
    try:
        from services.discord_notify import notify_session_created

        notify_session_created(sess.session_id, len(accepted))
    except Exception as exc:  # noqa: BLE001 — never block on notify
        log.debug("session_created notify skipped: %s", exc)
    return SessionInfo(
        session_id=sess.session_id,
        files=[
            FileMeta(file_id=f.file_id, name=f.name, size=f.size, status=f.status, error=f.error)
            for f in sess.files
        ],
        expires_at=sess.expires_at,
    )


@router.get("/session/{sid}", response_model=SessionInfo)
async def get_session(sid: str) -> SessionInfo:
    try:
        sess = get_store().get(sid)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except SessionExpired as exc:
        raise HTTPException(status_code=410, detail="session expired") from exc
    return SessionInfo(
        session_id=sess.session_id,
        files=[
            FileMeta(file_id=f.file_id, name=f.name, size=f.size, status=f.status, error=f.error)
            for f in sess.files
        ],
        expires_at=sess.expires_at,
    )


@router.delete("/session/{sid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(sid: str) -> None:
    deleted = get_store().delete(sid)
    if not deleted:
        raise HTTPException(status_code=404, detail="session not found")
    # C7: 関連 job 結果も削除 — 部品 file_id / sheet 配置情報を残さない
    try:
        from services.job_queue import get_queue

        get_queue().purge_for_session(sid)
    except Exception as exc:  # noqa: BLE001 — never block delete on cleanup
        log.debug("job purge after session delete failed: %s", exc)
    return None
