"""H6 regression: ``POST /offset`` returns 409 when the persisted outer
status is anything but ``success`` (or ``manual``, persisted as ``success``).
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


def test_low_confidence_outer_blocks_offset(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    """Manually rewrite outer.json with ``status="low_confidence"`` and
    verify the offset endpoint returns 409."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        # Trigger detect-outer so outer.json exists.
        r = c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        assert r.status_code == 200

        # Force the persisted status to a non-success value.
        store = isolated_store
        saved = store.read_outer(sid, fid)
        assert saved
        saved["status"] = "low_confidence"
        store.write_outer(sid, fid, saved)

        r = c.post(f"/api/session/{sid}/file/{fid}/offset", json={"default_mm": 3.0})
        assert r.status_code == 409, r.text
        assert "確定" in r.json()["detail"] or "未確定" in r.json()["detail"]
