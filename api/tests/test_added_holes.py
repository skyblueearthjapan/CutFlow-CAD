"""Phase 4 — hole-add service + endpoint tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import app
from models.schemas import AddedHole
from services.added_holes import expand_pattern, holes_dxf_extras


def _upload(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_added_hole_rejects_zero_diameter() -> None:
    with pytest.raises(ValidationError):
        AddedHole(id="h1", position=[0, 0], diameter=0.0)


def test_added_hole_rejects_negative_diameter() -> None:
    with pytest.raises(ValidationError):
        AddedHole(id="h1", position=[0, 0], diameter=-5.0)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_holes_dxf_extras_uses_hole_layer() -> None:
    out = holes_dxf_extras([{"id": "h1", "position": [10.0, 20.0], "diameter": 8.0}])
    assert len(out) == 1
    assert out[0]["layer"] == "CUTFLOW_HOLE"
    assert out[0]["radius"] == 4.0
    assert out[0]["center"] == [10.0, 20.0]


def test_expand_pattern_3x4() -> None:
    out = expand_pattern([0.0, 0.0], rows=3, cols=4, spacing=[20, 30], diameter=9.0)
    assert len(out) == 12
    # First hole at anchor, last at (3*20, 2*30) = (60, 60).
    assert out[0]["position"] == [0.0, 0.0]
    assert out[-1]["position"] == [60.0, 60.0]
    # All carry the same diameter.
    for h in out:
        assert h["diameter"] == 9.0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def test_holes_round_trip_and_append(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)

        r = c.post(
            f"/api/session/{sid}/file/{fid}/holes",
            json={"holes": [{"id": "h1", "position": [5.0, 5.0], "diameter": 9.0}]},
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["holes"]) == 1

        # Append a second hole.
        r2 = c.post(
            f"/api/session/{sid}/file/{fid}/holes",
            json={"holes": [{"id": "h2", "position": [10.0, 10.0], "diameter": 12.0,
                              "tap_note": "M10"}]},
        )
        assert r2.status_code == 200
        ids = sorted(h["id"] for h in r2.json()["holes"])
        assert ids == ["h1", "h2"]


def test_holes_pattern_endpoint(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.post(
            f"/api/session/{sid}/file/{fid}/holes/pattern",
            json={"anchor": [0, 0], "rows": 2, "cols": 3, "spacing": [10, 10],
                  "diameter": 6.0},
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["holes"]) == 6
