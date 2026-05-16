"""Split-view frame detection: M3 — when the classifier already labelled
one frame, the heuristic must still scan for additional ISO-aspect
rectangles in the same file (split-view drawings have two)."""

from __future__ import annotations

from models.schemas import (
    BoundingBox,
    DeleteCandidates,
    EntityOut,
    FileEntities,
    Stats,
)
from services.frame_cleanup import detect_frame_entities


def _iso_rect(eid: str, xmin: float, ymin: float, w: float, h: float) -> EntityOut:
    """Build a closed 4-vertex LWPOLYLINE that satisfies the
    ``_find_aspect_rectangles`` ISO-aspect guard (≈ √2)."""

    verts = [
        [xmin, ymin],
        [xmin + w, ymin],
        [xmin + w, ymin + h],
        [xmin, ymin + h],
    ]
    return EntityOut(
        id=eid,
        type="LWPOLYLINE",
        category="other",
        layer="0",
        color=256,
        geom={"vertices": verts, "closed": True},
    )


def _point_line(eid: str, x: float, y: float) -> EntityOut:
    """Small LINE used to populate the "inside the frame" entity count."""

    return EntityOut(
        id=eid,
        type="LINE",
        category="other",
        layer="0",
        color=256,
        geom={"x1": x, "y1": y, "x2": x + 1.0, "y2": y + 1.0},
    )


def test_detect_frame_entities_returns_both_split_view_frames() -> None:
    """A split-view drawing has TWO ISO-aspect rectangles, each enclosing
    >25 entities. The classifier may have picked only one (the dominant
    LWPOLYLINE). After M3, the heuristic must surface the second too.
    """

    # Two ISO-aspect frames (A-aspect ≈ 1.414): one on the left, one on
    # the right of the sheet. Each contains 30 dummy lines so they both
    # clear the _FRAME_MIN_INNER=25 gate.
    W = 420.0
    H = 297.0
    left_frame = _iso_rect("frame_left", 0.0, 0.0, W, H)
    right_frame = _iso_rect("frame_right", W + 20.0, 0.0, W, H)

    inside_left: list[EntityOut] = [
        _point_line(f"L{i:03d}", 10.0 + i * 2.0, 50.0) for i in range(30)
    ]
    inside_right: list[EntityOut] = [
        _point_line(f"R{i:03d}", W + 30.0 + i * 2.0, 50.0) for i in range(30)
    ]

    payload = FileEntities(
        file_id="fid",
        name="split.dxf",
        bounding_box=BoundingBox(min_x=0, min_y=0, max_x=2 * W + 20, max_y=H),
        entities=[left_frame, right_frame, *inside_left, *inside_right],
        # Classifier picked only the left frame (the dominant one).
        delete_candidates=DeleteCandidates(FRAME=["frame_left"]),
        stats=Stats(
            total=2 + 60,
            by_category={"frame": 1, "other": 1 + 60},
        ),
        units="mm",
    )

    out = detect_frame_entities(payload)
    # Both frames must be present — the second one came from the
    # unconditional split-view scan added by M3.
    assert "frame_left" in out
    assert "frame_right" in out
    # Order follows the original entity listing for stability.
    assert out.index("frame_left") < out.index("frame_right")
