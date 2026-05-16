"""H7 regression: ``detect_outer(items, delete_ids=...)`` excludes the
operator's delete reservations from the candidate pool.
"""

from __future__ import annotations

from services.outer_detector import detect_outer


def _square_items(big_radius: float = 50.0):
    """A dominant CIRCLE plus a smaller CIRCLE the operator deleted."""

    return [
        ("c_outer", "CIRCLE", "other", {"cx": 0.0, "cy": 0.0, "r": big_radius}),
        ("c_inner", "CIRCLE", "other", {"cx": 0.0, "cy": 0.0, "r": 5.0}),
    ]


def test_detect_skips_deleted_entities() -> None:
    items = _square_items()
    r = detect_outer(items, delete_ids=["c_outer"])
    # The big circle was deleted; only the small one remains as a candidate.
    assert "c_outer" not in r["outer_loop"]
    if r["outer_loop"]:
        assert r["outer_loop"] == ["c_inner"]


def test_detect_with_no_deleted_picks_largest() -> None:
    items = _square_items()
    r = detect_outer(items)
    # No deletion → biggest circle wins.
    assert r["outer_loop"] == ["c_outer"]


def test_detect_empty_after_delete_returns_failed() -> None:
    """Deleting every edge entity must not crash; it returns ``failed``."""

    items = [("c1", "CIRCLE", "other", {"cx": 0.0, "cy": 0.0, "r": 30.0})]
    r = detect_outer(items, delete_ids=["c1"])
    assert r["status"] == "failed"
    assert r["outer_loop"] == []
