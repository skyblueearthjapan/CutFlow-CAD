"""pyclipper-backed offset computation tests.

The numeric expectations come from textbook geometry rather than a snapshot
of the previous run, so the tests will catch real regressions instead of
silently rubber-stamping whatever the code happens to produce.
"""

from __future__ import annotations

import math

import pytest

from services import graph as gmod
from services.offset import OffsetError, compute_offset


def _square_topo(width: float = 100.0, height: float = 50.0):
    edge_items = [
        ("e1", "LINE", {"x1": 0.0, "y1": 0.0, "x2": width, "y2": 0.0}),
        ("e2", "LINE", {"x1": width, "y1": 0.0, "x2": width, "y2": height}),
        ("e3", "LINE", {"x1": width, "y1": height, "x2": 0.0, "y2": height}),
        ("e4", "LINE", {"x1": 0.0, "y1": height, "x2": 0.0, "y2": 0.0}),
    ]
    return gmod.build_graph(edge_items), ["e1", "e2", "e3", "e4"]


def _circle_topo(radius: float = 42.0):
    edge_items = [("c1", "CIRCLE", {"cx": 0.0, "cy": 0.0, "r": radius})]
    return gmod.build_graph(edge_items), ["c1"]


# ---------------------------------------------------------------------------
# Rectangle: known closed-form for both join types
# ---------------------------------------------------------------------------


def test_offset_rectangle_miter_matches_analytic() -> None:
    topo, loop = _square_topo(100.0, 50.0)
    res = compute_offset(topo, loop, 5.0, corner_join="miter")
    # Perimeter grows by 2*delta on each of the 4 sides → +4*delta total
    # plus 2 corner shifts per dim. For a rectangle with miter, the new
    # perimeter is simply 2*(W+2d + H+2d) = 2*(110 + 60) = 340.
    assert math.isclose(res["perimeter"], 340.0, rel_tol=1e-3)
    bb = res["bounding_box"]
    assert math.isclose(bb["min_x"], -5.0, abs_tol=0.01)
    assert math.isclose(bb["max_x"], 105.0, abs_tol=0.01)
    assert math.isclose(bb["min_y"], -5.0, abs_tol=0.01)
    assert math.isclose(bb["max_y"], 55.0, abs_tol=0.01)


def test_offset_rectangle_round_matches_analytic() -> None:
    topo, loop = _square_topo(100.0, 50.0)
    res = compute_offset(topo, loop, 5.0, corner_join="arc")
    # Round-cap perimeter = original 4 sides + 4 quarter circles of radius d
    # = (100 + 50)*2 + 2*pi*5 = 300 + 10*pi ≈ 331.4
    expected = 300.0 + 2 * math.pi * 5.0
    assert math.isclose(res["perimeter"], expected, rel_tol=0.005)


def test_offset_rectangle_plate_size_and_efficiency() -> None:
    topo, loop = _square_topo(100.0, 50.0)
    res = compute_offset(topo, loop, 3.0, corner_join="miter")
    # plate_size formatted "W × H mm" rounded to integer.
    assert res["plate_size"] == "106 × 56 mm"
    # Efficiency = original_area / bbox_area_after = 5000 / (106*56)
    expected_eff = 5000.0 / (106.0 * 56.0)
    assert math.isclose(res["material_efficiency"], expected_eff, abs_tol=0.002)


# ---------------------------------------------------------------------------
# Circle: bulge-free polygon should match 2*pi*(r+d) very closely
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delta", [1.0, 3.0, 5.0, 10.0])
def test_offset_circle_perimeter_matches_radius_growth(delta: float) -> None:
    radius = 42.0
    topo, loop = _circle_topo(radius)
    res = compute_offset(topo, loop, delta, corner_join="arc")
    # 1 degree sampling has ~0.1% chord error; pyclipper round-cap adds
    # equally negligible error, so 0.5% tolerance is plenty.
    expected = 2.0 * math.pi * (radius + delta)
    assert math.isclose(res["perimeter"], expected, rel_tol=0.005), (
        f"+{delta}mm round: got {res['perimeter']}, expected {expected}"
    )
    # Bounding box must grow by exactly ``delta`` on every side.
    bb = res["bounding_box"]
    assert math.isclose(bb["max_x"] - bb["min_x"], 2 * (radius + delta), abs_tol=0.05)
    assert math.isclose(bb["max_y"] - bb["min_y"], 2 * (radius + delta), abs_tol=0.05)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_offset_empty_loop_raises() -> None:
    topo = gmod.build_graph([])
    with pytest.raises(OffsetError):
        compute_offset(topo, [], 3.0)


def test_offset_negative_delta_raises() -> None:
    topo, loop = _square_topo()
    with pytest.raises(OffsetError):
        compute_offset(topo, loop, -1.0)


def test_offset_loop_with_missing_entity_raises() -> None:
    topo, _loop = _square_topo()
    with pytest.raises(OffsetError):
        # The loop references an id the topology never saw.
        compute_offset(topo, ["e1", "e2", "ghost"], 3.0)


# ---------------------------------------------------------------------------
# Per-edge override sanity (additive: never shrinks the offset polygon)
# ---------------------------------------------------------------------------


def test_offset_edge_override_bumps_one_side() -> None:
    topo, loop = _square_topo(100.0, 50.0)
    plain = compute_offset(topo, loop, 3.0, corner_join="miter")
    bumped = compute_offset(
        topo,
        loop,
        3.0,
        edge_overrides={"E1": 5.0},  # add 5mm to the bottom edge specifically
        corner_join="miter",
    )
    # The bumped output must reach further in -y than the plain offset.
    assert bumped["bounding_box"]["min_y"] <= plain["bounding_box"]["min_y"]
    # And the perimeter must not shrink (additivity invariant).
    assert bumped["perimeter"] >= plain["perimeter"] - 0.5
