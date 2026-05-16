"""Outer-shape detection (Phase 2).

Implements the STEP 1–5 pipeline described in ``docs/DESIGN.md`` §6:

* STEP 1 — collect edge-like entities (LINE / ARC / LWPOLYLINE / POLYLINE /
  CIRCLE), excluding things the classifier already flagged for deletion
  (dimensions, balloons, taps, drawing frame).
* STEP 2 — snap endpoints inside ``DEFAULT_TOL_MM`` and build an undirected
  graph (see ``services.graph``).
* STEP 3 — try four strategies in parallel and keep the best:
    A) lone CIRCLE that dominates the modelspace (typical for ``カラー``).
    B) the largest closed LWPOLYLINE (typical for clean panel cut-outs).
    C) graph cycle enumeration → largest-area closed cycle.
    D) convex hull of all endpoint samples (always available; weak prior).
* STEP 4 — score each candidate on completeness, area ratio and number of
  inner entities, then collapse to a single confidence in [0..1].
* STEP 5 — pick the winner; surface the runners-up so the UI can offer them.

The legacy ``detect_frame_polyline`` helper is preserved unchanged so the
classifier continues to identify drawing frames.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable

from ezdxf.entities import DXFEntity

from services import graph as gmod

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Legacy: frame detection (used by classifier.py)
# ---------------------------------------------------------------------------

_FRAME_DOMINANCE = 0.80
_FRAME_MIN_INNER_ENTITIES = 8


def detect_frame_polyline(
    items: Iterable[tuple[str, DXFEntity, dict[str, Any]]],
) -> str | None:
    """Return the entity-id of the most likely drawing frame, or ``None``.

    Identical behaviour to Phase 1: a closed 4-vertex LWPOLYLINE that
    dominates the modelspace AND visually encloses many other entities.
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

    union_w, union_h = _modelspace_extent(items_list)
    if union_w <= 0 or union_h <= 0:
        return None
    if biggest_area < _FRAME_DOMINANCE * (union_w * union_h):
        return None

    w = biggest_bbox[2] - biggest_bbox[0]
    h = biggest_bbox[3] - biggest_bbox[1]
    if h <= 0 or w <= 0:
        return None
    aspect = max(w / h, h / w)
    if aspect > 6.0:
        return None

    inner = _count_inside(items_list, biggest_id, biggest_bbox)
    if inner < _FRAME_MIN_INNER_ENTITIES:
        return None
    return biggest_id


# ---------------------------------------------------------------------------
# Phase 2: full outer-loop detection
# ---------------------------------------------------------------------------


# Categories the classifier already routed to delete — those entities can
# never be part of the outer profile.
_EXCLUDED_CATEGORIES = {"dim", "balloon", "tap", "frame"}
# Geometric primitives we know how to trace.
_EDGE_TYPES = {"LINE", "ARC", "LWPOLYLINE", "POLYLINE", "CIRCLE"}


def detect_outer(
    items: Iterable[tuple[str, str, str, dict[str, Any]]],
    delete_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Run the STEP 1–5 pipeline.

    ``items`` is an iterable of ``(entity_id, dxftype, category, geom)``.
    ``delete_ids`` (H7) is an optional set of entity ids the user has
    reserved for deletion — they are excluded from the candidate pool so
    the detector never picks a line the operator has already retired.
    Returns a dict with the following shape (matches ``OuterDetectionResult``):

    .. code-block:: python

        {
            "status": "success" | "low_confidence" | "failed",
            "confidence": 0.0..1.0,
            "method": str,
            "outer_loop": [entity_id, ...],
            "loop_summary": {...} | None,
            "warnings": [str, ...],
            "candidates": [{loop, confidence, area, method}, ...],
        }
    """

    items_list = [
        (eid, t, c or "other", g or {}) for eid, t, c, g in items
    ]
    deleted_set: set[str] = set(delete_ids or ())

    # STEP 1: filter to candidate edge entities.
    edge_items: list[tuple[str, str, dict[str, Any]]] = []
    for eid, t, cat, geom in items_list:
        if eid in deleted_set:
            continue
        if cat in _EXCLUDED_CATEGORIES:
            continue
        if t not in _EDGE_TYPES:
            continue
        edge_items.append((eid, t, geom))

    if not edge_items:
        return _empty_result("no edge entities to process")

    # STEP 2: graph.
    topo = gmod.build_graph(edge_items)

    # STEP 3: collect candidates from each strategy.
    raw_candidates: list[_Candidate] = []
    raw_candidates.extend(_strategy_a_circle(edge_items, topo))
    raw_candidates.extend(_strategy_b_closed_polyline(edge_items, topo))
    raw_candidates.extend(_strategy_c_graph_cycles(topo))
    raw_candidates.extend(_strategy_d_convex_hull(topo))

    if not raw_candidates:
        return _empty_result("no closed loop could be reconstructed")

    # STEP 4: scoring (compute confidence per candidate).
    msp_w, msp_h = _topo_extent(topo)
    msp_bbox_area = msp_w * msp_h if (msp_w > 0 and msp_h > 0) else 0.0
    inner_counters = _build_inner_counter(items_list, topo)

    scored: list[_Candidate] = []
    for cand in raw_candidates:
        c = _score_candidate(cand, msp_bbox_area, inner_counters)
        cand.confidence = c
        scored.append(cand)

    # Deduplicate (same set of edge_ids → keep the best confidence).
    by_sig: dict[frozenset[str], _Candidate] = {}
    for c in scored:
        sig = frozenset(c.loop)
        if sig not in by_sig or c.confidence > by_sig[sig].confidence:
            by_sig[sig] = c
    scored = list(by_sig.values())
    scored.sort(key=lambda c: (c.confidence, c.area), reverse=True)

    # Selection heuristic: among candidates within 0.10 of the top confidence,
    # pick the one with the largest area. This protects against the case
    # where multiple loops score the same (e.g. tap-mark cycles tied with the
    # real part outline) — the real part is always the bigger of the two.
    # H9: a candidate that *looks* like a drawing frame is heavily penalised
    # in the tie-break so it cannot beat a non-frame on raw area alone.
    top_conf = scored[0].confidence
    near_top = [c for c in scored if c.confidence >= top_conf - 0.10]

    def _tie_score(c: _Candidate) -> tuple[int, float]:
        # Primary key: candidates with a real entity chain always beat the
        # empty-loop convex-hull fallback in the tie-break. Without this
        # the convex hull of every endpoint tends to dwarf the actual part
        # outline on area alone and win, leaving downstream offset with an
        # empty loop.
        has_loop = 1 if c.loop else 0
        s = c.area
        if _candidate_looks_like_drawing_frame(c):
            s *= 0.5
        return (has_loop, s)

    near_top.sort(key=_tie_score, reverse=True)
    best = near_top[0]

    if best.confidence >= 0.80:
        status = "success"
    elif best.confidence >= 0.50:
        status = "low_confidence"
    else:
        status = "failed"

    warnings: list[str] = []
    if not best.closed:
        warnings.append("閉ループ未確認の箇所が1ヶ所あります")
    if best.confidence < 0.80:
        warnings.append(f"信頼度 {best.confidence * 100:.0f}% — 確認をお願いします")

    summary = _loop_summary(best, topo)

    # Make the chosen winner the first candidate so the UI's "primary" pill
    # and "alternatives" list stay consistent with the selection logic above.
    ordered = [best] + [c for c in scored if c is not best]
    cand_payload = [
        {
            "loop": c.loop,
            "confidence": round(c.confidence, 3),
            "area": round(c.area, 3),
            "method": c.method,
        }
        for c in ordered[:3]
    ]

    return {
        "status": status,
        "confidence": round(best.confidence, 3),
        "method": best.method,
        "outer_loop": best.loop,
        "loop_summary": summary,
        "warnings": warnings,
        "candidates": cand_payload,
    }


def evaluate_manual(
    items: Iterable[tuple[str, str, str, dict[str, Any]]],
    entity_ids: list[str],
) -> dict[str, Any]:
    """Validate a user-supplied entity chain claims to form a closed loop.

    Returns the same dict shape as :func:`detect_outer`. ``status`` is
    ``success`` iff the chain stitches into a polygon that closes within
    ``DEFAULT_TOL_MM``; otherwise ``failed`` with a warning.
    """

    items_list = [
        (eid, t, c or "other", g or {}) for eid, t, c, g in items
    ]
    by_id = {eid: (t, g) for eid, t, _c, g in items_list}

    edge_items: list[tuple[str, str, dict[str, Any]]] = []
    missing: list[str] = []
    for eid in entity_ids:
        rec = by_id.get(eid)
        if rec is None:
            missing.append(eid)
            continue
        t, geom = rec
        if t not in _EDGE_TYPES:
            missing.append(eid)
            continue
        edge_items.append((eid, t, geom))

    if missing:
        return {
            "status": "failed",
            "confidence": 0.0,
            "method": "manual",
            "outer_loop": [],
            "loop_summary": None,
            "warnings": [f"不正なエンティティID: {', '.join(missing[:5])}"],
            "candidates": [],
        }

    topo = gmod.build_graph(edge_items)
    pts = gmod.polygon_from_loop(topo, [eid for eid, _t, _g in edge_items])
    if len(pts) < 3:
        return {
            "status": "failed",
            "confidence": 0.0,
            "method": "manual",
            "outer_loop": [],
            "loop_summary": None,
            "warnings": ["ループを構築できませんでした"],
            "candidates": [],
        }

    closed = _is_closed_chain(topo, [eid for eid, _t, _g in edge_items])

    cand = _Candidate(
        loop=[eid for eid, _t, _g in edge_items],
        method="manual",
        points=pts,
        closed=closed,
    )
    cand.area = gmod.polygon_area(pts)
    cand.perimeter = gmod.polygon_perimeter(pts)

    # Manual confidence boost: trust the operator unless the ring is open.
    cand.confidence = 1.0 if closed else 0.4

    summary = _loop_summary(cand, topo)
    warnings: list[str] = []
    status = "success"
    if not closed:
        status = "failed"
        warnings.append("選択されたエンティティが閉ループを構成していません")

    return {
        "status": status,
        "confidence": cand.confidence,
        "method": "manual",
        "outer_loop": cand.loop,
        "loop_summary": summary,
        "warnings": warnings,
        "candidates": [],
    }


# ---------------------------------------------------------------------------
# Candidate plumbing
# ---------------------------------------------------------------------------


class _Candidate:
    __slots__ = ("loop", "method", "points", "closed", "area", "perimeter", "confidence", "_lines", "_arcs")

    def __init__(
        self,
        loop: list[str],
        method: str,
        points: list[tuple[float, float]],
        closed: bool,
    ) -> None:
        self.loop = loop
        self.method = method
        self.points = points
        self.closed = closed
        self.area = 0.0
        self.perimeter = 0.0
        self.confidence = 0.0
        self._lines = 0
        self._arcs = 0


def _candidate_looks_like_drawing_frame(cand: "_Candidate") -> bool:
    """Tie-break helper (H9): true when the candidate's polygon is an
    axis-aligned ISO-aspect rectangle (the classic A1/A2 frame shape).
    Distinct from ``_looks_like_drawing_frame`` (which needs the raw
    geom dict + sibling entities); this works off the candidate's
    sampled points alone.
    """

    pts = cand.points
    if len(pts) != 4:
        return False
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    pairs = [(0, 1), (1, 2), (2, 3), (3, 0)]
    aligned = 0
    for a, b in pairs:
        if abs(xs[a] - xs[b]) < 1e-6 or abs(ys[a] - ys[b]) < 1e-6:
            aligned += 1
    if aligned < 4:
        return False
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    if w <= 0 or h <= 0:
        return False
    aspect = max(w / h, h / w)
    return 1.38 < aspect < 1.45


def _strategy_a_circle(
    edge_items: list[tuple[str, str, dict[str, Any]]],
    topo: gmod.TopoGraph,
) -> list[_Candidate]:
    circles = [(eid, geom) for eid, t, geom in edge_items if t == "CIRCLE"]
    if not circles:
        return []

    # Pick the circle whose enclosed area dominates the bag of geometry.
    out: list[_Candidate] = []
    for eid, geom in circles:
        r = float(geom.get("r", 0.0))
        if r <= 0:
            continue
        area = math.pi * r * r
        info = topo.edges.get(eid)
        pts = info.samples if info else []
        cand = _Candidate(loop=[eid], method="circle", points=pts, closed=True)
        cand.area = area
        cand.perimeter = 2.0 * math.pi * r
        out.append(cand)

    # Keep the biggest only — multiple circles in one file usually means
    # holes, and the outer is by definition the largest.
    out.sort(key=lambda c: c.area, reverse=True)
    return out[:1]


def _strategy_b_closed_polyline(
    edge_items: list[tuple[str, str, dict[str, Any]]],
    topo: gmod.TopoGraph,
) -> list[_Candidate]:
    cands: list[_Candidate] = []
    for eid, t, geom in edge_items:
        if t not in ("LWPOLYLINE", "POLYLINE"):
            continue
        if not geom.get("closed"):
            continue
        info = topo.edges.get(eid)
        if info is None or len(info.samples) < 3:
            continue
        pts = info.samples
        if len(pts) >= 2 and pts[0] == pts[-1]:
            pts = pts[:-1]
        area = gmod.polygon_area(pts)
        if area <= 0:
            continue

        # Phase 2 guard: a 4-vertex axis-aligned rectangle that physically
        # contains many other entities is almost certainly a drawing-sheet
        # frame, even if it isn't the *largest* frame in the modelspace
        # (Japanese A1 sheets often appear twice for split-view drawings).
        if _looks_like_drawing_frame(geom, pts, edge_items):
            continue

        cand = _Candidate(loop=[eid], method="closed_polyline", points=pts, closed=True)
        cand.area = area
        cand.perimeter = gmod.polygon_perimeter(pts)
        cands.append(cand)

    cands.sort(key=lambda c: c.area, reverse=True)
    return cands[:3]


def _looks_like_drawing_frame(
    geom: dict[str, Any],
    pts: list[tuple[float, float]],
    edge_items: list[tuple[str, str, dict[str, Any]]],
) -> bool:
    """Heuristic: 4-vertex axis-aligned rectangle that wraps many other entities.

    Used to drop A1/A2 drawing frames from the outer-loop candidate pool
    even when the classifier didn't catch them (e.g. two frames side by
    side for split-view drawings).
    """

    verts = geom.get("vertices") or []
    if len(verts) != 4:
        return False
    xs = [float(v[0]) for v in verts]
    ys = [float(v[1]) for v in verts]
    # Axis-aligned ⇔ each pair of adjacent vertices shares an x or y.
    pairs = [(0, 1), (1, 2), (2, 3), (3, 0)]
    aligned = 0
    for a, b in pairs:
        if abs(xs[a] - xs[b]) < 1e-6 or abs(ys[a] - ys[b]) < 1e-6:
            aligned += 1
    if aligned < 4:
        return False

    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    if w <= 0 or h <= 0:
        return False
    aspect = max(w / h, h / w)
    # Real workshop parts are rarely exactly A-series ISO aspect (1.414).
    # A drawing sheet is almost always √2; everything else falls through.
    is_iso_aspect = 1.38 < aspect < 1.45
    if not is_iso_aspect:
        return False

    # Count entities whose midpoint sits inside the rectangle.
    xmin = min(xs); ymin = min(ys); xmax = max(xs); ymax = max(ys)
    inside = 0
    for _eid, t, g in edge_items:
        pt = _representative_point_from_geom(t, g)
        if pt is None:
            continue
        x, y = pt
        if xmin < x < xmax and ymin < y < ymax:
            inside += 1
            if inside >= 25:  # threshold met early — no need to scan further
                break
    return inside >= 25


def _strategy_c_graph_cycles(topo: gmod.TopoGraph) -> list[_Candidate]:
    loops = gmod.find_closed_loops(topo)
    cands: list[_Candidate] = []
    for loop in loops:
        pts = gmod.polygon_from_loop(topo, loop)
        if len(pts) < 3:
            continue
        area = gmod.polygon_area(pts)
        if area <= 0:
            continue
        # Geometric closure: the polygon assembled from the chain wraps
        # back on itself within tolerance. Falls back to the node-walk
        # check for safety, but the geometric test catches cases where
        # shapely's polygonize stitched a face together from open
        # polylines whose graph nodes don't align trivially (H4/H5).
        closed = _is_closed_chain(topo, loop) or _is_closed_geom(topo, loop)
        cand = _Candidate(loop=loop, method="graph", points=pts, closed=closed)
        cand.area = area
        cand.perimeter = gmod.polygon_perimeter(pts)
        cands.append(cand)

    cands.sort(key=lambda c: c.area, reverse=True)
    return cands[:5]


def _is_closed_geom(
    topo: gmod.TopoGraph, loop: list[str]
) -> bool:
    """Geometric closure check: stitch the chain end-to-end and verify the
    last reached point sits within tolerance of the starting point. Used
    as a backup for ``_is_closed_chain`` when shapely's polygonize fused
    open-polyline segments into a topological face (H4/H5).
    """

    if not loop:
        return False
    if len(loop) == 1:
        info = topo.edges.get(loop[0])
        if info is None:
            return False
        if not info.samples:
            return False
        first = info.samples[0]
        last = info.samples[-1]
        return math.hypot(first[0] - last[0], first[1] - last[1]) < 0.5

    first_info = topo.edges.get(loop[0])
    if first_info is None or not first_info.samples:
        return False
    # Decide orientation of edge 0 by proximity to edge 1's endpoints.
    second_info = topo.edges.get(loop[1])
    if second_info is None or not second_info.samples:
        return False
    e1_pts = (second_info.samples[0], second_info.samples[-1])
    e0_a, e0_b = first_info.samples[0], first_info.samples[-1]
    # Whichever end of e0 is closest to either end of e1 is the joining tail.
    def _d(p, q):
        return math.hypot(p[0] - q[0], p[1] - q[1])
    if min(_d(e0_b, e1_pts[0]), _d(e0_b, e1_pts[1])) <= min(
        _d(e0_a, e1_pts[0]), _d(e0_a, e1_pts[1])
    ):
        start = e0_a
        tail = e0_b
    else:
        start = e0_b
        tail = e0_a

    for k in range(1, len(loop)):
        info = topo.edges.get(loop[k])
        if info is None or not info.samples:
            return False
        a = info.samples[0]
        b = info.samples[-1]
        if _d(tail, a) <= _d(tail, b):
            tail = b
        else:
            tail = a

    return _d(start, tail) < 0.5


def _strategy_d_convex_hull(topo: gmod.TopoGraph) -> list[_Candidate]:
    pts: list[tuple[float, float]] = []
    for info in topo.edges.values():
        pts.extend(info.samples)
    if len(pts) < 3:
        return []

    hull = _convex_hull(pts)
    if len(hull) < 3:
        return []
    area = gmod.polygon_area(hull)
    if area <= 0:
        return []
    cand = _Candidate(loop=[], method="convex_hull", points=hull, closed=True)
    cand.area = area
    cand.perimeter = gmod.polygon_perimeter(hull)
    return [cand]


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone chain — O(n log n) — no scipy dep."""

    pts = sorted(set((round(x, 6), round(y, 6)) for x, y in points))
    if len(pts) <= 1:
        return list(pts)

    def cross(o, a, b):  # type: ignore[no-untyped-def]
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score_candidate(
    cand: _Candidate,
    msp_bbox_area: float,
    inner_counter,
) -> float:
    """Confidence score in [0, 1].

    The weights below are tuned against the three reference samples
    (カラー / センタープレート / ベースフレーム). The method prior is the
    dominant term because convex_hull is structurally always available but
    almost always wrong — it should only win if every structural strategy
    failed.
    """

    # (a) completeness ----------------------------------------------------
    if cand.closed:
        completeness = 1.0
    elif len(cand.points) >= 3:
        completeness = 0.6  # geometry traced but ring isn't fully closed
    else:
        completeness = 0.0

    # (b) compactness — convex polygons score higher (real parts are convex
    #     more often than not in this domain).
    if len(cand.points) >= 3:
        xmin, ymin, xmax, ymax = gmod.polygon_bbox(cand.points)
        bbox_area = max(1e-9, (xmax - xmin) * (ymax - ymin))
        compactness = min(1.0, cand.area / bbox_area)
    else:
        compactness = 0.0

    # (c) inner-element count — at least 1 hole/tap is a strong outer hint
    inner = inner_counter(cand)
    if inner >= 5:
        inner_score = 1.0
    elif inner >= 2:
        inner_score = 0.75
    elif inner >= 1:
        inner_score = 0.55
    else:
        inner_score = 0.25

    # Method prior — heavy emphasis: a CIRCLE / closed LWPOLYLINE / graph
    # cycle is by construction far more trustworthy than a convex hull.
    method_prior = {
        "circle": 1.00,
        "closed_polyline": 0.95,
        "graph": 0.85,
        "convex_hull": 0.30,
        "manual": 0.98,
    }.get(cand.method, 0.5)

    # (d) area ratio — guards against a tiny tap-mark circle outranking
    # the real part outline (H8). Candidates filling at least 4% of the
    # modelspace get full credit (real parts almost always do, even in
    # noisy drawings whose bbox is inflated by stray dimension-extension
    # LINEs the classifier missed); a ratio below ~1% (typical of
    # tap-mark / decorative circles) collapses the score.
    if msp_bbox_area > 0 and cand.area > 0:
        ratio = max(0.0, min(1.0, cand.area / msp_bbox_area))
        area_score = min(1.0, ratio / 0.04)
    else:
        area_score = 0.0

    confidence = (
        0.35 * method_prior
        + 0.20 * completeness
        + 0.15 * compactness
        + 0.10 * inner_score
        + 0.20 * area_score
    )

    return max(0.0, min(1.0, confidence))


def _build_inner_counter(
    items_list: list[tuple[str, str, str, dict[str, Any]]],
    topo: gmod.TopoGraph,
):
    """Return a callable ``f(candidate) -> int`` counting nested entities."""

    # Pre-compute a representative point per entity for cheap PiP tests.
    samples: list[tuple[str, tuple[float, float]]] = []
    for eid, _t, _cat, geom in items_list:
        info = topo.edges.get(eid)
        if info and info.samples:
            pts = info.samples
            # midpoint sample of the entity's sample list
            mid = pts[len(pts) // 2]
            samples.append((eid, mid))
            continue
        # Non-edge entities (TEXT / INSERT / DIMENSION) — use raw geom anchors.
        pt = _representative_point_from_geom(_t, geom)
        if pt is not None:
            samples.append((eid, pt))

    def counter(cand: _Candidate) -> int:
        loop_set = set(cand.loop)
        if len(cand.points) < 3:
            return 0
        count = 0
        for eid, pt in samples:
            if eid in loop_set:
                continue
            if gmod.point_in_polygon(pt, cand.points):
                count += 1
        return count

    return counter


def _topo_extent(topo: gmod.TopoGraph) -> tuple[float, float]:
    if not topo.edges:
        return 0.0, 0.0
    xs: list[float] = []
    ys: list[float] = []
    for info in topo.edges.values():
        for x, y in info.samples:
            xs.append(x)
            ys.append(y)
    if not xs:
        return 0.0, 0.0
    return max(xs) - min(xs), max(ys) - min(ys)


def _is_closed_chain(topo: gmod.TopoGraph, loop: list[str]) -> bool:
    """Are all entities pairwise-adjacent and the last meets the first?"""

    if not loop:
        return False
    if len(loop) == 1:
        info = topo.edges.get(loop[0])
        if info is None:
            return False
        # A CIRCLE self-loop is trivially closed; a single polyline is closed
        # if and only if its endpoints map to the same node.
        return info.node_a == info.node_b

    first_info = topo.edges.get(loop[0])
    if first_info is None:
        return False
    second_info = topo.edges.get(loop[1])
    if second_info is None:
        return False
    shared = {second_info.node_a, second_info.node_b}
    last_node = first_info.node_b if first_info.node_b in shared else first_info.node_a
    start_node = first_info.node_a if last_node == first_info.node_b else first_info.node_b

    for k in range(1, len(loop)):
        info = topo.edges.get(loop[k])
        if info is None:
            return False
        if info.node_a == last_node:
            last_node = info.node_b
        elif info.node_b == last_node:
            last_node = info.node_a
        else:
            return False
    return last_node == start_node


def _loop_summary(cand: _Candidate, topo: gmod.TopoGraph) -> dict[str, Any]:
    """Build the loop_summary dict from a scored candidate."""

    if not cand.points:
        return {
            "closed": cand.closed,
            "segments": 0,
            "lines": 0,
            "arcs": 0,
            "perimeter": 0.0,
            "area": 0.0,
            "bounding_box": {"min_x": 0.0, "min_y": 0.0, "max_x": 0.0, "max_y": 0.0},
        }

    lines, arcs = 0, 0
    for eid in cand.loop:
        info = topo.edges.get(eid)
        if info is None:
            continue
        if info.dxftype == "LINE":
            lines += 1
        elif info.dxftype in ("ARC", "CIRCLE"):
            arcs += 1
        elif info.dxftype in ("LWPOLYLINE", "POLYLINE"):
            verts = info.geom.get("vertices") or []
            for v in verts:
                if len(v) >= 3 and abs(float(v[2])) > 1e-9:
                    arcs += 1
                else:
                    lines += 1

    xmin, ymin, xmax, ymax = gmod.polygon_bbox(cand.points)
    return {
        "closed": cand.closed,
        "segments": max(len(cand.loop), lines + arcs),
        "lines": lines,
        "arcs": arcs,
        "perimeter": round(cand.perimeter, 3),
        "area": round(cand.area, 3),
        "bounding_box": {
            "min_x": round(xmin, 3),
            "min_y": round(ymin, 3),
            "max_x": round(xmax, 3),
            "max_y": round(ymax, 3),
        },
    }


def _empty_result(message: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "confidence": 0.0,
        "method": "",
        "outer_loop": [],
        "loop_summary": None,
        "warnings": [message],
        "candidates": [],
    }


# ---------------------------------------------------------------------------
# Geometry helpers carried over from Phase 1 (used by detect_frame_polyline)
# ---------------------------------------------------------------------------


def _count_inside(
    items: Iterable[tuple[str, DXFEntity, dict[str, Any]]],
    skip_id: str,
    bbox: tuple[float, float, float, float],
) -> int:
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
    if dxftype == "LINE":
        x1 = geom.get("x1"); y1 = geom.get("y1")
        x2 = geom.get("x2"); y2 = geom.get("y2")
        if None in (x1, y1, x2, y2):
            return None
        return ((float(x1) + float(x2)) / 2.0, (float(y1) + float(y2)) / 2.0)
    if dxftype in ("CIRCLE", "ARC"):
        cx = geom.get("cx"); cy = geom.get("cy")
        if cx is None or cy is None:
            return None
        return (float(cx), float(cy))
    if dxftype in ("LWPOLYLINE", "POLYLINE"):
        verts = geom.get("vertices") or []
        if not verts:
            return None
        sx = sum(float(v[0]) for v in verts)
        sy = sum(float(v[1]) for v in verts)
        n = len(verts)
        return (sx / n, sy / n)
    if dxftype in ("TEXT", "MTEXT", "INSERT", "POINT"):
        x = geom.get("x"); y = geom.get("y")
        if x is None or y is None:
            return None
        return (float(x), float(y))
    if dxftype == "DIMENSION":
        anchors = geom.get("anchors") or []
        if not anchors:
            return None
        return (float(anchors[0][0]), float(anchors[0][1]))
    return None


# Wrapper used during scoring; works on the parser-shaped geom dicts.
def _representative_point_from_geom(dxftype: str, geom: dict[str, Any]) -> tuple[float, float] | None:
    return _representative_point(dxftype, geom)


def _area_of(verts: list[list[float]]) -> float:
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
