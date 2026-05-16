"""Phase 4 — vertex edit service + endpoint tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app
from models.schemas import EntityOut
from services.edits import validate_edits


def _upload(c: TestClient, path: Path) -> tuple[str, str, list[dict]]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201, r.text
    data = r.json()
    sid = data["session_id"]
    fid = data["files"][0]["file_id"]
    r2 = c.get(f"/api/session/{sid}/file/{fid}")
    assert r2.status_code == 200
    return sid, fid, r2.json()["entities"]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_validate_edits_accepts_line_endpoint() -> None:
    ent = EntityOut(id="e001", type="LINE", geom={"x1": 0, "y1": 0, "x2": 10, "y2": 0})
    valid, errors = validate_edits(
        [{"entity_id": "e001", "vertex_index": 1, "new_position": [12.0, 5.0]}],
        {"e001": ent},
    )
    assert errors == []
    assert valid[0]["new_position"] == [12.0, 5.0]


def test_validate_edits_rejects_out_of_range() -> None:
    ent = EntityOut(id="e001", type="LINE", geom={"x1": 0, "y1": 0, "x2": 10, "y2": 0})
    valid, errors = validate_edits(
        [{"entity_id": "e001", "vertex_index": 99, "new_position": [1.0, 1.0]}],
        {"e001": ent},
    )
    assert valid == []
    assert len(errors) == 1
    assert "vertex_index" in errors[0]


def test_validate_edits_rejects_unknown_entity() -> None:
    valid, errors = validate_edits(
        [{"entity_id": "ghost", "vertex_index": 0, "new_position": [1.0, 1.0]}],
        {},
    )
    assert valid == []
    assert any("未知" in e for e in errors)


def test_validate_edits_rejects_unsupported_type() -> None:
    ent = EntityOut(id="t1", type="TEXT", geom={"x": 0, "y": 0, "text": "x"})
    valid, errors = validate_edits(
        [{"entity_id": "t1", "vertex_index": 0, "new_position": [1.0, 1.0]}],
        {"t1": ent},
    )
    assert valid == []
    assert errors


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def test_edits_round_trip(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid, entities = _upload(c, path)
        # Find a LINE in the parsed entities (samples almost always have one).
        line = next((e for e in entities if e["type"] == "LINE"), None)
        if line is None:
            pytest.skip("no LINE entity in sample")

        r = c.post(
            f"/api/session/{sid}/file/{fid}/edit-vertex",
            json={"edits": [{"entity_id": line["id"], "vertex_index": 0,
                              "new_position": [99.5, 88.5]}]},
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["edits"]) == 1

        r2 = c.get(f"/api/session/{sid}/file/{fid}/edits")
        assert r2.status_code == 200
        assert r2.json()["edits"][0]["new_position"] == [99.5, 88.5]

        # Re-edit the same vertex should replace, not append.
        r3 = c.post(
            f"/api/session/{sid}/file/{fid}/edit-vertex",
            json={"edits": [{"entity_id": line["id"], "vertex_index": 0,
                              "new_position": [11.0, 22.0]}]},
        )
        assert r3.status_code == 200
        r4 = c.get(f"/api/session/{sid}/file/{fid}/edits")
        edits = r4.json()["edits"]
        assert len(edits) == 1
        assert edits[0]["new_position"] == [11.0, 22.0]
