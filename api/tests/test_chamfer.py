"""Chamfer / bevel service + endpoint tests (Phase 3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app
from services import graph as gmod
from services.chamfer import (
    build_annotations,
    chamfer_dxf_extras,
    list_corners,
)


def _square_topo(w: float = 100.0, h: float = 50.0):
    edge_items = [
        ("e1", "LINE", {"x1": 0.0, "y1": 0.0, "x2": w, "y2": 0.0}),
        ("e2", "LINE", {"x1": w, "y1": 0.0, "x2": w, "y2": h}),
        ("e3", "LINE", {"x1": w, "y1": h, "x2": 0.0, "y2": h}),
        ("e4", "LINE", {"x1": 0.0, "y1": h, "x2": 0.0, "y2": 0.0}),
    ]
    return gmod.build_graph(edge_items), ["e1", "e2", "e3", "e4"]


def _upload(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201, r.text
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


# ---------------------------------------------------------------------------
# Service-layer tests
# ---------------------------------------------------------------------------


def test_list_corners_rectangle_has_four_convex() -> None:
    topo, loop = _square_topo()
    corners, edges = list_corners(topo, loop)

    assert len(corners) == 4
    # All 4 corners of a CCW square are 90° convex turns.
    for c in corners:
        assert 89.0 <= c["angle_deg"] <= 91.0
        assert c["is_convex"] is True
        assert c["is_acute"] is False

    # Sequential IDs C1..C4
    assert [c["corner_id"] for c in corners] == ["C1", "C2", "C3", "C4"]

    # Four edges, each midpoint sits on its side.
    assert len(edges) == 4
    assert [e["edge_id"] for e in edges] == ["E1", "E2", "E3", "E4"]
    assert edges[0]["length"] == pytest.approx(100.0)


def test_list_corners_circle_has_no_corners() -> None:
    edge_items = [("c1", "CIRCLE", {"cx": 0.0, "cy": 0.0, "r": 25.0})]
    topo = gmod.build_graph(edge_items)
    corners, edges = list_corners(topo, ["c1"])
    # Circle has no straight corners; we expect zero entries.
    assert corners == []
    # Edge still surfaces as E1 so the bevel UI can address the perimeter.
    assert len(edges) == 1
    assert edges[0]["edge_id"] == "E1"


def test_build_annotations_round_trip() -> None:
    topo, loop = _square_topo()
    corners, edges = list_corners(topo, loop)
    specs = [
        {"corner_id": "C1", "size_mm": 2.0, "angle_deg": 45.0, "type": "C"},
        {"corner_id": "E2", "size_mm": 0.0, "angle_deg": 30.0, "type": "bevel"},
        {"corner_id": "Cghost", "size_mm": 2.0, "angle_deg": 45.0, "type": "C"},
    ]
    items = build_annotations(specs, corners, edges)
    # Ghost ID silently dropped; the two valid specs survive.
    assert len(items) == 2
    labels = {it["corner_id"]: it["label"] for it in items}
    assert labels["C1"] == "C2"
    assert labels["E2"] == "開先 30°"


def test_chamfer_dxf_extras_carries_chamfer_layer() -> None:
    topo, loop = _square_topo()
    corners, edges = list_corners(topo, loop)
    specs = [{"corner_id": "C1", "size_mm": 3.0, "angle_deg": 45.0, "type": "C"}]
    extras = chamfer_dxf_extras(specs, corners, edges)
    assert extras and extras[0]["layer"] == "CUTFLOW_CHAMFER"
    assert extras[0]["text"] == "C3"


# ---------------------------------------------------------------------------
# Endpoint tests (require a real DXF sample)
# ---------------------------------------------------------------------------


def test_corners_requires_confirmed_outer(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.get(f"/api/session/{sid}/file/{fid}/corners")
        assert r.status_code == 422


def test_chamfer_flow_on_sample(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["medium"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        # Confirm outer first.
        r = c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        assert r.status_code == 200, r.text

        # Corners endpoint surfaces at least one edge.
        r2 = c.get(f"/api/session/{sid}/file/{fid}/corners")
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert "edges" in body
        assert len(body["edges"]) >= 1

        # Pick the first known label (corner or edge) for the spec.
        target = (
            body["corners"][0]["corner_id"]
            if body["corners"]
            else body["edges"][0]["edge_id"]
        )

        # POST chamfer with one spec.
        r3 = c.post(
            f"/api/session/{sid}/file/{fid}/chamfer",
            json={
                "specs": [
                    {"corner_id": target, "size_mm": 2.0, "angle_deg": 45.0, "type": "C"},
                ]
            },
        )
        assert r3.status_code == 200, r3.text
        out = r3.json()
        assert len(out["specs"]) == 1
        assert len(out["geometry"]["items"]) == 1

        # GET round-trip.
        r4 = c.get(f"/api/session/{sid}/file/{fid}/chamfer")
        assert r4.status_code == 200
        assert len(r4.json()["specs"]) == 1


def test_chamfer_rejects_unknown_corner_id(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        assert r.status_code == 200

        r2 = c.post(
            f"/api/session/{sid}/file/{fid}/chamfer",
            json={
                "specs": [
                    {"corner_id": "ZZZ_UNKNOWN", "size_mm": 2.0, "angle_deg": 45.0,
                     "type": "C"},
                ]
            },
        )
        assert r2.status_code == 422
