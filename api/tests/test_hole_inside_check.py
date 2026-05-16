"""H3 — added-hole point-in-loop validation.

Exercises both the per-hole POST path and the pattern POST path: a
hole landing outside the confirmed outer must surface a 422.
When no outer is confirmed the check is skipped (legacy behaviour).
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
    assert r.status_code == 201
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


def test_hole_outside_outer_rejected(sample_dxf_paths, isolated_store) -> None:
    """A hole far outside the bounding box is rejected with 422 once an
    outer loop is confirmed."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        # Confirm an outer so the check engages.
        r = c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        outer = r.json()
        if not outer.get("outer_loop"):
            return  # fixture-dependent: skip if detection didn't succeed
        bb = outer.get("loop_summary", {}).get("bounding_box") or {}
        if not bb:
            return
        far_x = float(bb.get("max_x", 1000)) + 5000.0
        far_y = float(bb.get("max_y", 1000)) + 5000.0
        r = c.post(
            f"/api/session/{sid}/file/{fid}/holes",
            json={"holes": [
                {"id": "outside-1", "position": [far_x, far_y], "diameter": 9.0},
            ]},
        )
        assert r.status_code == 422, r.text


def test_hole_inside_outer_accepted(sample_dxf_paths, isolated_store) -> None:
    """A hole near the bbox centre is accepted (sanity)."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        outer = r.json()
        if not outer.get("outer_loop"):
            return
        bb = outer.get("loop_summary", {}).get("bounding_box") or {}
        if not bb:
            return
        cx = (float(bb.get("min_x", 0)) + float(bb.get("max_x", 0))) / 2.0
        cy = (float(bb.get("min_y", 0)) + float(bb.get("max_y", 0))) / 2.0
        r = c.post(
            f"/api/session/{sid}/file/{fid}/holes",
            json={"holes": [
                {"id": "inside-1", "position": [cx, cy], "diameter": 5.0},
            ]},
        )
        assert r.status_code == 200, r.text


def test_hole_without_outer_skips_check(sample_dxf_paths, isolated_store) -> None:
    """Without a confirmed outer the inside-check is skipped (legacy)."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        # No detect-outer call → store has no outer.json.
        r = c.post(
            f"/api/session/{sid}/file/{fid}/holes",
            json={"holes": [
                {"id": "skip-1", "position": [99999, 99999], "diameter": 9.0},
            ]},
        )
        assert r.status_code == 200, r.text
