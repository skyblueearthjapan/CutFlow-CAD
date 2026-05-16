"""穴追加 — persistence + DXF circle materialisation.

Holes are stored as ``{id, position, diameter, tap_note}``; the writer
materialises each one as a CIRCLE entity on layer ``CUTFLOW_HOLE`` and,
when ``tap_note`` is present, drops an MTEXT label beside it.

Pattern expansion
-----------------

:func:`expand_pattern` takes ``(anchor, rows, cols, spacing, diameter)``
and returns a list of :class:`AddedHole`-shaped dicts. The pattern is
laid out row-major: anchor is the bottom-left hole.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Iterable

log = logging.getLogger(__name__)

HOLE_LAYER = "CUTFLOW_HOLE"
HOLE_COLOR = 5  # AutoCAD index 5 (blue) — distinguishes added holes from existing.
HOLE_TAP_TEXT_HEIGHT = 2.5  # mm


def holes_dxf_extras(holes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project saved hole dicts onto dxf_writer descriptors.

    Each output dict::

        {"kind": "circle", "center": [x, y], "radius": float,
         "tap_note": str | None, "layer": "CUTFLOW_HOLE", "color": 5}
    """

    out: list[dict[str, Any]] = []
    for h in holes:
        try:
            pos = h["position"]
            d = float(h["diameter"])
            cx, cy = float(pos[0]), float(pos[1])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("skipping malformed hole %s: %s", h.get("id"), exc)
            continue
        if d <= 0:
            continue
        out.append(
            {
                "kind": "circle",
                "center": [cx, cy],
                "radius": d / 2.0,
                "tap_note": h.get("tap_note"),
                "layer": HOLE_LAYER,
                "color": HOLE_COLOR,
            }
        )
    return out


def expand_pattern(
    anchor: list[float],
    rows: int,
    cols: int,
    spacing: list[float],
    diameter: float,
    tap_note: str | None = None,
) -> list[dict[str, Any]]:
    """Materialise an integer grid of holes; returns a list of dicts that
    match :class:`models.AddedHole`."""

    if rows <= 0 or cols <= 0:
        return []
    ax, ay = float(anchor[0]), float(anchor[1])
    sx, sy = float(spacing[0]), float(spacing[1])
    out: list[dict[str, Any]] = []
    for r in range(rows):
        for c in range(cols):
            out.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "position": [ax + c * sx, ay + r * sy],
                    "diameter": diameter,
                    "tap_note": tap_note,
                }
            )
    return out
