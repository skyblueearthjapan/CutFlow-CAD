"""C1 regression: edge_overrides keys are 1-based ``E{n}`` labels AND the
value is the *effective* offset for that edge (NOT additive to default).

These tests pin the contract the frontend relies on:
* keys are ``E1`` .. ``En`` in loop traversal order
* ``edge_overrides={"E1": 5.0}`` with ``default_mm=3`` shifts E1's side by
  5 mm outward — not 3+5 = 8 mm.
"""

from __future__ import annotations

import math

from services import graph as gmod
from services.offset import compute_offset


def _square_topo(width: float = 100.0, height: float = 50.0):
    """A CCW square: e1=bottom, e2=right, e3=top, e4=left."""

    edge_items = [
        ("e1", "LINE", {"x1": 0.0, "y1": 0.0, "x2": width, "y2": 0.0}),
        ("e2", "LINE", {"x1": width, "y1": 0.0, "x2": width, "y2": height}),
        ("e3", "LINE", {"x1": width, "y1": height, "x2": 0.0, "y2": height}),
        ("e4", "LINE", {"x1": 0.0, "y1": height, "x2": 0.0, "y2": 0.0}),
    ]
    return gmod.build_graph(edge_items), ["e1", "e2", "e3", "e4"]


def test_edge_override_effective_value_not_additive() -> None:
    """``E1`` override of 5.0 with default_mm=3 → bottom side at -5, not -8."""

    topo, loop = _square_topo(100.0, 50.0)
    res = compute_offset(
        topo,
        loop,
        default_mm=3.0,
        edge_overrides={"E1": 5.0},
        corner_join="miter",
    )
    # E1 = bottom edge → push in -y. The effective reach is 5 mm.
    assert math.isclose(res["bounding_box"]["min_y"], -5.0, abs_tol=0.1), (
        f"E1 should be 5 mm below origin (effective), got "
        f"{res['bounding_box']['min_y']}"
    )
    # Sides without an override stay at the default 3 mm shift.
    assert math.isclose(res["bounding_box"]["max_y"], 53.0, abs_tol=0.1)
    assert math.isclose(res["bounding_box"]["min_x"], -3.0, abs_tol=0.1)
    assert math.isclose(res["bounding_box"]["max_x"], 103.0, abs_tol=0.1)


def test_edge_override_smaller_than_default_is_clamped_to_default() -> None:
    """If an override is <= default_mm, the global Clipper pass already
    covers it — the offset should equal the default-only baseline."""

    topo, loop = _square_topo(100.0, 50.0)
    baseline = compute_offset(topo, loop, default_mm=5.0, corner_join="miter")
    capped = compute_offset(
        topo,
        loop,
        default_mm=5.0,
        edge_overrides={"E1": 2.0},  # below default
        corner_join="miter",
    )
    # The capped run must match the baseline exactly (within precision).
    assert math.isclose(
        capped["bounding_box"]["min_y"], baseline["bounding_box"]["min_y"], abs_tol=0.01
    )


def test_edge_override_each_side_independently() -> None:
    """Override every side with the same value → equivalent to bumping
    the global default. Tests that the E1..En key mapping is consistent
    around the loop."""

    topo, loop = _square_topo(100.0, 50.0)
    flat = compute_offset(topo, loop, default_mm=5.0, corner_join="miter")
    bumped = compute_offset(
        topo,
        loop,
        default_mm=3.0,
        edge_overrides={"E1": 5.0, "E2": 5.0, "E3": 5.0, "E4": 5.0},
        corner_join="miter",
    )
    # All four sides at +5 reach → same bbox as default_mm=5 with no overrides.
    for k in ("min_x", "min_y", "max_x", "max_y"):
        assert math.isclose(
            bumped["bounding_box"][k], flat["bounding_box"][k], abs_tol=0.1
        )
