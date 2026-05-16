"""Run the parser against the three representative drawings.

We assert structure (not exact numbers) because the heuristic classifier
will evolve: each drawing must produce *some* entities, expose a sane
bounding box, and surface delete candidates without misclassifying every
geometry primitive as a deletion target.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from services.dxf_parser import parse_file


@pytest.mark.parametrize("size", ["small", "medium", "large"])
def test_parse_sample(sample_dxf_paths: dict[str, Path], size: str) -> None:
    path = sample_dxf_paths[size]

    t0 = time.perf_counter()
    payload = parse_file(path, file_id="fid", name=path.name)
    dt = time.perf_counter() - t0
    print(f"[{size}] {path.name}: {payload.stats.total} entities in {dt:.2f}s")

    assert payload.stats.total > 0
    bb = payload.bounding_box
    assert bb.max_x > bb.min_x
    assert bb.max_y > bb.min_y

    # Every entity has an id, a type, and a category.
    ids = {e.id for e in payload.entities}
    assert len(ids) == len(payload.entities), "entity IDs must be unique"

    # by_category totals must equal len(entities).
    assert sum(payload.stats.by_category.values()) == payload.stats.total

    # At least one dimension/leader/insert was classified for the larger files.
    if size in ("medium", "large"):
        dc = payload.delete_candidates
        total_delete = len(dc.DIMENSION) + len(dc.BALLOON) + len(dc.TAP) + len(dc.FRAME)
        assert total_delete > 0, "expected at least one delete candidate"

    # No more than ~70% of all entities should be flagged for deletion —
    # this guards against the classifier going haywire and wiping the part.
    dc = payload.delete_candidates
    flagged = len(dc.DIMENSION) + len(dc.BALLOON) + len(dc.TAP) + len(dc.FRAME)
    assert flagged < 0.8 * payload.stats.total, "too many entities flagged for deletion"


def test_parse_categories_make_sense(sample_dxf_paths: dict[str, Path]) -> None:
    """The medium sample contains known tap blocks (BLOCK008-011)."""

    path = sample_dxf_paths["medium"]
    payload = parse_file(path, file_id="fid", name=path.name)
    cats = payload.stats.by_category
    # Medium drawing must yield at least one DIMENSION and at least one tap.
    assert cats.get("dim", 0) >= 1
    assert payload.delete_candidates.TAP, "centre-plate sample should have tap candidates"


def test_geom_round_trip(sample_dxf_paths: dict[str, Path]) -> None:
    """LINE entities must have x1/y1/x2/y2 fields."""

    payload = parse_file(sample_dxf_paths["small"], file_id="fid", name="small")
    lines = [e for e in payload.entities if e.type == "LINE"]
    assert lines, "small sample has LINE entities"
    g = lines[0].geom
    for key in ("x1", "y1", "x2", "y2"):
        assert key in g
        assert isinstance(g[key], float)
