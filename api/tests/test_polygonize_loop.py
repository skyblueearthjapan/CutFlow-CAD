"""H4/H5 regression: shapely-backed ``find_closed_loops`` recovers
multi-entity faces (D-shape: LINE + ARC) that ``nx.cycle_basis`` alone
would miss.
"""

from __future__ import annotations

import math

from services import graph as gmod


def test_d_shape_two_edge_face_found() -> None:
    """A semicircle ARC + straight LINE between the same two endpoints
    forms a D-shape face. The legacy cycle_basis collapses the multi-edge,
    polygonize_full recovers it."""

    radius = 10.0
    # LINE from (-r, 0) to (+r, 0)
    line = ("L", "LINE", {"x1": -radius, "y1": 0.0, "x2": radius, "y2": 0.0})
    # Upper-semicircle ARC from (+r, 0) to (-r, 0) (CCW from 0° to 180°).
    arc = (
        "A",
        "ARC",
        {
            "cx": 0.0, "cy": 0.0, "r": radius,
            "start_angle": 0.0, "end_angle": 180.0,
        },
    )
    topo = gmod.build_graph([line, arc])

    loops = gmod.find_closed_loops(topo)
    assert loops, "expected at least one closed loop from a D-shape"

    # One of the loops must reference both entities.
    sig = {frozenset(l) for l in loops}
    assert frozenset({"L", "A"}) in sig, (
        f"two-edge face missing from {sig!r}"
    )


def test_polygonize_face_area_matches_geometry() -> None:
    """The face area must match analytic D = π r² / 2."""

    radius = 12.0
    line = ("L", "LINE", {"x1": -radius, "y1": 0.0, "x2": radius, "y2": 0.0})
    arc = (
        "A",
        "ARC",
        {
            "cx": 0.0, "cy": 0.0, "r": radius,
            "start_angle": 0.0, "end_angle": 180.0,
        },
    )
    topo = gmod.build_graph([line, arc])

    loops = gmod.find_closed_loops(topo)
    found = False
    for loop in loops:
        if set(loop) == {"L", "A"}:
            pts = gmod.polygon_from_loop(topo, loop)
            area = gmod.polygon_area(pts)
            assert math.isclose(area, math.pi * radius * radius / 2.0, rel_tol=0.02)
            found = True
            break
    assert found, "D-shape loop not enumerated"
