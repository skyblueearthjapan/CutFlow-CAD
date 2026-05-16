"""C面 (面取り) / 開先 detection, listing, and annotation generation.

Phase 3 contract
----------------

Given a confirmed outer loop (list of entity_ids, from
``store.read_outer``), we expose:

* :func:`list_corners` — return ``C1..Cn`` (corners) and ``E1..En`` (edges)
  with coordinates + interior-angle info, so the frontend can render the
  chamfer-mode picker.
* :func:`build_annotations` — turn a list of saved :class:`ChamferSpec`
  entries into canvas annotations (position + label per spec) for the
  inspector / SVG overlay.
* :func:`chamfer_dxf_extras` — produce the LEADER + MTEXT entries the
  DXF writer should add to the cleaned export so an AutoCAD operator
  reads the chamfer / bevel notes directly from the file.

Design notes
------------

* The "corner" geometry is taken from the outer-loop polygon stitched by
  :func:`services.graph.polygon_from_loop`. We walk the polygon's edges,
  detect direction-change points (= corners), then label them ``C1..Cn``
  in CCW traversal order.
* For single-entity loops (CIRCLE / closed LWPOLYLINE), a CIRCLE has no
  corners (returns empty) and a polyline uses its raw vertices as
  corners (with the same direction-change check filtering near-collinear
  vertices out — straight midpoints would only confuse the operator).
* All DXF annotation entities land on a dedicated ``CUTFLOW_CHAMFER``
  layer (purple, color index 6) so they're trivially identifiable
  downstream.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable

from services import graph as gmod

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Layer & color used by the DXF writer for chamfer annotations.
CHAMFER_LAYER = "CUTFLOW_CHAMFER"
CHAMFER_COLOR = 6  # AutoCAD index 6 = magenta — closest to spec purple #a78bfa.

# Below this turning angle (degrees) we treat consecutive segments as
# "straight" and drop the in-between point from the corner list. Most
# real workshop drawings have either crisp 90° corners or arcs sampled
# at 2° steps — 3° is a good middle ground.
_CORNER_TURN_THRESHOLD_DEG = 3.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_corners(
    topo: gmod.TopoGraph,
    loop: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(corners, edges)`` for the confirmed outer loop.

    Each corner dict matches :class:`models.CornerInfo`::

        {"corner_id": "C1", "position": [x, y], "angle_deg": 90.0,
         "is_acute": False, "is_convex": True}

    Each edge dict matches :class:`models.EdgeInfo`::

        {"edge_id": "E1", "midpoint": [mx, my], "length": 123.45}

    Edge labels follow the same ``E{1-based}`` convention used by the
    offset service so a single ID space governs both inspectors.
    """

    if not loop:
        return [], []

    pts = gmod.polygon_from_loop(topo, loop)
    if len(pts) < 3:
        return [], []

    # M2: defend against degenerate polygons. A near-zero signed area means
    # the loop collapsed (collinear or overlapping vertices) — every angle
    # would be ill-defined, so returning no corners is safer than rendering
    # garbage at the canvas.
    if abs(_signed_area(pts)) < 1e-6:
        return [], []

    # Ensure CCW so "convex/凸" matches the visual "outward" intuition.
    pts = _ensure_ccw(pts)
    corner_points = _pick_corner_points(pts)

    corners: list[dict[str, Any]] = []
    n = len(corner_points)
    for i, (idx, pt, turn_deg) in enumerate(corner_points):
        # Interior angle = 180 - turn (turn>0 → convex left bend in CCW).
        interior = 180.0 - turn_deg
        corners.append(
            {
                "corner_id": f"C{i + 1}",
                "position": [round(pt[0], 3), round(pt[1], 3)],
                "angle_deg": round(interior, 2),
                "is_acute": interior < 90.0,
                "is_convex": turn_deg > 0.0,
            }
        )

    # Edge list — one per entity in the loop, in traversal order.
    # We use the per-entity midpoint and length from the topology so
    # "E1" ↔ ``loop[0]`` stays consistent with the offset module.
    edges: list[dict[str, Any]] = []
    for k, eid in enumerate(loop):
        info = topo.edges.get(eid)
        if info is None or not info.samples:
            continue
        mid = info.samples[len(info.samples) // 2]
        edges.append(
            {
                "edge_id": f"E{k + 1}",
                "midpoint": [round(float(mid[0]), 3), round(float(mid[1]), 3)],
                "length": round(float(info.length), 3),
            }
        )

    # For single-entity loops with multiple polyline segments, surface
    # composite labels ``E1#0 / E1#1 / ...`` so the open-bevel UI can
    # address individual polyline sides (mirrors the offset module's
    # ownership scheme).
    if len(loop) == 1:
        only = topo.edges.get(loop[0])
        if only is not None and only.dxftype in ("LWPOLYLINE", "POLYLINE"):
            verts = only.geom.get("vertices") or []
            n_v = len(verts)
            if n_v >= 2:
                edges = []  # replace whole-entity edge with per-side edges
                closed = bool(only.geom.get("closed"))
                last = n_v if closed else n_v - 1
                for k in range(last):
                    a = verts[k]
                    b = verts[(k + 1) % n_v]
                    ax, ay = float(a[0]), float(a[1])
                    bx, by = float(b[0]), float(b[1])
                    mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
                    length = math.hypot(bx - ax, by - ay)
                    edges.append(
                        {
                            "edge_id": f"E1#{k}",
                            "midpoint": [round(mx, 3), round(my, 3)],
                            "length": round(length, 3),
                        }
                    )

    return corners, edges


def build_annotations(
    specs: Iterable[dict[str, Any]],
    corners: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project saved chamfer specs onto canvas annotation items.

    Unknown ``corner_id`` entries (stale state after re-detect) are
    silently dropped — the caller has already validated the request
    payload separately.
    """

    by_corner = {c["corner_id"]: c for c in corners}
    by_edge = {e["edge_id"]: e for e in edges}

    items: list[dict[str, Any]] = []
    for spec in specs:
        cid = str(spec.get("corner_id") or "")
        ctype = str(spec.get("type") or "C")
        size = float(spec.get("size_mm") or 0.0)
        angle = float(spec.get("angle_deg") or 0.0)

        if cid in by_corner:
            pos = by_corner[cid]["position"]
            label = _format_chamfer_label(ctype, size, angle)
            items.append(
                {
                    "corner_id": cid,
                    "position": list(pos),
                    "label": label,
                    "kind": ctype if ctype in ("C", "bevel") else "C",
                }
            )
            continue

        if cid in by_edge:
            pos = by_edge[cid]["midpoint"]
            label = _format_chamfer_label(ctype, size, angle)
            items.append(
                {
                    "corner_id": cid,
                    "position": list(pos),
                    "label": label,
                    "kind": ctype if ctype in ("C", "bevel") else "bevel",
                }
            )
            continue

        # Unknown — skip but log so debugging stale state is possible.
        log.debug("chamfer spec references unknown corner/edge: %s", cid)

    return items


def chamfer_dxf_extras(
    specs: Iterable[dict[str, Any]],
    corners: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a list of dxf-writer-friendly annotation descriptors.

    Each dict has the shape::

        {"kind": "leader_text", "anchor": [x, y], "text": "C2",
         "layer": "CUTFLOW_CHAMFER", "color": 6}

    The writer's job is to translate each entry into a LEADER + MTEXT
    pair (or a standalone MTEXT for edges) anchored at ``anchor``.
    """

    annotations = build_annotations(specs, corners, edges)
    extras: list[dict[str, Any]] = []
    for item in annotations:
        extras.append(
            {
                "kind": "leader_text",
                "anchor": list(item["position"]),
                "text": item["label"],
                "layer": CHAMFER_LAYER,
                "color": CHAMFER_COLOR,
                "for": item["kind"],
            }
        )
    return extras


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signed_area(pts: list[tuple[float, float]]) -> float:
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _ensure_ccw(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return list(pts) if _signed_area(pts) > 0 else list(reversed(pts))


def _turn_deg(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
) -> float:
    """Signed turn angle (degrees) at ``b`` going A → B → C.

    Positive = left turn (convex on CCW polygon), negative = right turn
    (concave / reflex). Returns 0 for near-collinear points.
    """

    v1x, v1y = b[0] - a[0], b[1] - a[1]
    v2x, v2y = c[0] - b[0], c[1] - b[1]
    n1 = math.hypot(v1x, v1y)
    n2 = math.hypot(v2x, v2y)
    if n1 < 1e-9 or n2 < 1e-9:
        return 0.0
    cross = v1x * v2y - v1y * v2x
    dot = v1x * v2x + v1y * v2y
    return math.degrees(math.atan2(cross, dot))


def _pick_corner_points(
    pts: list[tuple[float, float]],
) -> list[tuple[int, tuple[float, float], float]]:
    """Identify corners as turning points exceeding the threshold.

    Returns ``[(polygon_index, (x, y), signed_turn_deg), ...]`` in
    traversal order. Arc-discretised polygons get one corner per
    geometric corner (the 2° intermediate samples fall below the
    threshold and are dropped).
    """

    n = len(pts)
    out: list[tuple[int, tuple[float, float], float]] = []
    for i in range(n):
        a = pts[(i - 1) % n]
        b = pts[i]
        c = pts[(i + 1) % n]
        t = _turn_deg(a, b, c)
        if abs(t) >= _CORNER_TURN_THRESHOLD_DEG:
            out.append((i, b, t))
    return out


def _format_chamfer_label(ctype: str, size_mm: float, angle_deg: float) -> str:
    """Render a human-readable label for a chamfer / bevel spec.

    Examples::

        C面 size=2 angle=45 → "C2"
        C面 size=3 angle=30 → "C3×30°"
        開先 angle=30        → "開先 30°"
    """

    if ctype == "bevel":
        return f"開先 {angle_deg:g}°"
    # ``C`` (面取り): show size as ``Cn``; append angle when non-45°.
    if abs(angle_deg - 45.0) < 0.01:
        # ``Cn`` reads as "C面 nmm at 45°" by convention.
        return f"C{size_mm:g}"
    return f"C{size_mm:g}×{angle_deg:g}°"
