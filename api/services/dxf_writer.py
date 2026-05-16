"""Write a cleaned DXF by re-reading the original and excluding deleted IDs.

We never mutate the source; we always re-parse from disk so a fresh export
always reflects the original geometry minus the current delete reservation.
This makes undo trivial (just clear the delete list).

Phase 2 adds the optional ``extra_polylines`` parameter so an export can
include the offset (加工代) loop as a new LWPOLYLINE on a dedicated layer.

Phase 3 adds the optional ``chamfer_annotations`` parameter so chamfer /
bevel notes (LEADER + MTEXT) ride along on the dedicated
``CUTFLOW_CHAMFER`` layer.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from ezdxf import recover

log = logging.getLogger(__name__)


# Layer / colour for offset polylines added by export_clean_dxf.
_OFFSET_LAYER = "CUTFLOW_OFFSET"
_OFFSET_COLOR = 4  # AutoCAD cyan — matches the on-screen "残す/構造" colour.

# Phase 3 — chamfer / bevel annotations.
_CHAMFER_LAYER = "CUTFLOW_CHAMFER"
_CHAMFER_COLOR = 6  # AutoCAD magenta — closest index to spec purple #a78bfa.
_CHAMFER_TEXT_HEIGHT = 2.5  # mm — matches DESIGN.md note about isocp 2.5mm
_CHAMFER_LEADER_LEN = 8.0  # mm — short pointer line from anchor to label


def export_clean_dxf(
    source: Path | str,
    deleted_ids: set[str],
    dest: Path | str,
    extra_polylines: Iterable[dict] | None = None,
    chamfer_annotations: Iterable[dict] | None = None,
) -> int:
    """Copy ``source`` to ``dest`` minus the modelspace entities with the
    given deterministic IDs (``e00001`` etc., assigned at parse time).

    ``extra_polylines`` (Phase 2) is an iterable of dicts shaped like
    ``{"vertices": [[x, y, bulge], ...], "closed": True, "layer": str,
    "color": int}``. Each entry is appended as a new LWPOLYLINE on the
    target DXF before saving. ``layer``/``color`` are optional.

    Returns the number of entities actually removed.
    """

    doc, _auditor = recover.readfile(str(source))
    msp = doc.modelspace()

    to_remove = []
    for idx, e in enumerate(msp):
        eid = f"e{idx:05d}"
        if eid in deleted_ids:
            to_remove.append(e)

    for e in to_remove:
        msp.delete_entity(e)

    # Append offset polylines as new entities so downstream CAM tools see
    # them. We keep them on a clearly-named layer (``CUTFLOW_OFFSET``) and
    # in cyan so they're easy to spot or strip out.
    added = 0
    if extra_polylines:
        for poly in extra_polylines:
            verts = poly.get("vertices") or []
            if len(verts) < 3:
                continue
            layer = str(poly.get("layer") or _OFFSET_LAYER)
            color = int(poly.get("color") or _OFFSET_COLOR)
            closed = bool(poly.get("closed", True))

            # ezdxf accepts [x, y] OR [x, y, start_width, end_width, bulge]
            # but auto-promotes 3-tuples (x, y, bulge) when format="xyb".
            try:
                _ensure_layer(doc, layer, color)
                msp.add_lwpolyline(
                    [[float(v[0]), float(v[1]), float(v[2]) if len(v) >= 3 else 0.0] for v in verts],
                    format="xyb",
                    close=closed,
                    dxfattribs={"layer": layer, "color": color},
                )
                added += 1
            except Exception as exc:  # noqa: BLE001 - bad input shouldn't kill export
                log.warning("skipping malformed extra polyline: %s", exc)

    # Append chamfer / bevel annotations (Phase 3). One LEADER + one
    # MTEXT per spec, all on ``CUTFLOW_CHAMFER`` so downstream operators
    # can isolate them with a single layer freeze.
    notes_added = 0
    if chamfer_annotations:
        _ensure_layer(doc, _CHAMFER_LAYER, _CHAMFER_COLOR)
        for ann in chamfer_annotations:
            anchor = ann.get("anchor")
            text = str(ann.get("text") or "")
            if not anchor or len(anchor) < 2 or not text:
                continue
            try:
                ax = float(anchor[0])
                ay = float(anchor[1])
                tx = ax + _CHAMFER_LEADER_LEN
                ty = ay + _CHAMFER_LEADER_LEN
                # LEADER from the anchor up-right to the text insert.
                msp.add_leader(
                    [(ax, ay), (tx, ty)],
                    dxfattribs={"layer": _CHAMFER_LAYER, "color": _CHAMFER_COLOR},
                )
                msp.add_mtext(
                    text,
                    dxfattribs={
                        "layer": _CHAMFER_LAYER,
                        "color": _CHAMFER_COLOR,
                        "char_height": _CHAMFER_TEXT_HEIGHT,
                        "insert": (tx + 0.5, ty + 0.5),
                    },
                )
                notes_added += 1
            except Exception as exc:  # noqa: BLE001 - malformed annotation should not abort export
                log.warning("skipping malformed chamfer annotation: %s", exc)

    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(dest))
    log.info(
        "exported %s (removed %d, added %d polylines, %d notes) — total now %d entities",
        dest,
        len(to_remove),
        added,
        notes_added,
        sum(1 for _ in msp),
    )
    return len(to_remove)


def _ensure_layer(doc, name: str, color: int) -> None:
    """Create the layer if needed; set colour to keep DXF readers happy."""

    if name in doc.layers:
        return
    try:
        doc.layers.add(name=name, color=color)
    except Exception:  # noqa: BLE001 - duplicate / readonly layer names
        pass
