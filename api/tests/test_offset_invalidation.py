"""H11 regression: outer re-detection, delete reservation and outer-manual
calls all invalidate the persisted ``offset.json`` so downstream callers
never see a stale preview.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from main import app


def _upload(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201, r.text
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


def test_detect_outer_invalidates_offset(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        # Detect → offset → confirm both files exist.
        c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        c.post(f"/api/session/{sid}/file/{fid}/offset", json={"default_mm": 3.0})
        store = isolated_store
        assert store.offset_path(sid, fid).exists()

        # Re-detect → offset.json must be gone.
        c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        assert not store.offset_path(sid, fid).exists()


def test_delete_invalidates_offset(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        c.post(f"/api/session/{sid}/file/{fid}/offset", json={"default_mm": 3.0})
        store = isolated_store
        assert store.offset_path(sid, fid).exists()

        # Pick an arbitrary entity id and delete it.
        r = c.get(f"/api/session/{sid}/file/{fid}")
        eid = r.json()["entities"][0]["id"]
        c.post(
            f"/api/session/{sid}/file/{fid}/delete",
            json={"entity_ids": [eid]},
        )
        assert not store.offset_path(sid, fid).exists()


def test_invalidate_offset_noop_when_no_cache(isolated_store) -> None:
    """Calling invalidate_offset without an existing file is a quiet no-op."""

    store = isolated_store
    # Spin up a fake session so get_store().get(sid) does not raise.
    sess = store.create([("dummy.dxf", b"0\nSECTION\n0\nENDSEC\n0\nEOF\n")])
    sid = sess.session_id
    fid = sess.files[0].file_id
    assert store.invalidate_offset(sid, fid) is False
