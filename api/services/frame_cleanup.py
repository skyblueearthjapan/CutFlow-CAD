"""Drawing-frame detection for the cleanup endpoint (Phase 3).

The existing classifier already routes the largest dominant LWPOLYLINE
into the ``FRAME`` bucket (see ``services.classifier`` /
``services.outer_detector.detect_frame_polyline``); this module wraps
that detection with the additional pieces the cleanup workflow needs:

* the IDs from the classifier's ``FRAME`` bucket
* any 4-vertex ISO-aspect rectangle that visually wraps the outer loop
  (split-view drawings often have two frames)
* an optional pass for the title-block INSERTs the classifier flagged as
  frame inserts

Routing
-------

The cleanup router reads these IDs and merges them into the per-file
delete reservation. We never call ``store`` from here — the router
controls persistence so the function stays trivially testable.
"""

from __future__ import annotations

import logging
from typing import Any

from models.schemas import FileEntities

log = logging.getLogger(__name__)


def detect_frame_entities(
    payload: FileEntities,
    outer_loop: list[str] | None = None,
) -> list[str]:
    """Return the list of entity-ids that should be removed as the
    "drawing frame + title block".

    Inputs
    ------
    ``payload`` is the FileEntities result from ``parse_file``. The
    classifier has already populated ``payload.delete_candidates.FRAME``
    with the best single-frame guess.

    ``outer_loop`` (optional) is the confirmed outer loop. When supplied,
    no entity from the loop can ever be classified as a frame (defence
    in depth — the classifier already does this, but a stale state
    refresh might bypass it).

    Output
    ------
    A deduplicated list of entity-ids, in the order they first appear
    in ``payload.entities``.
    """

    outer_set: set[str] = set(outer_loop or [])
    candidates: set[str] = set()

    # 1) Whatever the classifier flagged for the FRAME bucket.
    for eid in payload.delete_candidates.FRAME:
        if eid not in outer_set:
            candidates.add(eid)

    # 2) Anything else carrying the ``frame`` category (title-block INSERTs).
    for e in payload.entities:
        if e.category == "frame" and e.id not in outer_set:
            candidates.add(e.id)

    # 3) M3: ALWAYS scan for additional axis-aligned ISO-aspect rectangles
    #    enclosing many other entities. Split-view drawings have two frames
    #    — the classifier picks one, and short-circuiting here misses the
    #    second. ``set.update()`` dedupes if the rectangle already landed
    #    via the classifier path.
    extra = _find_aspect_rectangles(payload, outer_set)
    candidates.update(extra)

    # Preserve original-listing order so the router's response is stable.
    ordered: list[str] = []
    seen: set[str] = set()
    for e in payload.entities:
        if e.id in candidates and e.id not in seen:
            ordered.append(e.id)
            seen.add(e.id)
    return ordered


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FRAME_MIN_INNER = 25


def _find_aspect_rectangles(
    payload: FileEntities, outer_set: set[str]
) -> list[str]:
    """Scan for 4-vertex axis-aligned ISO-aspect rectangles enclosing many
    other entities. Mirrors the heuristic in
    ``services.outer_detector._looks_like_drawing_frame``.
    """

    points_by_id = {e.id: _repr_point(e.type, e.geom) for e in payload.entities}

    out: list[str] = []
    for e in payload.entities:
        if e.id in outer_set:
            continue
        if e.type != "LWPOLYLINE":
            continue
        if not e.geom.get("closed"):
            continue
        verts = e.geom.get("vertices") or []
        if len(verts) != 4:
            continue

        xs = [float(v[0]) for v in verts]
        ys = [float(v[1]) for v in verts]
        pairs = [(0, 1), (1, 2), (2, 3), (3, 0)]
        aligned = sum(
            1 for a, b in pairs
            if abs(xs[a] - xs[b]) < 1e-6 or abs(ys[a] - ys[b]) < 1e-6
        )
        if aligned < 4:
            continue
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        if w <= 0 or h <= 0:
            continue
        aspect = max(w / h, h / w)
        if not (1.38 < aspect < 1.45):
            continue

        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        inside = 0
        for eid, pt in points_by_id.items():
            if eid == e.id or pt is None:
                continue
            x, y = pt
            if xmin < x < xmax and ymin < y < ymax:
                inside += 1
                if inside >= _FRAME_MIN_INNER:
                    break
        if inside >= _FRAME_MIN_INNER:
            out.append(e.id)
    return out


def _repr_point(t: str, geom: dict[str, Any]) -> tuple[float, float] | None:
    """Cheap "where is this entity" point for inside-rect checks."""

    try:
        if t == "LINE":
            return (
                (float(geom["x1"]) + float(geom["x2"])) / 2.0,
                (float(geom["y1"]) + float(geom["y2"])) / 2.0,
            )
        if t in ("CIRCLE", "ARC"):
            return (float(geom["cx"]), float(geom["cy"]))
        if t in ("LWPOLYLINE", "POLYLINE"):
            verts = geom.get("vertices") or []
            if not verts:
                return None
            return (
                sum(float(v[0]) for v in verts) / len(verts),
                sum(float(v[1]) for v in verts) / len(verts),
            )
        if t in ("TEXT", "MTEXT", "INSERT", "POINT"):
            return (float(geom.get("x", 0.0)), float(geom.get("y", 0.0)))
        if t == "DIMENSION":
            anchors = geom.get("anchors") or []
            if anchors:
                return (float(anchors[0][0]), float(anchors[0][1]))
    except (KeyError, ValueError, TypeError):
        return None
    return None
