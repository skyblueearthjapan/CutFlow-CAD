"""Outer-shape detection.

Phase 1 keeps things minimal: we only flag the **drawing frame**
(largest closed LWPOLYLINE that dominates the modelspace) so the
classifier can route it to the FRAME delete bucket. True topology-based
outer-loop reconstruction is deferred to Phase 2.

A part outline that *happens* to be a closed 4-vertex rectangle (think
"鋼板を矩形にカット") must not be flagged as a frame — otherwise the
"図枠削除" button would wipe the actual part. To guard against this we
require the candidate to satisfy ALL of:

* bbox dominates the modelspace extent (≥80% of the bbox area), AND
* there are several other geometry entities nested inside it (a real
  drawing frame contains the part it surrounds).
"""

from __future__ import annotations

from typing import Any, Iterable

from ezdxf.entities import DXFEntity

# Minimum dominance ratio for the "this is the page frame" heuristic.
# Tightened to 0.80 (was 0.35): the frame should be by far the biggest box,
# otherwise the part outline itself would qualify on small drawings.
_FRAME_DOMINANCE = 0.80

# Frames enclose the part itself, so we expect a non-trivial number of other
# entities to be physically *inside* the candidate rectangle.
_FRAME_MIN_INNER_ENTITIES = 8


def detect_frame_polyline(
    items: Iterable[tuple[str, DXFEntity, dict[str, Any]]],
) -> str | None:
    """Return the entity-id of the most likely drawing frame, or ``None``.

    The frame is heuristically defined as a closed LWPOLYLINE with 4 vertices
    (a rectangle) that (a) dominates the modelspace bbox and (b) has many
    other entities physically nested inside it (i.e. it surrounds the part).
    """

    items_list = list(items)

    polys: list[tuple[str, list[list[float]]]] = []
    for eid, ent, geom in items_list:
        if ent.dxftype() != "LWPOLYLINE":
            continue
        if not geom.get("closed"):
            continue
        verts = geom.get("vertices") or []
        if len(verts) != 4:
            continue
        polys.append((eid, verts))

    if not polys:
        return None

    sized = [(eid, _area_of(v), _bbox_of(v)) for eid, v in polys]
    sized.sort(key=lambda r: r[1], reverse=True)
    biggest_id, biggest_area, biggest_bbox = sized[0]

    # Require dominance — otherwise it might just be the largest hole.
    union_w, union_h = _modelspace_extent(items_list)
    if union_w <= 0 or union_h <= 0:
        return None
    if biggest_area < _FRAME_DOMINANCE * (union_w * union_h):
        return None

    # Filter false positives: require near-rectangular aspect (between 1:5 and 5:1).
    w = biggest_bbox[2] - biggest_bbox[0]
    h = biggest_bbox[3] - biggest_bbox[1]
    if h <= 0 or w <= 0:
        return None
    aspect = max(w / h, h / w)
    if aspect > 6.0:
        return None

    # The clincher: a true drawing frame surrounds the part. Count entities
    # whose representative point falls inside the candidate's bbox.
    inner = _count_inside(items_list, biggest_id, biggest_bbox)
    if inner < _FRAME_MIN_INNER_ENTITIES:
        return None

    return biggest_id


def _count_inside(
    items: Iterable[tuple[str, DXFEntity, dict[str, Any]]],
    skip_id: str,
    bbox: tuple[float, float, float, float],
) -> int:
    """Count entities whose representative point lies strictly inside ``bbox``."""

    xmin, ymin, xmax, ymax = bbox
    count = 0
    for eid, ent, geom in items:
        if eid == skip_id:
            continue
        pt = _representative_point(ent.dxftype(), geom)
        if pt is None:
            continue
        x, y = pt
        if xmin < x < xmax and ymin < y < ymax:
            count += 1
    return count


def _representative_point(
    dxftype: str, geom: dict[str, Any]
) -> tuple[float, float] | None:
    """A cheap "centre" for the entity, used by ``_count_inside``."""

    if dxftype == "LINE":
        x1 = geom.get("x1")
        y1 = geom.get("y1")
        x2 = geom.get("x2")
        y2 = geom.get("y2")
        if None in (x1, y1, x2, y2):
            return None
        return ((float(x1) + float(x2)) / 2.0, (float(y1) + float(y2)) / 2.0)
    if dxftype in ("CIRCLE", "ARC"):
        cx = geom.get("cx")
        cy = geom.get("cy")
        if cx is None or cy is None:
            return None
        return (float(cx), float(cy))
    if dxftype in ("LWPOLYLINE", "POLYLINE"):
        verts = geom.get("vertices") or []
        if not verts:
            return None
        # Use centroid of vertices (sufficient for inside-bbox test).
        sx = sum(float(v[0]) for v in verts)
        sy = sum(float(v[1]) for v in verts)
        n = len(verts)
        return (sx / n, sy / n)
    if dxftype in ("TEXT", "MTEXT", "INSERT", "POINT"):
        x = geom.get("x")
        y = geom.get("y")
        if x is None or y is None:
            return None
        return (float(x), float(y))
    if dxftype == "DIMENSION":
        anchors = geom.get("anchors") or []
        if not anchors:
            return None
        return (float(anchors[0][0]), float(anchors[0][1]))
    return None


# ---------------------------------------------------------------------------
# Geometry helpers (kept here so the parser stays lean)
# ---------------------------------------------------------------------------


def _area_of(verts: list[list[float]]) -> float:
    # Vertices may be [x, y] or [x, y, bulge] (LWPOLYLINE retains bulge after
    # the parser change), so index explicitly instead of tuple-unpacking.
    if len(verts) < 3:
        return 0.0
    s = 0.0
    n = len(verts)
    for i in range(n):
        x1, y1 = verts[i][0], verts[i][1]
        x2, y2 = verts[(i + 1) % n][0], verts[(i + 1) % n][1]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _bbox_of(verts: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    return min(xs), min(ys), max(xs), max(ys)


def _modelspace_extent(
    items: Iterable[tuple[str, DXFEntity, dict[str, Any]]],
) -> tuple[float, float]:
    """Approximate (width, height) over LINE/LWPOLYLINE/CIRCLE/ARC geom dicts."""

    xs: list[float] = []
    ys: list[float] = []
    for _eid, ent, geom in items:
        t = ent.dxftype()
        if t == "LINE":
            xs.extend([geom.get("x1", 0.0), geom.get("x2", 0.0)])
            ys.extend([geom.get("y1", 0.0), geom.get("y2", 0.0)])
        elif t in ("LWPOLYLINE", "POLYLINE"):
            for v in geom.get("vertices") or []:
                xs.append(v[0])
                ys.append(v[1])
        elif t in ("CIRCLE", "ARC"):
            cx = geom.get("cx", 0.0)
            cy = geom.get("cy", 0.0)
            r = geom.get("r", 0.0)
            xs.extend([cx - r, cx + r])
            ys.extend([cy - r, cy + r])
    if not xs or not ys:
        return 0.0, 0.0
    return max(xs) - min(xs), max(ys) - min(ys)
