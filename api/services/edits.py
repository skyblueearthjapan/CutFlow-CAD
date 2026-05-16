"""線編集 (頂点移動) — persistence + DXF entity mutation helper.

The endpoint just records ``(entity_id, vertex_index, new_position)``
tuples; the actual ezdxf mutation happens at export time so we can keep
the original DXF intact and let users iterate freely.

We pre-validate edits against the parsed FileEntities payload so a stale
client cannot land out-of-range vertex indices in storage.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

log = logging.getLogger(__name__)


def validate_edits(
    edits: Iterable[dict[str, Any]],
    entities_by_id: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(valid, errors)`` for the supplied edits.

    ``entities_by_id`` maps ``entity_id`` → ``EntityOut`` (or dict with
    ``type`` and ``geom``). An edit is valid when:

    * the entity exists,
    * the entity type is in {LINE, LWPOLYLINE, POLYLINE},
    * vertex_index is in range for that type.
    """

    valid: list[dict[str, Any]] = []
    errors: list[str] = []
    for e in edits:
        eid = str(e.get("entity_id") or "")
        ent = entities_by_id.get(eid)
        if ent is None:
            errors.append(f"未知の entity_id: {eid}")
            continue
        # Support both Pydantic EntityOut and plain dict.
        etype = getattr(ent, "type", None) or (ent.get("type") if isinstance(ent, dict) else None)
        geom = getattr(ent, "geom", None) or (ent.get("geom") if isinstance(ent, dict) else {})
        vi = int(e.get("vertex_index") or 0)

        if etype == "LINE":
            if vi not in (0, 1):
                errors.append(f"{eid}: LINE は vertex_index ∈ {{0,1}}")
                continue
        elif etype in ("LWPOLYLINE", "POLYLINE"):
            n = len((geom or {}).get("vertices") or [])
            if vi < 0 or vi >= n:
                errors.append(f"{eid}: vertex_index {vi} が範囲外 (0..{n - 1})")
                continue
        else:
            errors.append(f"{eid}: type={etype} は頂点編集をサポートしません")
            continue

        np = e.get("new_position") or []
        if len(np) < 2:
            errors.append(f"{eid}: new_position が不正")
            continue

        valid.append(
            {
                "entity_id": eid,
                "vertex_index": vi,
                "new_position": [float(np[0]), float(np[1])],
            }
        )
    return valid, errors


def apply_edits_to_msp(msp, edits: Iterable[dict[str, Any]]) -> int:
    """Apply persisted edits to a live ezdxf modelspace.

    Returns the number of edits actually applied. Silent on unknown ids
    (the writer can't see them and we don't want to abort export over
    one stale entry). Each skip is logged at warning level so the
    operator can see them in api server output (M6).
    """

    # Build an index entity_id → live ezdxf entity. The deterministic
    # ``e{idx:05d}`` numbering matches dxf_parser._do_parse.
    by_id = {}
    for idx, ent in enumerate(msp):
        by_id[f"e{idx:05d}"] = ent

    applied = 0
    for ed in edits:
        eid = str(ed.get("entity_id") or "")
        ent = by_id.get(eid)
        if ent is None:
            log.warning("vertex edit skipped: unknown entity_id %s", eid)
            continue
        vi = int(ed.get("vertex_index") or 0)
        np = ed.get("new_position") or []
        if len(np) < 2:
            continue
        nx, ny = float(np[0]), float(np[1])

        try:
            t = ent.dxftype()
            if t == "LINE":
                if vi == 0:
                    ent.dxf.start = (nx, ny, getattr(ent.dxf.start, "z", 0.0))
                elif vi == 1:
                    ent.dxf.end = (nx, ny, getattr(ent.dxf.end, "z", 0.0))
                applied += 1
            elif t == "LWPOLYLINE":
                # M1: keep start/end width AND bulge so the export does
                # not silently flatten a wider polyline into a hairline.
                pts = list(ent.get_points("xyseb"))
                if 0 <= vi < len(pts):
                    old = pts[vi]
                    sw = old[2] if len(old) > 2 else 0.0
                    ew = old[3] if len(old) > 3 else 0.0
                    bulge = old[4] if len(old) > 4 else 0.0
                    pts[vi] = (nx, ny, sw, ew, bulge)
                    ent.set_points(pts, format="xyseb")
                    applied += 1
            elif t == "POLYLINE":
                verts = list(ent.vertices)
                if 0 <= vi < len(verts):
                    v = verts[vi]
                    v.dxf.location = (nx, ny, getattr(v.dxf.location, "z", 0.0))
                    applied += 1
        except Exception as exc:  # noqa: BLE001 — bad edit shouldn't abort export
            log.warning("vertex edit for %s vi=%d failed: %s", eid, vi, exc)
    return applied
