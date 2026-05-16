"""Filesystem-backed CutFlow CAD session store.

Layout
------
::

    /tmp/cutflow-sessions/{sid}/
        meta.json              # session metadata (created_at, expires_at, files[])
        deleted.json           # per-file delete reservations (id list)
        originals/{fid}.dxf    # uploaded DXFs, file-id keyed (extension preserved)

The store is filesystem-only; no DB, no in-memory cache (a single process can
serve a single user concurrently per the PRD). The 24h TTL is enforced lazily
on access **and** by a background cleanup task started from ``main.py``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

SESSION_TTL_HOURS = 24
_DEFAULT_ROOT = Path(tempfile.gettempdir()) / "cutflow-sessions"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SessionError(Exception):
    """Base class for session-store failures."""


class SessionNotFound(SessionError):
    pass


class SessionExpired(SessionError):
    pass


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class StoredFile:
    file_id: str
    name: str
    size: int
    path: Path
    status: str = "ready"
    error: str | None = None


@dataclass
class StoredSession:
    session_id: str
    created_at: datetime
    expires_at: datetime
    files: list[StoredFile]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class SessionStore:
    def __init__(self, root: Path | None = None, ttl_hours: int = SESSION_TTL_HOURS) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT
        self.ttl = timedelta(hours=ttl_hours)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- creation -----------------------------------------------------------

    def create(self, files: list[tuple[str, bytes]]) -> StoredSession:
        """Create a new session and persist the supplied uploads.

        Args
        ----
        files: list of ``(filename, bytes)`` already validated by the caller.
        """

        sid = uuid.uuid4().hex
        sdir = self.root / sid
        (sdir / "originals").mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        stored: list[StoredFile] = []
        for name, blob in files:
            fid = uuid.uuid4().hex
            path = sdir / "originals" / f"{fid}.dxf"
            path.write_bytes(blob)
            stored.append(StoredFile(file_id=fid, name=name, size=len(blob), path=path))

        sess = StoredSession(
            session_id=sid,
            created_at=now,
            expires_at=now + self.ttl,
            files=stored,
        )
        self._write_meta(sess)
        self._write_deleted(sid, {})
        log.info("session %s created with %d files", sid, len(stored))
        return sess

    # -- lookup -------------------------------------------------------------

    def get(self, sid: str) -> StoredSession:
        sdir = self.root / sid
        meta = sdir / "meta.json"
        if not meta.exists():
            raise SessionNotFound(sid)
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SessionError(f"corrupted meta for {sid}: {exc}") from exc

        expires_at = datetime.fromisoformat(data["expires_at"])
        if expires_at < datetime.now(timezone.utc):
            # Lazy purge.
            self.delete(sid)
            raise SessionExpired(sid)

        files = [
            StoredFile(
                file_id=f["file_id"],
                name=f["name"],
                size=int(f["size"]),
                path=Path(f["path"]),
                status=f.get("status", "ready"),
                error=f.get("error"),
            )
            for f in data["files"]
        ]
        return StoredSession(
            session_id=data["session_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=expires_at,
            files=files,
        )

    def get_file(self, sid: str, fid: str) -> StoredFile:
        sess = self.get(sid)
        for f in sess.files:
            if f.file_id == fid:
                # Catch the "meta still references a file that's been wiped"
                # case — bubble up as FileNotFoundError so the router can map
                # it to a 404 rather than the more confusing 410 (expired).
                if not f.path.exists():
                    raise FileNotFoundError(f"{sid}/{fid}: {f.path}")
                return f
        raise SessionNotFound(f"{sid}/{fid}")

    # -- delete reservation -------------------------------------------------

    def read_deleted(self, sid: str) -> dict[str, list[str]]:
        path = self.root / sid / "deleted.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def update_deleted(self, sid: str, fid: str, entity_ids: Iterable[str]) -> list[str]:
        """Merge ``entity_ids`` into the existing reservation for ``fid``.

        Returns the full deduplicated list for that file.
        """

        # Validate session existence first (raises if missing/expired).
        self.get(sid)

        data = self.read_deleted(sid)
        existing = set(data.get(fid, []))
        existing.update(entity_ids)
        merged = sorted(existing)
        data[fid] = merged
        self._write_deleted(sid, data)
        return merged

    def get_deleted_for_file(self, sid: str, fid: str) -> list[str]:
        return list(self.read_deleted(sid).get(fid, []))

    # -- removal ------------------------------------------------------------

    def delete(self, sid: str) -> bool:
        sdir = self.root / sid
        if not sdir.exists():
            return False
        shutil.rmtree(sdir, ignore_errors=True)
        log.info("session %s deleted", sid)
        return True

    # -- TTL sweep ----------------------------------------------------------

    def purge_expired(self, now: datetime | None = None) -> int:
        """Remove every session whose ``expires_at`` is in the past."""

        if not self.root.exists():
            return 0
        now = now or datetime.now(timezone.utc)
        removed = 0
        for sdir in self.root.iterdir():
            if not sdir.is_dir():
                continue
            meta = sdir / "meta.json"
            if not meta.exists():
                # Orphaned dir; remove if older than TTL.
                try:
                    mtime = datetime.fromtimestamp(sdir.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    continue
                if now - mtime > self.ttl:
                    shutil.rmtree(sdir, ignore_errors=True)
                    removed += 1
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                expires_at = datetime.fromisoformat(data["expires_at"])
            except Exception:  # noqa: BLE001
                shutil.rmtree(sdir, ignore_errors=True)
                removed += 1
                continue
            if expires_at < now:
                shutil.rmtree(sdir, ignore_errors=True)
                removed += 1
        if removed:
            log.info("purged %d expired sessions", removed)
        return removed

    # -- internal -----------------------------------------------------------

    def _write_meta(self, sess: StoredSession) -> None:
        path = self.root / sess.session_id / "meta.json"
        payload = {
            "session_id": sess.session_id,
            "created_at": sess.created_at.isoformat(),
            "expires_at": sess.expires_at.isoformat(),
            "files": [
                {
                    "file_id": f.file_id,
                    "name": f.name,
                    "size": f.size,
                    "path": str(f.path),
                    "status": f.status,
                    "error": f.error,
                }
                for f in sess.files
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_deleted(self, sid: str, data: dict[str, list[str]]) -> None:
        """Atomically replace ``deleted.json`` so a concurrent reader/writer
        never observes a torn file. We write a sibling temp file then call
        ``os.replace`` (atomic on both POSIX and Windows when on the same
        volume) — protects against lost updates on rapid-fire deletes."""

        target = self.root / sid / "deleted.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Use mkstemp on the same directory so os.replace stays atomic.
        fd, tmp_path = tempfile.mkstemp(prefix=".deleted.", suffix=".json", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp_path, target)
        except Exception:
            # Best-effort cleanup of the temp file on error.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Process-wide singleton + override hook for tests
# ---------------------------------------------------------------------------


_STORE: SessionStore | None = None


def get_store() -> SessionStore:
    global _STORE
    if _STORE is None:
        root_env = os.environ.get("CUTFLOW_SESSION_ROOT")
        ttl_env = os.environ.get("CUTFLOW_SESSION_TTL_HOURS")
        ttl = int(ttl_env) if ttl_env else SESSION_TTL_HOURS
        _STORE = SessionStore(
            root=Path(root_env) if root_env else None,
            ttl_hours=ttl,
        )
    return _STORE


def reset_store_for_tests(root: Path) -> SessionStore:
    """Override the singleton with a test-scoped temp directory."""

    global _STORE
    _STORE = SessionStore(root=root)
    return _STORE
