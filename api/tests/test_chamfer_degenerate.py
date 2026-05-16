"""Degenerate-polygon guard for the chamfer service (M2)."""

from __future__ import annotations

from services import graph as gmod
from services.chamfer import list_corners


def test_list_corners_collinear_polygon_returns_empty() -> None:
    """A loop whose vertices are collinear (zero signed area) must surface
    no corners — every interior angle is ill-defined.
    """

    # Three LINEs stacked along the X axis: A → B → C → A all on y=0.
    # The "polygon" has zero area; ``_signed_area`` returns 0.
    edge_items = [
        ("e1", "LINE", {"x1": 0.0, "y1": 0.0, "x2": 50.0, "y2": 0.0}),
        ("e2", "LINE", {"x1": 50.0, "y1": 0.0, "x2": 100.0, "y2": 0.0}),
        ("e3", "LINE", {"x1": 100.0, "y1": 0.0, "x2": 0.0, "y2": 0.0}),
    ]
    topo = gmod.build_graph(edge_items)
    corners, _edges = list_corners(topo, ["e1", "e2", "e3"])
    assert corners == []


def test_list_corners_tiny_polygon_returns_empty() -> None:
    """A polygon whose area is below the 1e-6 floor is treated as degenerate."""

    # 4 vertices spanning ~1e-4 mm on each side → signed area ≈ 1e-8.
    eps = 1e-4
    edge_items = [
        ("e1", "LINE", {"x1": 0.0, "y1": 0.0, "x2": eps, "y2": 0.0}),
        ("e2", "LINE", {"x1": eps, "y1": 0.0, "x2": eps, "y2": eps}),
        ("e3", "LINE", {"x1": eps, "y1": eps, "x2": 0.0, "y2": eps}),
        ("e4", "LINE", {"x1": 0.0, "y1": eps, "x2": 0.0, "y2": 0.0}),
    ]
    topo = gmod.build_graph(edge_items)
    corners, _edges = list_corners(topo, ["e1", "e2", "e3", "e4"])
    assert corners == []
