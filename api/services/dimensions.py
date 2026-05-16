"""寸法 (linear / aligned / diameter / radius) — persistence + DXF projection.

Phase 4 contract
----------------

* Frontend posts the full list on every change (last-write-wins, same as
  ChamferRequest). We store it under ``state/{fid}/dimensions.json``.
* On export, :func:`dimensions_dxf_extras` translates each entry into the
  parameters consumed by ``dxf_writer.add_dimensions`` (it uses ezdxf's
  ``msp.add_linear_dim`` / ``add_aligned_dim`` / ``add_radius_dim`` /
  ``add_diameter_dim``).
"""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable

log = logging.getLogger(__name__)

DIMENSION_LAYER = "CUTFLOW_DIM"
DIMENSION_COLOR = 3  # AutoCAD index 3 (green) — matches mockups.dim hue.


def dimensions_dxf_extras(
    dimensions: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project saved dimension dicts onto dxf_writer descriptors.

    Each output dict has the shape::

        {"type": "linear" | "aligned" | "diameter" | "radius",
         "p1": [x, y], "p2": [x, y],
         "text_override": str | None,
         "layer": "CUTFLOW_DIM", "color": 3,
         "offset": 8.0}

    The writer is responsible for selecting the right ezdxf call. We
    place the dim line 8 mm away from the measured segment by default —
    a workshop-friendly clearance.
    """

    out: list[dict[str, Any]] = []
    for d in dimensions:
        try:
            p1 = [float(d["p1"][0]), float(d["p1"][1])]
            p2 = [float(d["p2"][0]), float(d["p2"][1])]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("skipping malformed dimension %s: %s", d.get("id"), exc)
            continue
        out.append(
            {
                "id": str(d.get("id") or ""),
                "type": str(d.get("type") or "linear"),
                "p1": p1,
                "p2": p2,
                "text_override": d.get("text_override"),
                "layer": DIMENSION_LAYER,
                "color": DIMENSION_COLOR,
                "offset": 8.0,
            }
        )
    return out


def dimension_distance(p1: list[float], p2: list[float]) -> float:
    """Euclidean distance between two 2-D points; safe on short lists."""

    if len(p1) < 2 or len(p2) < 2:
        return 0.0
    return math.hypot(float(p2[0]) - float(p1[0]), float(p2[1]) - float(p1[1]))
