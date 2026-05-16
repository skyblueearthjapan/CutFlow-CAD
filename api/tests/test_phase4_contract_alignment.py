"""C1 — frontend/backend contract alignment snapshot tests.

These guard the wire shapes the frontend relies on (id / position /
position_ratio / width_mm / snapped / type ...). A breaking rename on
either side trips these well before the browser ever sees a 422.
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


def test_dimensions_wrapped_list_shape(sample_dxf_paths, isolated_store) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        # POST with the wrapped contract.
        r = c.post(
            f"/api/session/{sid}/file/{fid}/dimensions",
            json={"dimensions": [
                {"id": "d-1", "type": "linear", "p1": [0, 0], "p2": [10, 0]},
            ]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "dimensions" in body
        assert body["dimensions"][0]["id"] == "d-1"
        assert "p1" in body["dimensions"][0]
        # GET mirrors POST.
        r = c.get(f"/api/session/{sid}/file/{fid}/dimensions")
        assert r.status_code == 200
        assert r.json()["dimensions"][0]["id"] == "d-1"


def test_notes_use_welding_enum_value(sample_dxf_paths, isolated_store) -> None:
    """The wire preset is ``welding`` (was ``weld`` in early mocks)."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.post(
            f"/api/session/{sid}/file/{fid}/notes",
            json={
                "notes": [
                    {
                        "id": "n-1",
                        "position": [5, 5],
                        "text": "Ra 3.2",
                        "preset": "welding",
                        "font_size_mm": 2.5,
                    }
                ]
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["notes"][0]["preset"] == "welding"

        # Old enum value is rejected (422 from Pydantic).
        bad = c.post(
            f"/api/session/{sid}/file/{fid}/notes",
            json={
                "notes": [
                    {
                        "id": "n-2",
                        "position": [5, 5],
                        "text": "Old enum",
                        "preset": "weld",
                        "font_size_mm": 2.5,
                    }
                ]
            },
        )
        assert bad.status_code == 422


def test_holes_pattern_uses_anchor_and_spacing(sample_dxf_paths, isolated_store) -> None:
    """The pattern request shape is ``anchor`` + ``spacing`` (not origin/pitch_x/pitch_y)."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.post(
            f"/api/session/{sid}/file/{fid}/holes/pattern",
            json={
                "anchor": [0, 0],
                "rows": 2,
                "cols": 3,
                "spacing": [10, 10],
                "diameter": 5.0,
            },
        )
        assert r.status_code == 200, r.text
        holes = r.json()["holes"]
        assert len(holes) == 6
        assert "position" in holes[0]
        assert "id" in holes[0]


def test_snap_uses_position_snapped_and_type(sample_dxf_paths, isolated_store) -> None:
    """SnapRequest body: ``position`` + ``snap_types`` + ``tolerance``.
    SnapResponse: ``snapped`` + ``type`` (+ optional entity_id/distance)."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.post(
            f"/api/session/{sid}/file/{fid}/snap",
            json={
                "position": [0, 0],
                "snap_types": ["endpoint", "midpoint", "intersection", "center"],
                "tolerance": 100.0,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # The body shape always has ``snapped`` / ``type`` keys even when empty.
        assert "snapped" in body
        assert "type" in body


def test_bridges_wrap_and_id_field(sample_dxf_paths, isolated_store) -> None:
    """Bridge POST body uses ``{bridges: [...]}`` with ``id`` / ``width_mm``."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        # Confirm outer so /bridges accepts the payload.
        c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        r = c.get(f"/api/session/{sid}/file/{fid}/corners")
        edges = r.json().get("edges") or []
        if not edges:
            return  # outer detection didn't surface edges on this fixture
        edge_id = edges[0]["edge_id"]

        post = c.post(
            f"/api/session/{sid}/file/{fid}/bridges",
            json={
                "bridges": [
                    {
                        "id": "b-1",
                        "edge_id": edge_id,
                        "position_ratio": 0.4,
                        "width_mm": 3.0,
                    }
                ]
            },
        )
        assert post.status_code == 200, post.text
        bridges = post.json()["bridges"]
        assert bridges[0]["id"] == "b-1"
        assert bridges[0]["width_mm"] == 3.0
        # C1 — ``position`` is server-enriched.
        assert "position" in bridges[0]


def test_edited_vertex_uses_new_position(sample_dxf_paths, isolated_store) -> None:
    """EditedVertex shape: entity_id + vertex_index + ``new_position`` (no edit_id)."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        ents = c.get(f"/api/session/{sid}/file/{fid}").json()["entities"]
        line = next((e for e in ents if e["type"] == "LINE"), None)
        if line is None:
            return
        r = c.post(
            f"/api/session/{sid}/file/{fid}/edit-vertex",
            json={
                "edits": [
                    {
                        "entity_id": line["id"],
                        "vertex_index": 0,
                        "new_position": [1.0, 2.0],
                    }
                ]
            },
        )
        assert r.status_code == 200, r.text
        edits = r.json()["edits"]
        assert edits[0]["new_position"] == [1.0, 2.0]
        # No ``edit_id`` in the payload — keyed by (entity_id, vertex_index).
        assert "edit_id" not in edits[0]
