"""Phase 4 — snap-point service + endpoint tests."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from main import app
from models.schemas import EntityOut
from services.snap import find_snap


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
# Service
# ---------------------------------------------------------------------------


def _line(eid: str, x1: float, y1: float, x2: float, y2: float) -> EntityOut:
    return EntityOut(
        id=eid, type="LINE", geom={"x1": x1, "y1": y1, "x2": x2, "y2": y2}
    )


def test_snap_endpoint_returns_nearest_line_corner() -> None:
    ents = [_line("e1", 0, 0, 100, 0)]
    hit = find_snap((0.5, 0.5), ents, ["endpoint"], 5.0)
    assert hit is not None
    assert hit["type"] == "endpoint"
    assert hit["snapped"] == [0.0, 0.0]
    assert hit["entity_id"] == "e1"


def test_snap_midpoint() -> None:
    ents = [_line("e1", 0, 0, 100, 0)]
    hit = find_snap((50.1, 0.0), ents, ["midpoint"], 5.0)
    assert hit is not None
    assert hit["type"] == "midpoint"
    assert hit["snapped"] == [50.0, 0.0]


def test_snap_intersection_of_perpendicular_lines() -> None:
    ents = [_line("h", 0, 50, 100, 50), _line("v", 50, 0, 50, 100)]
    hit = find_snap((49.5, 49.5), ents, ["intersection"], 5.0)
    assert hit is not None
    assert hit["type"] == "intersection"
    assert abs(hit["snapped"][0] - 50.0) < 1e-3
    assert abs(hit["snapped"][1] - 50.0) < 1e-3


def test_snap_circle_center_and_quadrant() -> None:
    ents = [EntityOut(id="c1", type="CIRCLE", geom={"cx": 10, "cy": 10, "r": 5})]
    hit = find_snap((10.2, 10.1), ents, ["center"], 1.0)
    assert hit is not None and hit["type"] == "center"

    hit2 = find_snap((15.1, 10.0), ents, ["quadrant"], 1.0)
    assert hit2 is not None and hit2["type"] == "quadrant"
    assert hit2["snapped"] == [15.0, 10.0]


def test_snap_returns_none_when_out_of_tolerance() -> None:
    ents = [_line("e1", 0, 0, 100, 0)]
    assert find_snap((50.0, 100.0), ents, ["endpoint", "midpoint"], 5.0) is None


def test_snap_grid_is_fallback_only() -> None:
    """Grid snap fires only when nothing else lands within tolerance."""

    ents = [_line("e1", 0, 0, 100, 0)]
    # Endpoint at (0,0) is within tol; grid must NOT win.
    hit = find_snap((0.4, 0.4), ents, ["endpoint", "grid"], 5.0)
    assert hit is not None
    assert hit["type"] == "endpoint"

    # No entities → grid fallback wins.
    hit2 = find_snap((3.7, 2.2), [], ["grid"], 5.0)
    assert hit2 is not None
    assert hit2["type"] == "grid"
    assert hit2["snapped"] == [4.0, 2.0]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def test_snap_endpoint_smoke(sample_dxf_paths: dict[str, Path], isolated_store) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        # Query near origin — every sample has at least one entity near 0.
        r = c.post(
            f"/api/session/{sid}/file/{fid}/snap",
            json={"position": [0.0, 0.0], "snap_types": ["endpoint", "center"],
                  "tolerance": 100.0},
        )
        assert r.status_code == 200, r.text
        # Either snapped (sample-dependent) or no-hit; both are valid.
        body = r.json()
        assert "snapped" in body
