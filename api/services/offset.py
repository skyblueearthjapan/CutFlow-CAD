"""Outer-loop offsetting (加工代) via pyclipper / Clipper2.

Workflow
--------

1. The session already holds the confirmed outer loop as an ordered list of
   entity-ids (saved by :mod:`services.outer_detector`).
2. We discretise that loop into a single closed polygon, sampling arcs and
   bulged polyline segments at ~2° resolution.
3. The polygon is fed to ``pyclipper.PyclipperOffset`` with a positive
   delta to grow the boundary by the requested 加工代.
4. The largest result polygon is returned along with bbox / perimeter /
   plate size / material efficiency metrics.

Per-edge ``edge_overrides`` are interpreted as the **effective offset**
for that edge (NOT additive to ``default_mm``). The UI shows the override
value as the actual offset for that edge, so an entry of ``{"E1": 5.0}``
with ``default_mm=3`` means E1 is offset 5mm outward (the delta added
on top of the global Clipper pass is therefore ``5 - 3 = 2``).

Edge labels are 1-based loop traversal labels (``E1``..``En``). For closed
LWPOLYLINEs the loop is one entity, so composite IDs ``E1#0 / E1#1...``
are used to address each individual segment of the polyline (C2).
"""

from __future__ import annotations

import logging
import math
from typing import Any

import pyclipper

from services import graph as gmod

log = logging.getLogger(__name__)


# Clipper2 works in integer space — scale up to capture 0.001 mm precision.
_SCALE = 1000


class OffsetError(Exception):
    """Raised when pyclipper cannot produce a usable result."""


def compute_offset(
    topo: gmod.TopoGraph,
    loop: list[str],
    default_mm: float,
    edge_overrides: dict[str, float] | None = None,
    corner_join: str = "arc",
) -> dict[str, Any]:
    """Run the full outer-offset pipeline.

    Returns a dict matching ``OffsetResult``::

        {
            "offset_loop": {"type": "LWPOLYLINE", "vertices": [[x,y,bulge],..], "closed": True},
            "perimeter": float,
            "area": float,
            "bounding_box": {min_x, min_y, max_x, max_y},
            "plate_size": "W × H mm",
            "material_efficiency": float,  # original_area / bbox_area_after
            "warnings": [str, ...],
        }
    """

    if not loop:
        raise OffsetError("外径ループが空です")

    if default_mm < 0:
        raise OffsetError("加工代に負の値は指定できません")

    # Every loop entity must be known to the topology — bail early instead
    # of silently dropping orphan IDs (those would cascade into a bad
    # polygon and a misleading offset).
    missing = [eid for eid in loop if eid not in topo.edges]
    if missing:
        raise OffsetError(
            f"ループに未登録のエンティティが含まれています: {', '.join(missing[:5])}"
        )

    overrides = edge_overrides or {}

    # Build polygon AND remember per-sample edge ownership before any flips,
    # so the "E1" → first-loop-edge mapping survives a CCW reversal (H10).
    base_pts, pt_owner = _polygon_with_ownership(topo, loop)
    if len(base_pts) < 3:
        raise OffsetError("ループから多角形を構築できませんでした")

    original_area = gmod.polygon_area(base_pts)
    if original_area <= 0:
        raise OffsetError("ループの面積が0です (自交差の可能性)")

    # Normalise to CCW so pyclipper's "outside" matches our intent.
    # When we reverse the polygon for CCW orientation, the segment
    # ownership map must be re-indexed in lock-step or per-edge
    # overrides land on the wrong sides (H10). For segment-based
    # ownership the rule is: new_seg[i] = old_seg[(n-2-i) % n].
    if _signed_area(base_pts) <= 0:
        n = len(base_pts)
        base_pts = list(reversed(base_pts))
        pt_owner = [pt_owner[(n - 2 - i) % n] for i in range(n)]

    # Apply per-edge bumps before the global offset by inserting a pair of
    # outwardly-shifted "near-corner" points along each labelled edge.
    # ``edge_overrides`` values are the *effective* per-edge offset (not
    # additive to default_mm) — we insert points pushed by ``(override -
    # default_mm)`` so that after the global Clipper pass adds the
    # remaining ``default_mm`` the labelled side reaches the requested
    # value (C1).
    bumped_pts, bumped_owner = _apply_edge_overrides(
        loop, base_pts, pt_owner, overrides, default_mm
    )
    # `bumped_owner` is currently unused by the offsetter but kept so the
    # callsite can grow per-edge debug payloads later without another
    # major refactor.
    _ = bumped_owner

    # ---- Clipper2 offset --------------------------------------------------
    join_type = pyclipper.JT_ROUND if corner_join == "arc" else pyclipper.JT_MITER
    offsetter = pyclipper.PyclipperOffset(miter_limit=2.0)
    int_path = [(int(round(x * _SCALE)), int(round(y * _SCALE))) for x, y in bumped_pts]
    offsetter.AddPath(int_path, join_type, pyclipper.ET_CLOSEDPOLYGON)

    try:
        solution = offsetter.Execute(default_mm * _SCALE)
    except Exception as exc:  # noqa: BLE001 - pyclipper raises plain Exception
        raise OffsetError(f"pyclipper offset failed: {exc}") from exc

    if not solution:
        raise OffsetError("pyclipper returned no polygons (オフセット失敗)")

    # Pick the largest result polygon (offsets sometimes return holes too).
    best_pts = max(solution, key=_int_area_abs)
    out_pts: list[tuple[float, float]] = [(p[0] / _SCALE, p[1] / _SCALE) for p in best_pts]
    out_pts = _ensure_ccw(out_pts)

    perimeter = gmod.polygon_perimeter(out_pts)
    area = gmod.polygon_area(out_pts)
    bbox = gmod.polygon_bbox(out_pts)
    xmin, ymin, xmax, ymax = bbox
    plate_w = xmax - xmin
    plate_h = ymax - ymin
    plate_size = f"{plate_w:.0f} × {plate_h:.0f} mm"

    bbox_area = max(1e-9, plate_w * plate_h)
    efficiency = max(0.0, min(1.0, original_area / bbox_area))

    warnings: list[str] = []
    if len(solution) > 1:
        warnings.append(
            f"オフセット結果が {len(solution)} 個のポリゴンに分裂しています (最大のものを採用)"
        )

    vertices = [[round(x, 3), round(y, 3), 0.0] for x, y in out_pts]

    return {
        "offset_loop": {
            "type": "LWPOLYLINE",
            "vertices": vertices,
            "closed": True,
        },
        "perimeter": round(perimeter, 3),
        "area": round(area, 3),
        "bounding_box": {
            "min_x": round(xmin, 3),
            "min_y": round(ymin, 3),
            "max_x": round(xmax, 3),
            "max_y": round(ymax, 3),
        },
        "plate_size": plate_size,
        "material_efficiency": round(efficiency, 4),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Internal helpers
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


def _int_area_abs(int_path: list[list[int]] | list[tuple[int, int]]) -> float:
    n = len(int_path)
    if n < 3:
        return 0.0
    s = 0
    for i in range(n):
        x1, y1 = int_path[i]
        x2, y2 = int_path[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _polygon_with_ownership(
    topo: gmod.TopoGraph,
    loop: list[str],
) -> tuple[list[tuple[float, float]], list[str | None]]:
    """Build the polygon AND a parallel list mapping each polygon
    *segment* (the edge from ``pts[i]`` to ``pts[(i+1) % n]``) to its
    owning entity label.

    Labels follow the ``E{1-based}`` convention. For a *single closed
    polyline* loop (Strategy B), composite labels ``E1#0``, ``E1#1``, ...
    address each vertex-to-vertex side individually (C2).

    The ``segment_owner`` list has the same length as ``pts``: entry
    ``i`` is the label of the polygon segment leaving ``pts[i]``.
    """

    pts = gmod.polygon_from_loop(topo, loop)
    owner: list[str | None] = [None] * len(pts)

    if not loop or len(pts) < 2:
        return pts, owner

    # ----- Single-entity loop (CIRCLE / closed polyline / closed POLYLINE)
    if len(loop) == 1:
        info = topo.edges.get(loop[0])
        if info is None:
            return pts, owner
        if info.dxftype in ("LWPOLYLINE", "POLYLINE"):
            verts = info.geom.get("vertices") or []
            n_verts = len(verts)
            if n_verts >= 2:
                seg_labels = [f"E1#{k}" for k in range(n_verts)]
                vertex_coords = [(float(v[0]), float(v[1])) for v in verts]
                # Each polygon point belongs to the segment leaving it.
                # We assign by walking: every time the *next* polygon
                # point matches a vertex, the segment advances.
                cur_seg = 0
                for i in range(len(pts)):
                    owner[i] = seg_labels[cur_seg]
                    # Look ahead to decide if the NEXT point lands at a
                    # new vertex (and so leaves a new segment).
                    j = (i + 1) % len(pts)
                    nxt_v = vertex_coords[(cur_seg + 1) % n_verts]
                    if gmod._close(pts[j], nxt_v, tol=1e-3) and cur_seg + 1 < n_verts:
                        cur_seg += 1
            else:
                owner = ["E1"] * len(pts)
        else:
            owner = ["E1"] * len(pts)
        return pts, owner

    # ----- Multi-entity loop — re-walk the chain to assign per-segment
    # labels in lock-step with ``polygon_from_loop``.
    e0 = topo.edges.get(loop[0])
    e1 = topo.edges.get(loop[1])
    if e0 is None or e1 is None:
        return pts, owner

    out_pts: list[tuple[float, float]] = []
    seg_labels: list[str | None] = []  # label of segment leaving each pt

    shared_with_next = {e1.node_a, e1.node_b}
    if e0.node_b in shared_with_next:
        cur_pts = list(e0.samples)
        last_node = e0.node_b
    else:
        cur_pts = list(reversed(e0.samples))
        last_node = e0.node_a
    out_pts.extend(cur_pts)
    # Segments INSIDE e0 (between e0's own samples) are owned by E1; the
    # last sample's outgoing segment belongs to the NEXT edge (will be
    # patched below).
    seg_labels.extend(["E1"] * len(cur_pts))

    for k in range(1, len(loop)):
        info = topo.edges.get(loop[k])
        if info is None:
            continue
        label = f"E{k + 1}"
        if info.node_a == last_node:
            seg = list(info.samples)
            last_node = info.node_b
        elif info.node_b == last_node:
            seg = list(reversed(info.samples))
            last_node = info.node_a
        else:
            tail = out_pts[-1] if out_pts else (0.0, 0.0)
            d_a = math.hypot(info.samples[0][0] - tail[0], info.samples[0][1] - tail[1])
            d_b = math.hypot(info.samples[-1][0] - tail[0], info.samples[-1][1] - tail[1])
            if d_a <= d_b:
                seg = list(info.samples)
                last_node = info.node_b
            else:
                seg = list(reversed(info.samples))
                last_node = info.node_a
        if seg and out_pts and gmod._close(seg[0], out_pts[-1]):
            # The shared corner is dropped from `seg`; the OUTGOING
            # segment from `out_pts[-1]` now belongs to this new edge.
            if seg_labels:
                seg_labels[-1] = label
            seg = seg[1:]
        out_pts.extend(seg)
        seg_labels.extend([label] * len(seg))

    # Drop closing duplicate, matching polygon_from_loop's behaviour.
    # The dropped point's outgoing segment wrapped back to pts[0], so
    # we leave seg_labels[-1] (now belonging to the *previous* point)
    # owning that wrap-around segment.
    if len(out_pts) >= 2 and gmod._close(out_pts[0], out_pts[-1]):
        out_pts.pop()
        seg_labels.pop()

    # Now seg_labels[i] owns the segment from out_pts[i] to out_pts[i+1].
    return out_pts, seg_labels


def _apply_edge_overrides(
    loop: list[str],
    base_pts: list[tuple[float, float]],
    seg_owner: list[str | None],
    overrides: dict[str, float],
    default_mm: float,
) -> tuple[list[tuple[float, float]], list[str | None]]:
    """Shift each labelled polygon segment outward by ``(override - default_mm)``.

    Implementation: rather than translate the shared corner vertices
    (which would also drag the adjacent edges around), we insert a pair
    of new "near-corner" sample points along each labelled segment, each
    pushed outward by the requested delta. The result is a flat shelf
    along the labelled side; pyclipper then offsets the whole polygon
    by ``default_mm`` so the labelled side reaches the requested
    effective offset and adjacent edges keep their unshifted distance.

    ``seg_owner[i]`` owns the segment from ``base_pts[i]`` to
    ``base_pts[(i+1) % n]``.

    Returns ``(new_pts, new_seg_owner)``.
    """

    if not overrides or not loop or len(base_pts) < 2:
        return base_pts, list(seg_owner)

    n = len(base_pts)
    out_pts: list[tuple[float, float]] = []
    out_owner: list[str | None] = []

    for i in range(n):
        out_pts.append(base_pts[i])
        owner = seg_owner[i] if i < len(seg_owner) else None
        out_owner.append(owner)
        if owner is None or owner not in overrides:
            continue
        extra = overrides[owner] - default_mm
        if extra <= 0:
            continue
        p_start = base_pts[i]
        p_end = base_pts[(i + 1) % n]
        dx = p_end[0] - p_start[0]
        dy = p_end[1] - p_start[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        tx, ty = dx / length, dy / length
        # Outward normal for a CCW polygon = rotate tangent -90°.
        ox, oy = ty, -tx
        # Inset 1% of segment length (clamped) so the shelf never spills
        # into the adjacent edges.
        inset = max(min(length * 0.01, 0.5), 1e-3)
        near_start = (
            p_start[0] + tx * inset + ox * extra,
            p_start[1] + ty * inset + oy * extra,
        )
        near_end = (
            p_end[0] - tx * inset + ox * extra,
            p_end[1] - ty * inset + oy * extra,
        )
        # Insert near_start then near_end so the polygon order is
        # corner-start → near_start → near_end → corner-end. Their
        # outgoing segments are still owned by the same label.
        out_pts.append(near_start)
        out_owner.append(owner)
        out_pts.append(near_end)
        out_owner.append(owner)

    return out_pts, out_owner
