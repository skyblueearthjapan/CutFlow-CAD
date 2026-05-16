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
    dimensions: Iterable[dict] | None = None,
    edits: Iterable[dict] | None = None,
    added_holes: Iterable[dict] | None = None,
    notes: Iterable[dict] | None = None,
    bridges: Iterable[dict] | None = None,
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

    # Apply vertex edits BEFORE deletion so the index mapping (e{idx:05d})
    # still matches the parsed payload (deletion shifts iteration order).
    if edits:
        try:
            from services.edits import apply_edits_to_msp

            apply_edits_to_msp(msp, edits)
        except Exception as exc:  # noqa: BLE001
            log.warning("edits pass failed: %s", exc)

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

    # ---- Phase 4 — dimensions, added holes, notes, bridges ---------------
    dims_added = 0
    if dimensions:
        from services.dimensions import DIMENSION_COLOR, DIMENSION_LAYER

        _ensure_layer(doc, DIMENSION_LAYER, DIMENSION_COLOR)
        for d in dimensions:
            try:
                _add_dimension(msp, d)
                dims_added += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("skipping malformed dimension: %s", exc)

    holes_added = 0
    if added_holes:
        from services.added_holes import HOLE_COLOR, HOLE_LAYER, HOLE_TAP_TEXT_HEIGHT

        _ensure_layer(doc, HOLE_LAYER, HOLE_COLOR)
        for h in added_holes:
            try:
                c = h.get("center") or []
                r = float(h.get("radius") or 0.0)
                if r <= 0 or len(c) < 2:
                    continue
                cx, cy = float(c[0]), float(c[1])
                msp.add_circle(
                    (cx, cy),
                    r,
                    dxfattribs={"layer": HOLE_LAYER, "color": HOLE_COLOR},
                )
                tap = h.get("tap_note")
                if tap:
                    msp.add_mtext(
                        str(tap),
                        dxfattribs={
                            "layer": HOLE_LAYER,
                            "color": HOLE_COLOR,
                            "char_height": HOLE_TAP_TEXT_HEIGHT,
                            "insert": (cx + r + 1.0, cy + r + 1.0),
                        },
                    )
                holes_added += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("skipping malformed added hole: %s", exc)

    notes_count = 0
    if notes:
        for n in notes:
            try:
                pos = n.get("position") or []
                text = str(n.get("text") or "")
                if not text or len(pos) < 2:
                    continue
                layer = str(n.get("layer") or "CUTFLOW_NOTE")
                color = int(n.get("color") or 7)
                _ensure_layer(doc, layer, color)
                msp.add_mtext(
                    text,
                    dxfattribs={
                        "layer": layer,
                        "color": color,
                        "char_height": float(n.get("height") or 2.5),
                        "rotation": float(n.get("rotation") or 0.0),
                        "insert": (float(pos[0]), float(pos[1])),
                    },
                )
                notes_count += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("skipping malformed note: %s", exc)

    bridges_added = 0
    if bridges:
        from services.bridges import BRIDGE_COLOR, BRIDGE_LAYER

        _ensure_layer(doc, BRIDGE_LAYER, BRIDGE_COLOR)
        for br in bridges:
            try:
                s = br.get("start") or []
                e = br.get("end") or []
                if len(s) < 2 or len(e) < 2:
                    continue
                msp.add_line(
                    (float(s[0]), float(s[1])),
                    (float(e[0]), float(e[1])),
                    dxfattribs={"layer": BRIDGE_LAYER, "color": BRIDGE_COLOR},
                )
                bridges_added += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("skipping malformed bridge: %s", exc)

    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(dest))
    log.info(
        "exported %s (removed %d, +%d poly, +%d notes, +%d dims, +%d holes, "
        "+%d preset-notes, +%d bridges) — total now %d entities",
        dest,
        len(to_remove),
        added,
        notes_added,
        dims_added,
        holes_added,
        notes_count,
        bridges_added,
        sum(1 for _ in msp),
    )
    return len(to_remove)


def _add_dimension(msp, d: dict) -> None:
    """Dispatch one dimension descriptor onto the right ezdxf factory.

    ezdxf's dim factories return a ``DimensionRenderer`` that we must
    call ``.render()`` on for the dim to appear in modelspace.

    M3 / M4 — placement geometry:

    * ``linear``  — base is offset along the perpendicular of (p1, p2) so
                    horizontal AND vertical measurements look correct.
                    Falls back to a horizontal offset when p1 == p2.
    * ``aligned`` — uses ezdxf's distance arg verbatim (it handles the
                    perpendicular calculation internally).
    * ``radius`` / ``diameter`` — the leader angle points from centre
                    (p1) to the arc point (p2); previously hard-coded
                    to 0° which made every dim line point east.
    """

    import math

    from services.dimensions import DIMENSION_COLOR, DIMENSION_LAYER

    p1 = d.get("p1") or []
    p2 = d.get("p2") or []
    if len(p1) < 2 or len(p2) < 2:
        return
    dtype = str(d.get("type") or "linear")
    text_override = d.get("text_override")
    text = str(text_override) if text_override else "<>"
    offset = float(d.get("offset") or 8.0)
    attribs = {"layer": DIMENSION_LAYER, "color": DIMENSION_COLOR}

    p1t = (float(p1[0]), float(p1[1]))
    p2t = (float(p2[0]), float(p2[1]))

    if dtype == "linear":
        # M3: offset perpendicular to the segment so vertical / diagonal
        # measurements don't render with the dim line on top of the data.
        dx, dy = p2t[0] - p1t[0], p2t[1] - p1t[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            nx, ny = 0.0, 1.0
        else:
            nx, ny = -dy / length, dx / length
        midx = (p1t[0] + p2t[0]) / 2.0
        midy = (p1t[1] + p2t[1]) / 2.0
        base = (midx + nx * offset, midy + ny * offset)
        dim = msp.add_linear_dim(
            base=base,
            p1=p1t,
            p2=p2t,
            text=text,
            dxfattribs=attribs,
        )
        dim.render()
    elif dtype == "aligned":
        dim = msp.add_aligned_dim(
            p1=p1t, p2=p2t, distance=offset, text=text, dxfattribs=attribs
        )
        dim.render()
    elif dtype == "radius":
        # p1 = centre, p2 = arc point. M4: leader points from centre
        # toward the arc point so the user sees the dim arrow on the arc.
        radius = math.hypot(p2t[0] - p1t[0], p2t[1] - p1t[1])
        angle = math.degrees(math.atan2(p2t[1] - p1t[1], p2t[0] - p1t[0]))
        dim = msp.add_radius_dim(
            center=p1t,
            radius=radius,
            angle=angle,
            text=text,
            dxfattribs=attribs,
        )
        dim.render()
    elif dtype == "diameter":
        radius = math.hypot(p2t[0] - p1t[0], p2t[1] - p1t[1])
        angle = math.degrees(math.atan2(p2t[1] - p1t[1], p2t[0] - p1t[0]))
        dim = msp.add_diameter_dim(
            center=p1t,
            radius=radius,
            angle=angle,
            text=text,
            dxfattribs=attribs,
        )
        dim.render()


def _ensure_layer(doc, name: str, color: int) -> None:
    """Create the layer if needed; set colour to keep DXF readers happy."""

    if name in doc.layers:
        return
    try:
        doc.layers.add(name=name, color=color)
    except Exception:  # noqa: BLE001 - duplicate / readonly layer names
        pass
