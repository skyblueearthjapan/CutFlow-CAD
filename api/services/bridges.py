"""ブリッジ (保持タブ) — persistence, auto-placement, DXF projection.

A bridge marks a short gap (``width_mm``) along an outer-loop edge where
the operator wants to leave the part attached to the parent sheet so it
doesn't tip when the cut completes. We store ``(edge_id, position_ratio,
width_mm)``; at export time the writer dispatches each entry into:

* a ``LINE`` annotation on layer ``CUTFLOW_BRIDGE`` (visualises the tab),
* and (optionally, behind ``with_bridges=true``) a perimeter-split
  hint — actually splitting the outer loop into two LINEs is left to the
  CAM nesting step rather than performed here because the part's outer
  is consumed by ``compute_offset`` separately.

Auto-placement (:func:`auto_distribute`) divides the confirmed outer
perimeter evenly into ``count`` slots and selects edges (and ratios)
matching those slots.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any, Iterable

log = logging.getLogger(__name__)

BRIDGE_LAYER = "CUTFLOW_BRIDGE"
BRIDGE_COLOR = 1  # AutoCAD index 1 (red) — high-contrast tab marker.


def _edge_endpoints_by_id(
    payload, loop: list[str]
) -> list[tuple[str, tuple[float, float], tuple[float, float], float]]:
    """Return ``[(edge_id, a, b, length), ...]`` for each entity in ``loop``.

    ``edge_id`` follows the ``E{1..n}`` convention used by chamfer/
    offset. Only LINE / ARC / LWPOLYLINE / POLYLINE are walked here;
    other types contribute zero-length and are skipped by callers.
    """

    by_id = {e.id: e for e in payload.entities}
    out: list[tuple[str, tuple[float, float], tuple[float, float], float]] = []
    for k, eid in enumerate(loop):
        ent = by_id.get(eid)
        if ent is None:
            continue
        g = ent.geom or {}
        if ent.type == "LINE":
            a = (float(g.get("x1", 0.0)), float(g.get("y1", 0.0)))
            b = (float(g.get("x2", 0.0)), float(g.get("y2", 0.0)))
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            out.append((f"E{k + 1}", a, b, length))
        elif ent.type == "ARC":
            cx, cy, r = float(g.get("cx", 0.0)), float(g.get("cy", 0.0)), float(g.get("r", 0.0))
            sa = math.radians(float(g.get("start_angle", 0.0)))
            ea = math.radians(float(g.get("end_angle", 0.0)))
            # Treat the arc as a chord for tab placement (good enough for
            # auto-distribution; the writer can refine if needed).
            a = (cx + r * math.cos(sa), cy + r * math.sin(sa))
            b = (cx + r * math.cos(ea), cy + r * math.sin(ea))
            sweep = ea - sa
            if sweep < 0:
                sweep += 2 * math.pi
            length = abs(r * sweep)
            out.append((f"E{k + 1}", a, b, length))
        elif ent.type in ("LWPOLYLINE", "POLYLINE"):
            verts = g.get("vertices") or []
            if len(verts) >= 2:
                a = (float(verts[0][0]), float(verts[0][1]))
                b = (float(verts[-1][0]), float(verts[-1][1]))
                # Polyline length: sum chord segments.
                length = 0.0
                for i in range(len(verts) - 1):
                    length += math.hypot(
                        float(verts[i + 1][0]) - float(verts[i][0]),
                        float(verts[i + 1][1]) - float(verts[i][1]),
                    )
                out.append((f"E{k + 1}", a, b, length))
    return out


def _expand_polyline_edges(
    payload, loop: list[str]
) -> list[tuple[str, tuple[float, float], tuple[float, float], float]]:
    """Like :func:`_edge_endpoints_by_id` but a closed LWPOLYLINE that
    represents the entire outer expands into one entry per chord
    segment (``E1#0`` .. ``E1#n``). This lets H6 auto-distribute place
    bridges evenly along the actual path rather than collapsing it to
    a single chord between first and last vertices.
    """

    by_id = {e.id: e for e in payload.entities}
    out: list[tuple[str, tuple[float, float], tuple[float, float], float]] = []
    for k, eid in enumerate(loop):
        ent = by_id.get(eid)
        if ent is None:
            continue
        g = ent.geom or {}
        if ent.type in ("LWPOLYLINE", "POLYLINE"):
            verts = g.get("vertices") or []
            closed = bool(g.get("closed"))
            n = len(verts)
            if n < 2:
                continue
            last = n if closed else n - 1
            for j in range(last):
                a = (float(verts[j][0]), float(verts[j][1]))
                b = (
                    float(verts[(j + 1) % n][0]),
                    float(verts[(j + 1) % n][1]),
                )
                length = math.hypot(b[0] - a[0], b[1] - a[1])
                # Single-entity loop → don't prefix the index (label as E1#k).
                if len(loop) == 1:
                    label = f"E{k + 1}#{j}"
                else:
                    label = f"E{k + 1}#{j}"
                out.append((label, a, b, length))
        else:
            # Re-use the chord/arc-as-chord values from the base helper.
            base = _edge_endpoints_by_id(payload, [eid])
            for item in base:
                # Re-label so the per-entity index stays consistent with loop ordering.
                _, a, b, length = item
                out.append((f"E{k + 1}", a, b, length))
    return out


def auto_distribute(
    payload,
    loop: list[str],
    count: int,
    width_mm: float,
) -> list[dict[str, Any]]:
    """Return ``count`` evenly-spaced bridges across the outer perimeter.

    The longest ``count`` edges receive one tab each; for the trivial
    case where the loop has fewer edges than tabs we wrap around and
    place multiple tabs on the longest edges at distinct ``position_ratio``
    values to keep them well separated.

    H6: when the outer loop is a single closed LWPOLYLINE we walk each
    chord segment separately so the slots are distributed across the
    real perimeter rather than degenerating onto the polyline's chord
    between first and last vertices. The composite ``En#k`` label is
    accepted by the POST /bridges validator.
    """

    if count <= 0:
        return []
    # Prefer the per-segment view when *any* loop entity is a polyline so
    # the distribution honours the real outline.
    edges = _expand_polyline_edges(payload, loop) or _edge_endpoints_by_id(payload, loop)
    if not edges:
        return []

    # Total perimeter for slot placement.
    total = sum(e[3] for e in edges) or 0.0
    if total <= 0:
        return []

    slots = [(i + 0.5) * (total / count) for i in range(count)]
    bridges: list[dict[str, Any]] = []
    # Walk the loop accumulating arc length; each slot picks the edge
    # that contains its target distance, with a ratio derived from how
    # far into that edge the slot lands.
    cumulative = 0.0
    edge_ranges: list[tuple[str, float, float, float]] = []  # (eid, start, end, length)
    for eid, _a, _b, length in edges:
        edge_ranges.append((eid, cumulative, cumulative + length, length))
        cumulative += length

    for s in slots:
        for eid, start, end, length in edge_ranges:
            if length <= 0:
                continue
            if start <= s <= end:
                ratio = max(0.0, min(1.0, (s - start) / length))
                bridges.append(
                    {
                        "id": uuid.uuid4().hex[:12],
                        "edge_id": eid,
                        "position_ratio": round(ratio, 4),
                        "width_mm": float(width_mm),
                    }
                )
                break
    return bridges


def attach_positions(
    bridges: Iterable[dict[str, Any]],
    payload,
    loop: list[str],
) -> list[dict[str, Any]]:
    """Return a new list with ``position`` populated for each bridge.

    Lets the frontend skip its own edge→XY math: the backend already
    knows the outer-loop geometry, so we resolve ``(edge_id,
    position_ratio)`` once here and ride it back in the wire payload.

    H6: bridges can reference either ``En`` (chord/arc-as-chord) or the
    composite ``En#k`` form (per-vertex polyline segment); both
    resolutions land here. Bridges whose ``edge_id`` no longer
    references a known edge get ``position=None`` so the UI can omit
    them gracefully.
    """

    # Combine plain + per-segment views so both label formats resolve.
    combined: dict[str, tuple[tuple[float, float], tuple[float, float], float]] = {}
    for eid, a, b, length in _edge_endpoints_by_id(payload, loop):
        combined.setdefault(eid, (a, b, length))
    for eid, a, b, length in _expand_polyline_edges(payload, loop):
        combined.setdefault(eid, (a, b, length))

    out: list[dict[str, Any]] = []
    for br in bridges:
        item = dict(br)
        eid = str(item.get("edge_id") or "")
        ratio = float(item.get("position_ratio") or 0.5)
        info = combined.get(eid)
        if info is None:
            item["position"] = None
            out.append(item)
            continue
        a, b, _length = info
        item["position"] = [
            a[0] + (b[0] - a[0]) * ratio,
            a[1] + (b[1] - a[1]) * ratio,
        ]
        out.append(item)
    return out


def bridges_dxf_extras(
    bridges: Iterable[dict[str, Any]],
    payload,
    loop: list[str],
) -> list[dict[str, Any]]:
    """Project saved bridges into dxf_writer LINE descriptors.

    Each entry is rendered as a short LINE perpendicular to the edge at
    the bridge centre, of length ``width_mm`` — visually a tab marker
    rather than a true geometric split.

    Output dict::

        {"kind": "line", "start": [x, y], "end": [x, y],
         "layer": "CUTFLOW_BRIDGE", "color": 1}
    """

    edges: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {}
    for eid, a, b, _ in _edge_endpoints_by_id(payload, loop):
        edges.setdefault(eid, (a, b))
    for eid, a, b, _ in _expand_polyline_edges(payload, loop):
        edges.setdefault(eid, (a, b))
    out: list[dict[str, Any]] = []
    for br in bridges:
        eid = str(br.get("edge_id") or "")
        pair = edges.get(eid)
        if pair is None:
            log.warning("bridge %s references unknown edge_id %s", br.get("id"), eid)
            continue
        a, b = pair
        ratio = float(br.get("position_ratio") or 0.5)
        width = float(br.get("width_mm") or 2.0)
        mx = a[0] + (b[0] - a[0]) * ratio
        my = a[1] + (b[1] - a[1]) * ratio
        dx, dy = (b[0] - a[0]), (b[1] - a[1])
        n = math.hypot(dx, dy)
        if n < 1e-9:
            continue
        # Perpendicular unit vector × ½ width on each side.
        nx, ny = -dy / n, dx / n
        out.append(
            {
                "kind": "line",
                "start": [mx - nx * width / 2.0, my - ny * width / 2.0],
                "end": [mx + nx * width / 2.0, my + ny * width / 2.0],
                "layer": BRIDGE_LAYER,
                "color": BRIDGE_COLOR,
            }
        )
    return out
