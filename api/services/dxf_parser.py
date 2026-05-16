"""Convert a DXF document into the JSON shape consumed by the SVG canvas.

Coordinates are kept in DXF space (Y up). The frontend handles axis flipping
based on the returned bounding box.

Entity IDs are deterministic strings (``e{index}``) assigned in modelspace
iteration order, which lets the delete/export round trip use the same IDs
without persisting anything extra.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox as ezdxf_bbox
from ezdxf import recover
from ezdxf.document import Drawing
from ezdxf.entities import DXFEntity

from services.classifier import classify_entities
from models.schemas import (
    BoundingBox,
    DeleteCandidates,
    EntityOut,
    FileEntities,
    Stats,
)

log = logging.getLogger(__name__)

# Entity types we currently know how to render. Unknown types still surface
# as ``other`` with empty geom so the frontend can show counts.
_SUPPORTED_TYPES = {
    "LINE",
    "CIRCLE",
    "ARC",
    "LWPOLYLINE",
    "POLYLINE",
    "ELLIPSE",
    "SPLINE",
    "TEXT",
    "MTEXT",
    "INSERT",
    "DIMENSION",
    "LEADER",
    "HATCH",
    "POINT",
    "SOLID",
}


def load_document(path: Path | str) -> Drawing:
    """Read a DXF file with ezdxf's tolerant recovery loader."""

    doc, _auditor = recover.readfile(str(path))
    return doc


# Tiny in-process cache keyed on (path, mtime_ns, size) so a re-fetch of the
# same DXF (after every delete reservation, and on tab switch) skips the
# ezdxf re-parse. Capped at 8 entries — even a 50-file session stays bounded
# because the LRU is approximated via insertion-order eviction. The cached
# payload has per-request fields cleared so cache hits never leak stale
# file_id/deleted_ids to other callers (M2).
_PARSE_CACHE: dict[tuple[str, int, int, tuple[str, ...]], FileEntities] = {}
_PARSE_CACHE_MAX = 8


def parse_file(
    path: Path | str,
    file_id: str,
    name: str,
    outer_ids: list[str] | None = None,
) -> FileEntities:
    """End-to-end: read DXF, build per-entity dicts, classify, return payload.

    Cached by ``(path, mtime, size, outer_ids)`` — the second call for an
    unchanged file with the same confirmed outer-loop is near-instant. The
    ``outer_ids`` argument (H1) is forwarded to ``classify_entities`` so
    re-classification after detection cannot demote any confirmed outer
    entity into the FRAME bucket.
    """

    p = Path(path)
    outer_tuple: tuple[str, ...] = tuple(sorted(outer_ids or ()))
    try:
        st = p.stat()
        key: tuple[str, int, int, tuple[str, ...]] | None = (
            str(p), st.st_mtime_ns, st.st_size, outer_tuple,
        )
    except OSError:
        key = None

    if key is not None and key in _PARSE_CACHE:
        cached = _PARSE_CACHE[key]
        # Refresh per-request fields so the cached entity payload is reused
        # but the request-bound metadata reflects this caller.
        return cached.model_copy(update={"file_id": file_id, "name": name, "deleted_ids": []})

    payload = _do_parse(path, file_id, name, outer_ids=outer_ids)
    if key is not None:
        cacheable = payload.model_copy(update={"file_id": "", "name": p.name, "deleted_ids": []})
        _PARSE_CACHE[key] = cacheable
        while len(_PARSE_CACHE) > _PARSE_CACHE_MAX:
            _PARSE_CACHE.pop(next(iter(_PARSE_CACHE)))
    return payload


def _do_parse(
    path: Path | str,
    file_id: str,
    name: str,
    outer_ids: list[str] | None = None,
) -> FileEntities:
    """The real parse implementation (was the body of ``parse_file``)."""

    doc = load_document(path)
    msp = doc.modelspace()

    entities: list[EntityOut] = []
    raw_for_classifier: list[tuple[str, DXFEntity, dict[str, Any]]] = []

    for idx, e in enumerate(msp):
        eid = f"e{idx:05d}"
        geom = _geom_for(e, doc)
        ent = EntityOut(
            id=eid,
            type=e.dxftype(),
            category="other",
            color=int(getattr(e.dxf, "color", 256) or 256),
            layer=str(getattr(e.dxf, "layer", "0")),
            geom=geom,
        )
        entities.append(ent)
        raw_for_classifier.append((eid, e, geom))

    # Run the (heuristic) classifier and patch categories in place.
    categories, candidates = classify_entities(raw_for_classifier, doc, outer_ids=outer_ids)
    for ent in entities:
        ent.category = categories.get(ent.id, "other")

    bbox_obj = _safe_bbox(msp)
    stats = _build_stats(entities)
    units = _units_label(doc)

    return FileEntities(
        file_id=file_id,
        name=name,
        bounding_box=bbox_obj,
        entities=entities,
        delete_candidates=DeleteCandidates(**candidates),
        stats=stats,
        units=units,
        deleted_ids=[],
    )


# ---------------------------------------------------------------------------
# Per-entity geometry serialisers
# ---------------------------------------------------------------------------


def _geom_for(e: DXFEntity, doc: Drawing) -> dict[str, Any]:
    t = e.dxftype()
    try:
        if t == "LINE":
            s, ed = e.dxf.start, e.dxf.end
            return {"x1": float(s.x), "y1": float(s.y), "x2": float(ed.x), "y2": float(ed.y)}

        if t == "CIRCLE":
            c = e.dxf.center
            return {"cx": float(c.x), "cy": float(c.y), "r": float(e.dxf.radius)}

        if t == "ARC":
            c = e.dxf.center
            return {
                "cx": float(c.x),
                "cy": float(c.y),
                "r": float(e.dxf.radius),
                "start_angle": float(e.dxf.start_angle),
                "end_angle": float(e.dxf.end_angle),
            }

        if t == "LWPOLYLINE":
            # Capture bulge per vertex so the frontend can draw arcs between
            # consecutive vertices. ``get_points("xyb")`` returns (x, y, bulge);
            # bulge = tan(included_angle / 4), 0 = straight line, sign = sweep
            # direction (positive = CCW in DXF space). Storing as a plain
            # 3-tuple keeps the JSON wire format compact and JSON-clean.
            pts: list[list[float]] = []
            try:
                for p in e.get_points("xyb"):
                    pts.append([float(p[0]), float(p[1]), float(p[2])])
            except Exception:  # noqa: BLE001 - rare ezdxf quirks → straight-only
                pts = [[float(p[0]), float(p[1]), 0.0] for p in e.get_points("xy")]
            return {"vertices": pts, "closed": bool(e.closed)}

        if t == "POLYLINE":
            verts: list[list[float]] = []
            for v in e.vertices:
                loc = v.dxf.location
                bulge = float(getattr(v.dxf, "bulge", 0.0) or 0.0)
                verts.append([float(loc.x), float(loc.y), bulge])
            return {"vertices": verts, "closed": bool(e.is_closed)}

        if t == "ELLIPSE":
            c = e.dxf.center
            ma = e.dxf.major_axis
            return {
                "cx": float(c.x),
                "cy": float(c.y),
                "major_x": float(ma.x),
                "major_y": float(ma.y),
                "ratio": float(e.dxf.ratio),
                "start_param": float(e.dxf.start_param),
                "end_param": float(e.dxf.end_param),
            }

        if t == "SPLINE":
            ctrl = [[float(p.x), float(p.y)] for p in e.control_points]
            return {"control_points": ctrl, "degree": int(e.dxf.degree)}

        if t == "TEXT":
            ip = e.dxf.insert
            return {
                "x": float(ip.x),
                "y": float(ip.y),
                "text": str(e.dxf.text),
                "height": float(getattr(e.dxf, "height", 0.0)),
                "rotation": float(getattr(e.dxf, "rotation", 0.0)),
            }

        if t == "MTEXT":
            ip = e.dxf.insert
            try:
                txt = e.plain_text()
            except Exception:  # noqa: BLE001 - encoded glyphs etc.
                txt = str(getattr(e, "text", ""))
            return {
                "x": float(ip.x),
                "y": float(ip.y),
                "text": txt,
                "height": float(getattr(e.dxf, "char_height", 0.0)),
                "rotation": float(getattr(e.dxf, "rotation", 0.0)),
            }

        if t == "INSERT":
            ip = e.dxf.insert
            return {
                "x": float(ip.x),
                "y": float(ip.y),
                "name": str(e.dxf.name),
                "rotation": float(getattr(e.dxf, "rotation", 0.0)),
                "xscale": float(getattr(e.dxf, "xscale", 1.0)),
                "yscale": float(getattr(e.dxf, "yscale", 1.0)),
            }

        if t == "DIMENSION":
            anchors: list[list[float]] = []
            for a in ("defpoint", "defpoint2", "defpoint3", "defpoint4", "defpoint5", "text_midpoint"):
                pt = getattr(e.dxf, a, None)
                if pt is not None:
                    anchors.append([float(pt.x), float(pt.y)])
            # AutoCAD stores ``<>`` (literally) for "use the measured value",
            # and an empty string when the user wants the default rendering.
            # Either way we resolve to a number so the frontend can show
            # something readable instead of "<>".
            text = str(getattr(e.dxf, "text", "") or "")
            if not text or text == "<>":
                meas = getattr(e.dxf, "actual_measurement", None)
                if meas is None:
                    try:
                        meas = e.get_measurement()  # type: ignore[attr-defined]
                    except Exception:  # noqa: BLE001 - some DIMs don't support it
                        meas = None
                if isinstance(meas, (int, float)):
                    text = f"{float(meas):.1f}"
                else:
                    text = "(寸法)"
            return {"anchors": anchors, "text": text, "dimtype": int(getattr(e.dxf, "dimtype", 0))}

        if t == "LEADER":
            verts = [[float(v.x), float(v.y)] for v in e.vertices]
            return {"vertices": verts}

        if t == "HATCH":
            paths: list[list[list[float]]] = []
            for p in e.paths:
                # Best-effort path discretisation; we only render hatches faintly.
                vs = []
                try:
                    for v in p.vertices:  # polyline path
                        vs.append([float(v[0]), float(v[1])])
                except Exception:  # noqa: BLE001 - edge paths fall back to bbox below
                    pass
                if vs:
                    paths.append(vs)
            return {"paths": paths, "pattern": str(getattr(e.dxf, "pattern_name", "SOLID"))}

        if t == "POINT":
            p = e.dxf.location
            return {"x": float(p.x), "y": float(p.y)}

        if t == "SOLID":
            return {
                "vertices": [
                    [float(getattr(e.dxf, f"vtx{i}").x), float(getattr(e.dxf, f"vtx{i}").y)]
                    for i in range(4)
                    if getattr(e.dxf, f"vtx{i}", None) is not None
                ]
            }

    except Exception as exc:  # noqa: BLE001 - skip malformed entities, keep parsing.
        log.debug("geom extraction failed for %s: %s", t, exc)

    return {}


# ---------------------------------------------------------------------------
# Stats / bbox helpers
# ---------------------------------------------------------------------------


def _safe_bbox(msp: Any) -> BoundingBox:
    """Compute the modelspace bounding box, falling back to (0,0)-(1,1)."""

    try:
        ext = ezdxf_bbox.extents(msp)
        if ext.has_data:
            mn, mx = ext.extmin, ext.extmax
            return BoundingBox(
                min_x=float(mn.x), min_y=float(mn.y), max_x=float(mx.x), max_y=float(mx.y)
            )
    except Exception as exc:  # noqa: BLE001 - bbox can throw on degenerate content
        log.debug("bbox extents failed: %s", exc)
    return BoundingBox(min_x=0.0, min_y=0.0, max_x=1.0, max_y=1.0)


def _build_stats(entities: list[EntityOut]) -> Stats:
    by_cat: dict[str, int] = {}
    for ent in entities:
        by_cat[ent.category] = by_cat.get(ent.category, 0) + 1
    return Stats(total=len(entities), by_category=by_cat)


def _units_label(doc: Drawing) -> str:
    """Map DXF $INSUNITS to a short string. Default to ``mm``."""

    code = int(doc.header.get("$INSUNITS", 4))
    return {
        0: "unitless",
        1: "in",
        2: "ft",
        4: "mm",
        5: "cm",
        6: "m",
    }.get(code, "mm")


def is_supported(entity_type: str) -> bool:
    return entity_type in _SUPPORTED_TYPES


def angular_distance(a: float, b: float) -> float:
    """Smallest signed angle diff in degrees; useful for ARC sanity checks."""

    d = (a - b + 180.0) % 360.0 - 180.0
    return abs(d)


def euclid(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])
