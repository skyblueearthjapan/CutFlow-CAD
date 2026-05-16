"""H8 regression: tiny circles (area_ratio < ~1%) must not score as
``success``. The area-ratio weight ensures a tap-mark circle cannot
outrank the real part outline on raw method-prior + completeness alone.
"""

from __future__ import annotations

from services.outer_detector import detect_outer


def test_tiny_circle_in_large_modelspace_is_low_confidence() -> None:
    """A r=2 mm CIRCLE with another 500 mm × 300 mm rectangle outline in
    the same modelspace: the rectangle wins, the tap-mark circle never
    reaches ``status="success"`` on its own."""

    items = [
        # The real part outline — closed polyline, area 150000 mm².
        (
            "p_outer", "LWPOLYLINE", "other",
            {
                "vertices": [
                    [0.0, 0.0, 0.0],
                    [500.0, 0.0, 0.0],
                    [500.0, 300.0, 0.0],
                    [0.0, 300.0, 0.0],
                ],
                "closed": True,
            },
        ),
        # An obvious "inner" hole so the closed polyline scores well.
        ("h", "CIRCLE", "other", {"cx": 250.0, "cy": 150.0, "r": 10.0}),
        # A tap-mark sized circle: r=2 mm. With H8 it should be punished.
        ("tap", "CIRCLE", "other", {"cx": 50.0, "cy": 50.0, "r": 2.0}),
    ]
    r = detect_outer(items)
    # The winner must be the rectangle (or the inner hole), never the tap.
    assert r["outer_loop"] != ["tap"], (
        "tap-mark circle outranked the real outline — H8 weighting failed"
    )


def test_solo_tap_circle_does_not_reach_success() -> None:
    """Strip down to JUST a tap-mark circle in an inflated modelspace —
    by also adding a far-away helper LINE the bbox grows to swamp the
    circle. Without the area-ratio guard the circle would auto-confirm."""

    items = [
        ("tap", "CIRCLE", "other", {"cx": 0.0, "cy": 0.0, "r": 2.0}),
        # Helper line at the far corner to inflate the modelspace bbox to
        # ~1000 × 1000 mm — area_ratio (~π·4 / 1e6) ≈ 1.2e-5.
        (
            "lline", "LINE", "other",
            {"x1": 1000.0, "y1": 1000.0, "x2": 1001.0, "y2": 1001.0},
        ),
    ]
    r = detect_outer(items)
    assert r["status"] != "success", (
        f"solo tap-mark circle reached status={r['status']!r} despite "
        f"area_ratio ≈ 0"
    )
