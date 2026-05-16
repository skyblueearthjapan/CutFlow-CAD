"""C1a — per-id DELETE for holes / notes / bridges."""

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


def test_delete_hole_by_id(sample_dxf_paths, isolated_store) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        c.post(
            f"/api/session/{sid}/file/{fid}/holes",
            json={"holes": [{"id": "h-1", "position": [5, 5], "diameter": 9.0}]},
        )
        c.post(
            f"/api/session/{sid}/file/{fid}/holes",
            json={"holes": [{"id": "h-2", "position": [10, 10], "diameter": 5.0}]},
        )
        # Sanity: both present.
        assert len(c.get(f"/api/session/{sid}/file/{fid}/holes").json()["holes"]) == 2
        # DELETE one.
        r = c.delete(f"/api/session/{sid}/file/{fid}/holes/h-1")
        assert r.status_code == 204
        remaining = c.get(f"/api/session/{sid}/file/{fid}/holes").json()["holes"]
        assert len(remaining) == 1
        assert remaining[0]["id"] == "h-2"
        # Unknown id → 404.
        bad = c.delete(f"/api/session/{sid}/file/{fid}/holes/h-nonexistent")
        assert bad.status_code == 404


def test_delete_note_by_id(sample_dxf_paths, isolated_store) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        c.post(
            f"/api/session/{sid}/file/{fid}/notes",
            json={"notes": [
                {"id": "n-1", "position": [5, 5], "text": "first"},
                {"id": "n-2", "position": [10, 10], "text": "second"},
            ]},
        )
        r = c.delete(f"/api/session/{sid}/file/{fid}/notes/n-1")
        assert r.status_code == 204
        notes = c.get(f"/api/session/{sid}/file/{fid}/notes").json()["notes"]
        assert [n["id"] for n in notes] == ["n-2"]


def test_delete_bridge_by_id(sample_dxf_paths, isolated_store) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        edges = c.get(f"/api/session/{sid}/file/{fid}/corners").json().get("edges") or []
        if not edges:
            return
        edge_id = edges[0]["edge_id"]
        c.post(
            f"/api/session/{sid}/file/{fid}/bridges",
            json={"bridges": [
                {"id": "b-1", "edge_id": edge_id, "position_ratio": 0.3, "width_mm": 2.0},
                {"id": "b-2", "edge_id": edge_id, "position_ratio": 0.7, "width_mm": 2.0},
            ]},
        )
        r = c.delete(f"/api/session/{sid}/file/{fid}/bridges/b-1")
        assert r.status_code == 204
        bridges = c.get(f"/api/session/{sid}/file/{fid}/bridges").json()["bridges"]
        assert [b["id"] for b in bridges] == ["b-2"]
