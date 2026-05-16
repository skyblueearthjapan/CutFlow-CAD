"""注記 — preset-aware MTEXT label persistence + DXF projection.

Three presets share one MTEXT shape but ride on three layers so an
operator can freeze any one category in AutoCAD with a single click.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

log = logging.getLogger(__name__)

NOTE_LAYER_BY_PRESET = {
    "roughness": "CUTFLOW_NOTE_RA",
    "welding": "CUTFLOW_NOTE_WELD",
    "general": "CUTFLOW_NOTE",
}
NOTE_COLOR_BY_PRESET = {
    "roughness": 2,   # yellow
    "welding": 1,     # red
    "general": 7,     # white / by-block default
}


def notes_dxf_extras(notes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project saved notes onto dxf_writer MTEXT descriptors.

    Output shape::

        {"kind": "mtext", "position": [x, y], "text": str,
         "height": float, "rotation": float,
         "layer": str, "color": int}
    """

    out: list[dict[str, Any]] = []
    for n in notes:
        try:
            pos = n["position"]
            txt = str(n.get("text") or "")
            if not txt:
                continue
            x, y = float(pos[0]), float(pos[1])
            h = float(n.get("font_size_mm") or 2.5)
            rot = float(n.get("rotation_deg") or 0.0)
            preset = str(n.get("preset") or "general")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("skipping malformed note %s: %s", n.get("id"), exc)
            continue

        out.append(
            {
                "kind": "mtext",
                "position": [x, y],
                "text": txt,
                "height": h,
                "rotation": rot,
                "layer": NOTE_LAYER_BY_PRESET.get(preset, NOTE_LAYER_BY_PRESET["general"]),
                "color": NOTE_COLOR_BY_PRESET.get(preset, NOTE_COLOR_BY_PRESET["general"]),
            }
        )
    return out
