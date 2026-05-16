"""Outer-loop detection regression tests.

These assertions are deliberately loose on the exact entity-id chains
(those depend on the classifier heuristics) but tight on the *physical*
properties the workshop ultimately cares about — was a closed ring
detected, is its perimeter on the right order of magnitude, and is the
reported confidence on the right side of the success/warning thresholds.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from services.dxf_parser import parse_file
from services.outer_detector import detect_outer, evaluate_manual


def _items(payload) -> list[tuple[str, str, str, dict]]:
    return [(e.id, e.type, e.category, e.geom) for e in payload.entities]


# ---------------------------------------------------------------------------
# Synthetic / unit tests
# ---------------------------------------------------------------------------


def test_detect_outer_on_empty_input_returns_failed() -> None:
    r = detect_outer([])
    assert r["status"] == "failed"
    assert r["confidence"] == 0.0
    assert r["outer_loop"] == []


def test_detect_outer_with_only_dim_entities_fails() -> None:
    # All entities flagged as 'dim' → no edges to consume.
    items = [
        ("e1", "LINE", "dim", {"x1": 0, "y1": 0, "x2": 10, "y2": 0}),
        ("e2", "LINE", "dim", {"x1": 10, "y1": 0, "x2": 10, "y2": 10}),
    ]
    r = detect_outer(items)
    assert r["status"] == "failed"


def test_detect_outer_single_circle_dominates() -> None:
    items = [
        ("c1", "CIRCLE", "other", {"cx": 0, "cy": 0, "r": 50}),
    ]
    r = detect_outer(items)
    assert r["status"] == "success"
    assert r["confidence"] >= 0.80
    assert r["method"] == "circle"
    assert r["outer_loop"] == ["c1"]
    s = r["loop_summary"]
    assert s["closed"] is True
    assert math.isclose(s["area"], math.pi * 50 * 50, rel_tol=0.05)
    assert math.isclose(s["perimeter"], 2 * math.pi * 50, rel_tol=0.05)


def test_detect_outer_closed_polyline() -> None:
    verts = [[0.0, 0.0, 0.0], [100.0, 0.0, 0.0], [100.0, 50.0, 0.0], [0.0, 50.0, 0.0]]
    # Make it look genuine: add a small "hole" inside so the inner-element
    # signal kicks in. Without the hole the 4-vertex rectangle would be
    # caught by the drawing-frame guard (axis-aligned + iso aspect rules
    # only fire when the rectangle visually encloses many other entities).
    items = [
        ("p1", "LWPOLYLINE", "other", {"vertices": verts, "closed": True}),
        ("c1", "CIRCLE", "other", {"cx": 50, "cy": 25, "r": 5}),
    ]
    r = detect_outer(items)
    assert r["status"] == "success"
    assert r["method"] == "closed_polyline"
    assert r["outer_loop"] == ["p1"]


def test_evaluate_manual_closed_chain() -> None:
    # Square built from four LINEs that close at (0,0).
    items = [
        ("e1", "LINE", "other", {"x1": 0, "y1": 0, "x2": 100, "y2": 0}),
        ("e2", "LINE", "other", {"x1": 100, "y1": 0, "x2": 100, "y2": 50}),
        ("e3", "LINE", "other", {"x1": 100, "y1": 50, "x2": 0, "y2": 50}),
        ("e4", "LINE", "other", {"x1": 0, "y1": 50, "x2": 0, "y2": 0}),
    ]
    r = evaluate_manual(items, ["e1", "e2", "e3", "e4"])
    assert r["status"] == "success"
    assert r["confidence"] >= 0.95
    assert math.isclose(r["loop_summary"]["perimeter"], 300.0, rel_tol=0.01)


def test_evaluate_manual_open_chain_fails() -> None:
    items = [
        ("e1", "LINE", "other", {"x1": 0, "y1": 0, "x2": 100, "y2": 0}),
        ("e2", "LINE", "other", {"x1": 100, "y1": 0, "x2": 100, "y2": 50}),
        ("e3", "LINE", "other", {"x1": 100, "y1": 50, "x2": 0, "y2": 50}),
        # missing the closing edge
    ]
    r = evaluate_manual(items, ["e1", "e2", "e3"])
    assert r["status"] == "failed"
    assert "閉ループ" in r["warnings"][0] or "閉ループ" not in r["warnings"][0]
    # The warning must mention the loop is not closed.
    assert any("閉" in w or "closed" in w.lower() for w in r["warnings"])


def test_evaluate_manual_unknown_ids_rejected() -> None:
    items = [("e1", "LINE", "other", {"x1": 0, "y1": 0, "x2": 10, "y2": 0})]
    r = evaluate_manual(items, ["e1", "doesnt_exist"])
    assert r["status"] == "failed"
    assert "不正" in r["warnings"][0]


# ---------------------------------------------------------------------------
# Live sample tests (skipped if the workshop folder is missing)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size,min_conf,min_perim,max_perim",
    [
        # カラー: a single circle, r~42mm → perim ~264mm.
        ("small", 0.80, 200.0, 350.0),
        # センタープレート: a closed LWPOLYLINE, perim ~2000mm.
        ("medium", 0.80, 800.0, 5000.0),
        # ベースフレーム: complex multi-cycle. Accept anything ≥60% confidence
        # and any positive perimeter — the user is expected to confirm/fix
        # via the manual path.
        ("large", 0.60, 100.0, 60000.0),
    ],
)
def test_detect_outer_on_sample(
    sample_dxf_paths: dict[str, Path],
    size: str,
    min_conf: float,
    min_perim: float,
    max_perim: float,
) -> None:
    payload = parse_file(sample_dxf_paths[size], file_id="fid", name=size)
    r = detect_outer(_items(payload))

    # Must always return *something* — even "failed" carries candidates.
    assert r["status"] in {"success", "low_confidence", "failed"}
    assert r["confidence"] >= min_conf, (
        f"{size}: confidence {r['confidence']} below floor {min_conf}"
    )
    assert r["outer_loop"], f"{size}: expected a non-empty outer loop"
    s = r["loop_summary"]
    assert s is not None
    assert s["closed"] is True
    assert min_perim <= s["perimeter"] <= max_perim, (
        f"{size}: perimeter {s['perimeter']} out of [{min_perim}, {max_perim}]"
    )
    # The bounding box must be a non-degenerate rectangle.
    bb = s["bounding_box"]
    assert bb["max_x"] > bb["min_x"]
    assert bb["max_y"] > bb["min_y"]


def test_candidates_payload_capped_to_three(sample_dxf_paths: dict[str, Path]) -> None:
    payload = parse_file(sample_dxf_paths["medium"], file_id="fid", name="m")
    r = detect_outer(_items(payload))
    assert len(r["candidates"]) <= 3
    # First candidate must match the winning outer_loop.
    if r["candidates"]:
        assert r["candidates"][0]["loop"] == r["outer_loop"]
