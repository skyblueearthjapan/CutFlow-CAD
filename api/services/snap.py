"""Snap-point computation for line-edit mode.

Given a click position (mm) and a snap radius (mm), return the closest
candidate point of the requested types. We materialise candidates from
the parsed FileEntities payload — no KD-tree needed at our entity counts
(workshop drawings rarely exceed a few hundred entities), but the code
is structured so plugging scipy.spatial.cKDTree in later is a 5-line
swap.

Snap types
----------

* ``endpoint``     — LINE / ARC / POLYLINE endpoints.
* ``midpoint``     — LINE midpoints; polyline segment midpoints.
* ``center``       — CIRCLE / ARC centres.
* ``quadrant``     — CIRCLE quadrants (N/E/S/W).
* ``intersection`` — pairwise LINE-LINE intersections (skipped if more
  than ``_MAX_LINES_FOR_INTERSECT`` lines to keep responses snappy).
* ``grid``         — round position to ``grid_size`` (defaults to 1 mm).
"""

from __future__ import annotations

import math
from typing import Any, Iterable

# Beyond this many lines we skip the O(n²) intersection scan — it would
# stall the request for >50 ms which is noticeable in an interactive
# snap loop.
_MAX_LINES_FOR_INTERSECT = 200
_GRID_DEFAULT_MM = 1.0


def find_snap(
    position: tuple[float, float],
    entities: Iterable[Any],
    snap_types: Iterable[str],
    tolerance: float,
) -> dict[str, Any] | None:
    """Return the best snap candidate within ``tolerance`` of ``position``.

    Returns ``None`` when no candidate is in range. The returned dict
    matches :class:`models.SnapResponse` minus the wrapper:
    ``{"snapped": [x, y], "type": SnapType, "entity_id": str | None,
       "distance": float}``.
    """

    types = set(snap_types)
    px, py = float(position[0]), float(position[1])
    best: tuple[float, dict[str, Any]] | None = None

    def _consider(p: tuple[float, float], t: str, eid: str | None) -> None:
        nonlocal best
        d = math.hypot(p[0] - px, p[1] - py)
        if d > tolerance:
            return
        if best is None or d < best[0]:
            best = (d, {
                "snapped": [round(p[0], 4), round(p[1], 4)],
                "type": t,
                "entity_id": eid,
                "distance": round(d, 4),
            })

    lines: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    for ent in entities:
        et = getattr(ent, "type", None)
        geom = getattr(ent, "geom", None) or {}
        eid = getattr(ent, "id", None)

        if et == "LINE":
            a = (float(geom.get("x1", 0.0)), float(geom.get("y1", 0.0)))
            b = (float(geom.get("x2", 0.0)), float(geom.get("y2", 0.0)))
            if "endpoint" in types:
                _consider(a, "endpoint", eid)
                _consider(b, "endpoint", eid)
            if "midpoint" in types:
                _consider(((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0), "midpoint", eid)
            if "intersection" in types:
                lines.append((eid or "", a, b))

        elif et == "CIRCLE":
            cx, cy, r = float(geom.get("cx", 0.0)), float(geom.get("cy", 0.0)), float(geom.get("r", 0.0))
            if "center" in types:
                _consider((cx, cy), "center", eid)
            if "quadrant" in types and r > 0:
                _consider((cx + r, cy), "quadrant", eid)
                _consider((cx - r, cy), "quadrant", eid)
                _consider((cx, cy + r), "quadrant", eid)
                _consider((cx, cy - r), "quadrant", eid)

        elif et == "ARC":
            cx, cy, r = float(geom.get("cx", 0.0)), float(geom.get("cy", 0.0)), float(geom.get("r", 0.0))
            if "center" in types:
                _consider((cx, cy), "center", eid)
            if "endpoint" in types and r > 0:
                sa = math.radians(float(geom.get("start_angle", 0.0)))
                ea = math.radians(float(geom.get("end_angle", 0.0)))
                _consider((cx + r * math.cos(sa), cy + r * math.sin(sa)), "endpoint", eid)
                _consider((cx + r * math.cos(ea), cy + r * math.sin(ea)), "endpoint", eid)

        elif et in ("LWPOLYLINE", "POLYLINE"):
            verts = geom.get("vertices") or []
            n = len(verts)
            closed = bool(geom.get("closed"))
            if n == 0:
                continue
            if "endpoint" in types:
                for v in verts:
                    _consider((float(v[0]), float(v[1])), "endpoint", eid)
            if "midpoint" in types:
                last = n if closed else n - 1
                for k in range(last):
                    a = verts[k]
                    b = verts[(k + 1) % n]
                    ax, ay = float(a[0]), float(a[1])
                    bx, by = float(b[0]), float(b[1])
                    _consider(((ax + bx) / 2.0, (ay + by) / 2.0), "midpoint", eid)

    # Intersection scan — O(n²) but bounded.
    if "intersection" in types and len(lines) <= _MAX_LINES_FOR_INTERSECT:
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                hit = _line_intersection(lines[i][1], lines[i][2], lines[j][1], lines[j][2])
                if hit is not None:
                    _consider(hit, "intersection", None)

    # Grid snap is always a last-resort fallback (lowest priority — it
    # has no entity attached so we only emit it if nothing better was
    # found within tolerance).
    if best is None and "grid" in types:
        g = _GRID_DEFAULT_MM
        gx = round(px / g) * g
        gy = round(py / g) * g
        d = math.hypot(gx - px, gy - py)
        if d <= tolerance:
            return {
                "snapped": [gx, gy],
                "type": "grid",
                "entity_id": None,
                "distance": round(d, 4),
            }

    return best[1] if best else None


def _line_intersection(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    p4: tuple[float, float],
) -> tuple[float, float] | None:
    """Return the intersection point of two finite segments, or None.

    Standard parametric form; bounds-checked in both parameters so we
    only emit true intersections (not virtual extensions). Returns None
    on parallel lines (denominator → 0).
    """

    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-9:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom
    if not (-1e-6 <= t <= 1 + 1e-6 and -1e-6 <= u <= 1 + 1e-6):
        return None
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
