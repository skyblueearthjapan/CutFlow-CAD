"""Per-file endpoints: parse, delete reservation, outer detection, offset, export."""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from models import (
    AddedHole,
    AnnotationsResponse,
    Bridge,
    BridgeAutoRequest,
    BridgeListResponse,
    BridgeRequest,
    ChamferRequest,
    ChamferResponse,
    CornersResponse,
    DeleteRequest,
    DeleteResponse,
    Dimension,
    DimensionListResponse,
    DimensionRequest,
    EditedVertex,
    EditedVertexListResponse,
    EditVertexRequest,
    FileEntities,
    FrameCleanupResponse,
    HoleAddRequest,
    HoleListResponse,
    HolePatternRequest,
    Note,
    NoteListResponse,
    NoteRequest,
    OffsetRequest,
    OffsetResult,
    OuterDetectionResult,
    OuterManualRequest,
    SnapRequest,
    SnapResponse,
)
from services import graph as gmod
from services.added_holes import expand_pattern, holes_dxf_extras
from services.bridges import attach_positions, auto_distribute, bridges_dxf_extras
from services.chamfer import (
    build_annotations,
    chamfer_dxf_extras,
    list_corners,
)
from services.dimensions import dimensions_dxf_extras
from services.dxf_parser import parse_file
from services.dxf_writer import export_clean_dxf
from services.edits import validate_edits
from services.frame_cleanup import detect_frame_entities
from services.notes import notes_dxf_extras
from services.offset import OffsetError, compute_offset
from services.outer_detector import detect_outer, evaluate_manual
from services.pdf_export import export_pdf
from services.snap import find_snap
from services.svg_render import render_dxf_to_svg
from storage import SessionExpired, SessionNotFound, get_store

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/session/{sid}/file/{fid}", tags=["files"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve(sid: str, fid: str):
    try:
        store = get_store()
        sf = store.get_file(sid, fid)
        return store, sf
    except SessionExpired as exc:
        raise HTTPException(status_code=410, detail="session expired") from exc
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    except FileNotFoundError as exc:
        # The session metadata still references this file but the bytes on
        # disk are gone (e.g. tmp wiped between requests). Treat as 404.
        raise HTTPException(status_code=404, detail="file no longer available") from exc


def _items_for_detection(payload: FileEntities) -> list[tuple[str, str, str, dict]]:
    """Reduce a parsed payload to the (eid, type, category, geom) tuples the
    detector consumes."""

    return [(e.id, e.type, e.category, e.geom) for e in payload.entities]


def _collect_outer_polygon_pts(payload: FileEntities, loop: list[str]) -> list[tuple[float, float]]:
    """Return the outer-loop polygon as a flat ``[(x, y), ...]`` ring.

    Used for the H3 point-in-loop check. We sample ARCs as their two
    endpoints — this is a coarse but conservative outline (slightly
    smaller than the real arc), so any hole that passes the check sits
    safely inside the true polygon.
    """

    import math

    by_id = {e.id: e for e in payload.entities}
    out: list[tuple[float, float]] = []
    for eid in loop:
        ent = by_id.get(eid)
        if ent is None:
            continue
        g = ent.geom or {}
        if ent.type == "LINE":
            out.append((float(g.get("x1", 0.0)), float(g.get("y1", 0.0))))
            out.append((float(g.get("x2", 0.0)), float(g.get("y2", 0.0))))
        elif ent.type == "ARC":
            cx, cy, r = float(g.get("cx", 0.0)), float(g.get("cy", 0.0)), float(g.get("r", 0.0))
            sa = math.radians(float(g.get("start_angle", 0.0)))
            ea = math.radians(float(g.get("end_angle", 0.0)))
            out.append((cx + r * math.cos(sa), cy + r * math.sin(sa)))
            out.append((cx + r * math.cos(ea), cy + r * math.sin(ea)))
        elif ent.type == "CIRCLE":
            cx, cy, r = float(g.get("cx", 0.0)), float(g.get("cy", 0.0)), float(g.get("r", 0.0))
            if r > 0:
                # Sample the circle as a 32-gon so the ray-cast test sees
                # a real polygon (a 2-point ring degenerates to a line).
                segs = 32
                for k in range(segs):
                    theta = 2 * math.pi * k / segs
                    out.append((cx + r * math.cos(theta), cy + r * math.sin(theta)))
        elif ent.type in ("LWPOLYLINE", "POLYLINE"):
            for v in g.get("vertices") or []:
                out.append((float(v[0]), float(v[1])))
    return out


def _point_in_polygon(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    """Standard ray-casting; returns True for points strictly inside the
    polygon (and may give either result on edge cases — fine for the
    operator-warning use case)."""

    n = len(ring)
    if n < 3:
        return True  # No usable ring → don't block.
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def _filter_holes_inside_outer(
    holes: list[dict],
    payload: FileEntities,
    loop: list[str],
) -> tuple[list[dict], int]:
    """Return ``(kept, dropped_count)`` filtering holes outside the outer.

    A hole is "outside" when its centre fails the polygon-in test against
    the ring sampled from the loop. If we cannot derive a usable ring
    (too few points) we keep every hole and report 0 drops.
    """

    ring = _collect_outer_polygon_pts(payload, loop)
    if len(ring) < 3:
        return holes, 0
    kept: list[dict] = []
    dropped = 0
    for h in holes:
        pos = h.get("position") or []
        if len(pos) < 2:
            kept.append(h)
            continue
        if _point_in_polygon(float(pos[0]), float(pos[1]), ring):
            kept.append(h)
        else:
            dropped += 1
    return kept, dropped


def _build_topo(payload: FileEntities, loop: list[str]) -> gmod.TopoGraph:
    """Re-derive the topology graph for the offset stage from the live payload."""

    edge_items: list[tuple[str, str, dict]] = []
    for e in payload.entities:
        if e.type not in {"LINE", "ARC", "LWPOLYLINE", "POLYLINE", "CIRCLE"}:
            continue
        edge_items.append((e.id, e.type, e.geom))
    topo = gmod.build_graph(edge_items)
    # Ensure every loop edge exists in the graph; if not, raise.
    missing = [eid for eid in loop if eid not in topo.edges]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"外径ループに存在しないエンティティが含まれています: {missing[:5]}",
        )
    return topo


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=FileEntities)
async def get_file_entities(sid: str, fid: str) -> FileEntities:
    """Parse the DXF and return JSON entities + delete candidates."""

    store, sf = _resolve(sid, fid)
    # Read the confirmed outer-loop first so the classifier never re-routes
    # any of those entities into the FRAME bucket on re-parse (H1).
    saved = store.read_outer(sid, fid)
    outer_ids = list(saved.get("loop") or []) if saved else None
    try:
        payload = parse_file(sf.path, file_id=fid, name=sf.name, outer_ids=outer_ids)
    except Exception as exc:  # noqa: BLE001 - DXF parsing has many failure modes
        log.exception("parse failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}") from exc

    # Apply confirmed outer-loop category overlay if present so the frontend
    # paints those entities in cyan immediately.
    if saved and saved.get("loop"):
        loop_ids = set(saved["loop"])
        for e in payload.entities:
            if e.id in loop_ids:
                e.category = "outer"

    payload.deleted_ids = store.get_deleted_for_file(sid, fid)
    return payload


@router.post("/delete", response_model=DeleteResponse)
async def post_delete(sid: str, fid: str, body: DeleteRequest) -> DeleteResponse:
    """Reserve the given entity IDs for removal at export time.

    Unknown entity IDs are silently ignored so a stale client cannot get
    stuck in a 4xx loop — we just don't count them. ``deleted_count``
    reflects the size of the *valid* merged set on disk.
    """

    store, sf = _resolve(sid, fid)

    # Parse once: we need the valid-id set for filtering AND the remaining
    # count for the response, so paying the parse cost twice is wasteful.
    try:
        payload = parse_file(sf.path, file_id=fid, name=sf.name)
    except Exception as exc:  # noqa: BLE001
        log.exception("parse failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}") from exc

    valid_ids = {e.id for e in payload.entities}
    filtered = [eid for eid in body.entity_ids if eid in valid_ids]
    if len(filtered) != len(body.entity_ids):
        dropped = set(body.entity_ids) - valid_ids
        log.info("post_delete: dropped %d unknown id(s) for %s/%s: %s",
                 len(dropped), sid, fid, sorted(dropped)[:10])

    merged = store.update_deleted(sid, fid, filtered)
    # H11: any cached offset is now stale (the geometry it was computed
    # against has changed). Force a recompute on the next call.
    store.invalidate_offset(sid, fid)
    remaining = max(payload.stats.total - len(merged), 0)
    return DeleteResponse(deleted_count=len(merged), remaining=remaining)


@router.post("/detect-outer", response_model=OuterDetectionResult)
async def post_detect_outer(sid: str, fid: str) -> OuterDetectionResult:
    """Run the STEP 1–5 outer-loop detection pipeline.

    Persists the winning loop to ``state/{fid}/outer.json`` so subsequent
    offset / export calls can reuse it without re-running the heuristics.
    """

    store, sf = _resolve(sid, fid)
    try:
        payload = parse_file(sf.path, file_id=fid, name=sf.name)
    except Exception as exc:  # noqa: BLE001
        log.exception("parse failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}") from exc

    try:
        # H7: honour delete reservations so the detector never picks an
        # entity the user has already flagged for removal.
        deleted = store.get_deleted_for_file(sid, fid)
        result = detect_outer(_items_for_detection(payload), delete_ids=deleted)
    except Exception as exc:  # noqa: BLE001
        log.exception("detect_outer failed for %s/%s", sid, fid)
        raise HTTPException(status_code=500, detail=f"detection failed: {exc}") from exc

    # Persist a small payload for downstream callers (offset / export).
    summary = result.get("loop_summary") or {}
    store.write_outer(
        sid,
        fid,
        {
            "loop": list(result.get("outer_loop") or []),
            "confidence": float(result.get("confidence") or 0.0),
            "method": result.get("method", ""),
            "perimeter": float(summary.get("perimeter") or 0.0),
            "area": float(summary.get("area") or 0.0),
            "status": result.get("status", "failed"),
        },
    )
    # H11: invalidate any cached offset — the outer it was computed
    # against has just been replaced.
    store.invalidate_offset(sid, fid)
    return OuterDetectionResult(**result)


@router.post("/outer-manual", response_model=OuterDetectionResult)
async def post_outer_manual(
    sid: str, fid: str, body: OuterManualRequest
) -> OuterDetectionResult:
    """Validate a manually-selected entity chain and persist if it closes."""

    if not body.entity_ids:
        raise HTTPException(status_code=400, detail="entity_ids が空です")

    store, sf = _resolve(sid, fid)
    try:
        payload = parse_file(sf.path, file_id=fid, name=sf.name)
    except Exception as exc:  # noqa: BLE001
        log.exception("parse failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}") from exc

    result = evaluate_manual(_items_for_detection(payload), body.entity_ids)

    # Only persist on success — a failed manual attempt should not clobber
    # an existing confirmed loop.
    if result.get("status") == "success":
        summary = result.get("loop_summary") or {}
        store.write_outer(
            sid,
            fid,
            {
                "loop": list(result.get("outer_loop") or []),
                "confidence": float(result.get("confidence") or 1.0),
                "method": "manual",
                "perimeter": float(summary.get("perimeter") or 0.0),
                "area": float(summary.get("area") or 0.0),
                "status": "success",
            },
        )
        # H11: invalidate cached offset on outer change.
        store.invalidate_offset(sid, fid)
        return OuterDetectionResult(**result)

    # Manual selection that doesn't close → 422 with the warnings attached.
    raise HTTPException(
        status_code=422,
        detail={
            "message": "manual outer loop is not closed",
            "warnings": result.get("warnings") or [],
        },
    )


@router.post("/offset", response_model=OffsetResult)
async def post_offset(sid: str, fid: str, body: OffsetRequest) -> OffsetResult:
    """Compute the outer-offset polygon for the confirmed outer loop."""

    store, sf = _resolve(sid, fid)
    saved = store.read_outer(sid, fid)
    if not saved or not saved.get("loop"):
        raise HTTPException(
            status_code=422,
            detail="先に外径を確定してください (detect-outer / outer-manual)",
        )
    # H6: never offset an unconfirmed outer (low_confidence / failed). The
    # UI must walk the user through manual confirmation first to avoid
    # silently shipping the wrong polygon downstream.
    saved_status = saved.get("status") or ""
    if saved_status not in {"success", ""}:
        raise HTTPException(
            status_code=409,
            detail="外径が未確定です。先に外径を確定 (信頼度 success / 手動) してから加工代を計算してください",
        )

    try:
        payload = parse_file(sf.path, file_id=fid, name=sf.name, outer_ids=list(saved.get("loop") or []))
    except Exception as exc:  # noqa: BLE001
        log.exception("parse failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}") from exc

    loop_ids: list[str] = list(saved["loop"])
    topo = _build_topo(payload, loop_ids)

    try:
        result = compute_offset(
            topo,
            loop_ids,
            default_mm=body.default_mm,
            edge_overrides=body.edge_overrides,
            corner_join=body.corner_join,
        )
    except OffsetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("offset failed for %s/%s", sid, fid)
        raise HTTPException(status_code=500, detail=f"offset failed: {exc}") from exc

    store.write_offset(sid, fid, {"request": body.model_dump(), "result": result})
    return OffsetResult(**result)


@router.get("/outer")
async def get_outer(sid: str, fid: str) -> dict:
    """Return the persisted outer-loop result (or 404 if not detected yet).

    Used by the frontend to rehydrate Phase 2 state when a tab is opened
    after a refresh (M3).
    """

    store, _sf = _resolve(sid, fid)
    saved = store.read_outer(sid, fid)
    if not saved:
        raise HTTPException(status_code=404, detail="外径未検出")
    return saved


@router.get("/offset")
async def get_offset(sid: str, fid: str) -> dict:
    """Return the persisted offset payload (or 404 if not computed yet)."""

    store, _sf = _resolve(sid, fid)
    saved = store.read_offset(sid, fid)
    if not saved:
        raise HTTPException(status_code=404, detail="加工代未計算")
    return saved


@router.get("/render-svg")
async def get_render_svg(
    sid: str,
    fid: str,
    apply_deletions: bool = Query(True),
    apply_edits: bool = Query(False),
    dark_theme: bool = Query(True),
) -> dict:
    """Return a CAD-accurate SVG of the file rendered by ezdxf.

    Phase 6: this is the **background layer** the frontend draws beneath
    its editable overlay. ``apply_deletions`` honours the per-file delete
    reservation (so removed entities never appear); ``apply_edits`` reads
    the persisted Phase 4 vertex edits and bakes them into the live ezdxf
    document before render so the operator's line-edit translations show
    up under their overlay (HIGH-2); ``dark_theme`` flips the colour
    policy to ``MONOCHROME_DARK_BG`` so the SVG is legible on the app's
    dark canvas. The endpoint never mutates state.

    ezdxf's render is CPU-bound and can take 100ms+ on busy DXFs, so we
    dispatch it via ``run_in_threadpool`` to keep the event loop free for
    other requests (HIGH-1).
    """

    store, sf = _resolve(sid, fid)
    exclude_ids: set[str] | None = None
    if apply_deletions:
        deleted = store.get_deleted_for_file(sid, fid)
        if deleted:
            exclude_ids = set(deleted)

    edits: list[dict] | None = None
    if apply_edits:
        data = store.read_phase4(sid, fid, "edits") or {}
        items = data.get("edits") or []
        if items:
            edits = items

    try:
        return await run_in_threadpool(
            render_dxf_to_svg,
            sf.path,
            dark_theme=dark_theme,
            exclude_entity_ids=exclude_ids,
            edits=edits,
        )
    except RuntimeError as exc:
        log.exception("render-svg failed for %s/%s", sid, fid)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/corners", response_model=CornersResponse)
async def get_corners(sid: str, fid: str) -> CornersResponse:
    """Return ``C1..Cn`` corners + ``E1..En`` edges of the confirmed outer loop.

    Requires that the outer loop has been confirmed (``detect-outer`` or
    ``outer-manual`` returned ``success``); otherwise returns 422.
    """

    store, sf = _resolve(sid, fid)
    saved = store.read_outer(sid, fid)
    if not saved or not saved.get("loop"):
        raise HTTPException(
            status_code=422,
            detail="先に外径を確定してください (detect-outer / outer-manual)",
        )

    try:
        payload = parse_file(sf.path, file_id=fid, name=sf.name, outer_ids=list(saved.get("loop") or []))
    except Exception as exc:  # noqa: BLE001
        log.exception("parse failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}") from exc

    loop_ids: list[str] = list(saved["loop"])
    topo = _build_topo(payload, loop_ids)
    corners, edges = list_corners(topo, loop_ids)
    return CornersResponse(corners=corners, edges=edges)


@router.post("/chamfer", response_model=ChamferResponse)
async def post_chamfer(sid: str, fid: str, body: ChamferRequest) -> ChamferResponse:
    """Persist chamfer / bevel specs and return canvas annotations.

    ``body.specs[]`` is the full replacement list (last write wins) — the
    UI sends the entire current state on every change so we don't have to
    reconcile partial diffs server-side.
    """

    store, sf = _resolve(sid, fid)
    saved = store.read_outer(sid, fid)
    if not saved or not saved.get("loop"):
        raise HTTPException(
            status_code=422,
            detail="先に外径を確定してください (detect-outer / outer-manual)",
        )

    specs = [s.model_dump() for s in body.specs]

    # Validate corner IDs against the confirmed loop.
    try:
        payload = parse_file(sf.path, file_id=fid, name=sf.name, outer_ids=list(saved["loop"]))
    except Exception as exc:  # noqa: BLE001
        log.exception("parse failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}") from exc
    loop_ids: list[str] = list(saved["loop"])
    topo = _build_topo(payload, loop_ids)
    corners, edges = list_corners(topo, loop_ids)
    valid_ids = {c["corner_id"] for c in corners} | {e["edge_id"] for e in edges}

    unknown = [s for s in specs if s.get("corner_id") not in valid_ids]
    if unknown:
        bad = ", ".join(str(s.get("corner_id")) for s in unknown[:5])
        raise HTTPException(
            status_code=422,
            detail=f"未知の corner_id が含まれています: {bad}",
        )

    store.write_chamfer(sid, fid, {"specs": specs})
    items = build_annotations(specs, corners, edges)
    return ChamferResponse(
        specs=body.specs,
        geometry={"type": "annotations", "items": items},
    )


@router.get("/chamfer", response_model=ChamferResponse)
async def get_chamfer(sid: str, fid: str) -> ChamferResponse:
    """Return the persisted chamfer specs (empty if unset)."""

    store, sf = _resolve(sid, fid)
    saved_outer = store.read_outer(sid, fid)
    saved = store.read_chamfer(sid, fid) or {}
    specs = list(saved.get("specs") or [])

    items: list[dict] = []
    if saved_outer and saved_outer.get("loop"):
        try:
            payload = parse_file(
                sf.path, file_id=fid, name=sf.name, outer_ids=list(saved_outer["loop"])
            )
            loop_ids: list[str] = list(saved_outer["loop"])
            topo = _build_topo(payload, loop_ids)
            corners, edges = list_corners(topo, loop_ids)
            items = build_annotations(specs, corners, edges)
        except Exception as exc:  # noqa: BLE001 - annotations are advisory; never block read
            log.warning("chamfer annotations rebuild failed for %s/%s: %s", sid, fid, exc)

    return ChamferResponse(specs=specs, geometry={"type": "annotations", "items": items})


@router.post("/cleanup-frame", response_model=FrameCleanupResponse)
async def post_cleanup_frame(sid: str, fid: str) -> FrameCleanupResponse:
    """Detect the production frame / title block and reserve them for delete.

    The classifier's FRAME bucket already covers the dominant case; we
    add any axis-aligned ISO-aspect rectangle that visually wraps the
    drawing (split-view drawings). The selected IDs are appended to the
    per-file delete reservation so a subsequent export drops them.
    """

    store, sf = _resolve(sid, fid)
    try:
        payload = parse_file(sf.path, file_id=fid, name=sf.name)
    except Exception as exc:  # noqa: BLE001
        log.exception("parse failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}") from exc

    saved_outer = store.read_outer(sid, fid)
    outer_loop = list(saved_outer.get("loop") or []) if saved_outer else None
    frame_ids = detect_frame_entities(payload, outer_loop=outer_loop)
    if frame_ids:
        store.update_deleted(sid, fid, frame_ids)
        store.invalidate_offset(sid, fid)
    return FrameCleanupResponse(
        removed_count=len(frame_ids),
        frame_entity_ids=frame_ids,
    )


# ---------------------------------------------------------------------------
# Phase 4 — drawing tools (dimensions / edits / holes / notes / bridges)
# ---------------------------------------------------------------------------


@router.post("/dimensions", response_model=DimensionListResponse)
async def post_dimensions(
    sid: str, fid: str, body: DimensionRequest
) -> DimensionListResponse:
    """Persist the full dimension list for the file (last-write-wins)."""

    store, _sf = _resolve(sid, fid)
    payload = {"dimensions": [d.model_dump() for d in body.dimensions]}
    store.write_phase4(sid, fid, "dimensions", payload)
    return DimensionListResponse(dimensions=body.dimensions)


@router.get("/dimensions", response_model=DimensionListResponse)
async def get_dimensions(sid: str, fid: str) -> DimensionListResponse:
    store, _sf = _resolve(sid, fid)
    data = store.read_phase4(sid, fid, "dimensions") or {}
    items = [Dimension(**d) for d in (data.get("dimensions") or [])]
    return DimensionListResponse(dimensions=items)


@router.delete("/dimensions/{dim_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dimension(sid: str, fid: str, dim_id: str) -> None:
    store, _sf = _resolve(sid, fid)
    removed = store.delete_phase4_item(sid, fid, "dimensions", dim_id)
    if not removed:
        raise HTTPException(status_code=404, detail="dimension id not found")
    return None


@router.post("/edit-vertex", response_model=EditedVertexListResponse)
async def post_edit_vertex(
    sid: str, fid: str, body: EditVertexRequest
) -> EditedVertexListResponse:
    """Validate vertex edits against the parsed entities and persist.

    Validation surfaces a 422 with all errors at once so the UI can show
    them in a single toast rather than play whack-a-mole.
    """

    store, sf = _resolve(sid, fid)
    try:
        parsed = parse_file(sf.path, file_id=fid, name=sf.name)
    except Exception as exc:  # noqa: BLE001
        log.exception("parse failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}") from exc

    by_id = {e.id: e for e in parsed.entities}
    raw = [ed.model_dump() for ed in body.edits]
    valid, errors = validate_edits(raw, by_id)
    if errors and not valid:
        raise HTTPException(status_code=422, detail={"errors": errors})

    # Merge with any existing edits, keyed by (entity_id, vertex_index)
    # so a re-edit of the same vertex replaces (not appends) the previous
    # position. This mirrors how a CAD operator expects "move vertex".
    existing = store.read_phase4(sid, fid, "edits") or {}
    by_key: dict[tuple[str, int], dict] = {}
    for ed in existing.get("edits") or []:
        by_key[(str(ed.get("entity_id")), int(ed.get("vertex_index") or 0))] = ed
    for v in valid:
        by_key[(v["entity_id"], v["vertex_index"])] = v
    merged = list(by_key.values())
    store.write_phase4(sid, fid, "edits", {"edits": merged})

    edits_out = [EditedVertex(**e) for e in merged]
    return EditedVertexListResponse(edits=edits_out)


@router.get("/edits", response_model=EditedVertexListResponse)
async def get_edits(sid: str, fid: str) -> EditedVertexListResponse:
    store, _sf = _resolve(sid, fid)
    data = store.read_phase4(sid, fid, "edits") or {}
    items = [EditedVertex(**e) for e in (data.get("edits") or [])]
    return EditedVertexListResponse(edits=items)


@router.post("/snap", response_model=SnapResponse)
async def post_snap(sid: str, fid: str, body: SnapRequest) -> SnapResponse:
    """Return the closest snap point near ``body.position`` (or empty result)."""

    store, sf = _resolve(sid, fid)
    try:
        parsed = parse_file(sf.path, file_id=fid, name=sf.name)
    except Exception as exc:  # noqa: BLE001
        log.exception("parse failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}") from exc

    hit = find_snap(
        (float(body.position[0]), float(body.position[1])),
        parsed.entities,
        body.snap_types,
        body.tolerance,
    )
    if hit is None:
        # Empty SnapResponse is intentional — caller checks ``snapped``.
        return SnapResponse()
    return SnapResponse(**hit)


@router.post("/holes", response_model=HoleListResponse)
async def post_holes(sid: str, fid: str, body: HoleAddRequest) -> HoleListResponse:
    """Append holes to the per-file added-holes list.

    POST semantics: ``holes`` are appended (and deduplicated by ``id``),
    matching how an operator iteratively adds taps without re-sending
    every previous hole. To replace the list call ``DELETE`` first or
    POST with a fresh ``id`` strategy.

    H3: any hole whose centre falls *outside* the confirmed outer loop
    is rejected (422) so the operator doesn't accidentally land a tap on
    scrap material. The check is skipped when no outer is confirmed.
    """

    store, sf = _resolve(sid, fid)
    new_holes = [h.model_dump() for h in body.holes]
    saved_outer = store.read_outer(sid, fid)
    if saved_outer and saved_outer.get("loop") and new_holes:
        try:
            parsed_for_inside = parse_file(
                sf.path,
                file_id=fid,
                name=sf.name,
                outer_ids=list(saved_outer["loop"]),
            )
            filtered, dropped = _filter_holes_inside_outer(
                new_holes, parsed_for_inside, list(saved_outer["loop"])
            )
            if dropped:
                raise HTTPException(
                    status_code=422,
                    detail=f"{dropped} 件の穴が外径の外側にあります",
                )
            new_holes = filtered
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "hole inside-loop check failed for %s/%s: %s", sid, fid, exc
            )
    existing = store.read_phase4(sid, fid, "added_holes") or {}
    by_id = {str(h.get("id")): h for h in (existing.get("added_holes") or [])}
    for h in new_holes:
        by_id[str(h.get("id"))] = h
    payload = {"added_holes": list(by_id.values())}
    store.write_phase4(sid, fid, "added_holes", payload)
    return HoleListResponse(holes=[AddedHole(**h) for h in payload["added_holes"]])


@router.get("/holes", response_model=HoleListResponse)
async def get_holes(sid: str, fid: str) -> HoleListResponse:
    store, _sf = _resolve(sid, fid)
    data = store.read_phase4(sid, fid, "added_holes") or {}
    items = [AddedHole(**h) for h in (data.get("added_holes") or [])]
    return HoleListResponse(holes=items)


@router.post("/holes/pattern", response_model=HoleListResponse)
async def post_holes_pattern(
    sid: str, fid: str, body: HolePatternRequest
) -> HoleListResponse:
    """Expand a rectangular pattern and append the resulting holes.

    H3: each pattern hole is checked against the confirmed outer (when
    available) and skipped with a warning if it falls outside — this
    prevents an accidental cuff-of-screws landing in scrap material.
    """

    store, sf = _resolve(sid, fid)
    new_holes = expand_pattern(
        body.anchor,
        body.rows,
        body.cols,
        body.spacing,
        body.diameter,
        tap_note=body.tap_note,
    )
    # H3: filter outside-loop holes when the outer is confirmed.
    saved_outer = store.read_outer(sid, fid)
    if saved_outer and saved_outer.get("loop"):
        try:
            parsed_for_inside = parse_file(
                sf.path,
                file_id=fid,
                name=sf.name,
                outer_ids=list(saved_outer["loop"]),
            )
            filtered, dropped = _filter_holes_inside_outer(
                new_holes, parsed_for_inside, list(saved_outer["loop"])
            )
            if dropped:
                raise HTTPException(
                    status_code=422,
                    detail=f"{dropped} 件の穴が外径の外側にあるためスキップしました",
                )
            new_holes = filtered
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — never block on inside check
            log.warning(
                "hole inside-loop check failed for %s/%s: %s", sid, fid, exc
            )
    existing = store.read_phase4(sid, fid, "added_holes") or {}
    items = list(existing.get("added_holes") or []) + new_holes
    store.write_phase4(sid, fid, "added_holes", {"added_holes": items})
    return HoleListResponse(holes=[AddedHole(**h) for h in items])


@router.delete("/holes/{hole_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_hole(sid: str, fid: str, hole_id: str) -> None:
    """Remove a single added-hole by id (C1a)."""

    store, _sf = _resolve(sid, fid)
    removed = store.delete_phase4_item(sid, fid, "added_holes", hole_id)
    if not removed:
        raise HTTPException(status_code=404, detail="hole id not found")
    return None


@router.post("/notes", response_model=NoteListResponse)
async def post_notes(sid: str, fid: str, body: NoteRequest) -> NoteListResponse:
    """Persist the full note list (last-write-wins, same as dimensions)."""

    store, _sf = _resolve(sid, fid)
    payload = {"notes": [n.model_dump() for n in body.notes]}
    store.write_phase4(sid, fid, "notes", payload)
    return NoteListResponse(notes=body.notes)


@router.get("/notes", response_model=NoteListResponse)
async def get_notes(sid: str, fid: str) -> NoteListResponse:
    store, _sf = _resolve(sid, fid)
    data = store.read_phase4(sid, fid, "notes") or {}
    items = [Note(**n) for n in (data.get("notes") or [])]
    return NoteListResponse(notes=items)


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(sid: str, fid: str, note_id: str) -> None:
    """Remove a single note by id (C1a)."""

    store, _sf = _resolve(sid, fid)
    removed = store.delete_phase4_item(sid, fid, "notes", note_id)
    if not removed:
        raise HTTPException(status_code=404, detail="note id not found")
    return None


def _load_outer_for_bridges(store, sf, sid: str, fid: str):
    """Helper: confirm outer loop exists and return (payload, loop_ids)."""

    saved = store.read_outer(sid, fid)
    if not saved or not saved.get("loop"):
        raise HTTPException(
            status_code=422,
            detail="先に外径を確定してください (detect-outer / outer-manual)",
        )
    try:
        parsed = parse_file(sf.path, file_id=fid, name=sf.name, outer_ids=list(saved["loop"]))
    except Exception as exc:  # noqa: BLE001
        log.exception("parse failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}") from exc
    return parsed, list(saved["loop"])


@router.post("/bridges", response_model=BridgeListResponse)
async def post_bridges(
    sid: str, fid: str, body: BridgeRequest
) -> BridgeListResponse:
    """Persist the full bridge list. Requires a confirmed outer loop so
    ``edge_id`` references can be validated."""

    store, sf = _resolve(sid, fid)
    parsed, loop = _load_outer_for_bridges(store, sf, sid, fid)
    # Accept both ``En`` and the composite ``En#k`` form (H2) used when a
    # single closed LWPOLYLINE represents the entire outer — auto_distribute
    # may emit such ids for per-vertex bridge slots.
    valid_edge_ids = {f"E{i + 1}" for i in range(len(loop))}
    unknown = [
        b.edge_id
        for b in body.bridges
        if b.edge_id.split("#", 1)[0] not in valid_edge_ids
    ]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"未知の edge_id が含まれています: {sorted(set(unknown))[:5]}",
        )
    payload = {"bridges": [b.model_dump() for b in body.bridges]}
    store.write_phase4(sid, fid, "bridges", payload)
    enriched = attach_positions(payload["bridges"], parsed, loop)
    return BridgeListResponse(bridges=[Bridge(**b) for b in enriched])


@router.get("/bridges", response_model=BridgeListResponse)
async def get_bridges(sid: str, fid: str) -> BridgeListResponse:
    store, sf = _resolve(sid, fid)
    data = store.read_phase4(sid, fid, "bridges") or {}
    raw = list(data.get("bridges") or [])
    # If the outer is confirmed, enrich with computed positions so the UI
    # can render bridge glyphs without re-doing the edge math itself.
    saved = store.read_outer(sid, fid)
    if saved and saved.get("loop") and raw:
        try:
            parsed = parse_file(
                sf.path, file_id=fid, name=sf.name, outer_ids=list(saved["loop"])
            )
            raw = attach_positions(raw, parsed, list(saved["loop"]))
        except Exception as exc:  # noqa: BLE001 — never block a list endpoint
            log.warning("bridge position attach failed for %s/%s: %s", sid, fid, exc)
    return BridgeListResponse(bridges=[Bridge(**b) for b in raw])


@router.delete("/bridges/{bridge_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bridge(sid: str, fid: str, bridge_id: str) -> None:
    """Remove a single bridge by id (C1a — parity with /dimensions)."""

    store, _sf = _resolve(sid, fid)
    removed = store.delete_phase4_item(sid, fid, "bridges", bridge_id)
    if not removed:
        raise HTTPException(status_code=404, detail="bridge id not found")
    return None


@router.post("/bridges/auto", response_model=BridgeListResponse)
async def post_bridges_auto(
    sid: str, fid: str, body: BridgeAutoRequest
) -> BridgeListResponse:
    """Auto-distribute ``count`` bridges across the confirmed outer perimeter."""

    store, sf = _resolve(sid, fid)
    parsed, loop = _load_outer_for_bridges(store, sf, sid, fid)
    new_bridges = auto_distribute(parsed, loop, body.count, body.width_mm)
    store.write_phase4(sid, fid, "bridges", {"bridges": new_bridges})
    enriched = attach_positions(new_bridges, parsed, loop)
    return BridgeListResponse(bridges=[Bridge(**b) for b in enriched])


@router.get("/annotations", response_model=AnnotationsResponse)
async def get_annotations(sid: str, fid: str) -> AnnotationsResponse:
    """Unified read of all 5 Phase-4 overlays in one round trip.

    The frontend uses this when entering a mode that needs to render all
    existing markups in context (e.g. opening the note panel still shows
    pending dimensions/bridges so the operator doesn't lose orientation).
    """

    store, sf = _resolve(sid, fid)
    dims = (store.read_phase4(sid, fid, "dimensions") or {}).get("dimensions") or []
    notes = (store.read_phase4(sid, fid, "notes") or {}).get("notes") or []
    bridges = (store.read_phase4(sid, fid, "bridges") or {}).get("bridges") or []
    holes = (store.read_phase4(sid, fid, "added_holes") or {}).get("added_holes") or []
    edits = (store.read_phase4(sid, fid, "edits") or {}).get("edits") or []

    # Enrich bridge entries with a backend-computed ``position`` field so
    # the frontend can draw markers without re-deriving outer-edge math.
    saved_outer = store.read_outer(sid, fid)
    if bridges and saved_outer and saved_outer.get("loop"):
        try:
            parsed = parse_file(
                sf.path, file_id=fid, name=sf.name, outer_ids=list(saved_outer["loop"])
            )
            bridges = attach_positions(bridges, parsed, list(saved_outer["loop"]))
        except Exception as exc:  # noqa: BLE001 — annotations are advisory
            log.warning(
                "annotations: bridge position attach failed for %s/%s: %s",
                sid, fid, exc,
            )
    return AnnotationsResponse(
        dimensions=[Dimension(**d) for d in dims],
        notes=[Note(**n) for n in notes],
        bridges=[Bridge(**b) for b in bridges],
        added_holes=[AddedHole(**h) for h in holes],
        edits=[EditedVertex(**e) for e in edits],
    )


@router.get("/export")
async def export(
    sid: str,
    fid: str,
    format: str = "dxf",
    with_offset: bool = Query(False),
    with_chamfer: bool = Query(False),
    with_dimensions: bool = Query(False),
    with_added_holes: bool = Query(False),
    with_notes: bool = Query(False),
    with_bridges: bool = Query(False),
    with_edits: bool = Query(False),
    with_frame: str = Query("auto"),
    material: str | None = Query(None),
) -> FileResponse:
    """Stream the cleaned drawing back to the browser.

    ``format`` ∈ {``dxf``, ``pdf``}. ``with_offset``/``with_chamfer`` toggle
    the optional 加工代 / C面 overlays. ``with_frame`` (``auto`` /
    ``none`` / ``cutflow``) controls the material-takeoff frame for PDF
    output and is ignored for DXF exports. ``material`` (H4) is a free-text
    material label rendered on the PDF header band when supplied; ignored
    for DXF exports.
    """

    if format not in ("dxf", "pdf"):
        raise HTTPException(
            status_code=400, detail="format must be 'dxf' or 'pdf'"
        )
    if with_frame not in ("auto", "none", "cutflow"):
        raise HTTPException(
            status_code=400,
            detail="with_frame must be one of: auto, none, cutflow",
        )

    store, sf = _resolve(sid, fid)
    deleted = set(store.get_deleted_for_file(sid, fid))

    extra: list[dict] | None = None
    if with_offset:
        saved_offset = store.read_offset(sid, fid)
        if not saved_offset or not saved_offset.get("result"):
            raise HTTPException(
                status_code=422,
                detail="加工代がまだ計算されていません (先に POST /offset を呼んでください)",
            )
        loop = saved_offset["result"].get("offset_loop") or {}
        verts = loop.get("vertices") or []
        if verts:
            extra = [{
                "vertices": verts,
                "closed": bool(loop.get("closed", True)),
                "layer": "CUTFLOW_OFFSET",
                "color": 4,
            }]

    chamfer_extras: list[dict] | None = None
    saved_outer = store.read_outer(sid, fid)
    if with_chamfer:
        if not saved_outer or not saved_outer.get("loop"):
            raise HTTPException(
                status_code=422,
                detail="外径未確定です。C面注記を付加するには先に外径を確定してください",
            )
        saved_chamfer = store.read_chamfer(sid, fid) or {}
        specs = list(saved_chamfer.get("specs") or [])
        if specs:
            try:
                payload = parse_file(
                    sf.path, file_id=fid, name=sf.name, outer_ids=list(saved_outer["loop"])
                )
                loop_ids: list[str] = list(saved_outer["loop"])
                topo = _build_topo(payload, loop_ids)
                corners, edges = list_corners(topo, loop_ids)
                chamfer_extras = chamfer_dxf_extras(specs, corners, edges)
            except Exception as exc:  # noqa: BLE001
                log.warning("chamfer annotation render failed for %s/%s: %s", sid, fid, exc)

    # Phase 4 — gather optional overlays from session storage.
    dims_extras: list[dict] | None = None
    holes_extras: list[dict] | None = None
    notes_extras: list[dict] | None = None
    bridges_extras: list[dict] | None = None
    edits_extras: list[dict] | None = None

    if with_dimensions:
        data = store.read_phase4(sid, fid, "dimensions") or {}
        items = data.get("dimensions") or []
        if items:
            dims_extras = dimensions_dxf_extras(items)

    if with_added_holes:
        data = store.read_phase4(sid, fid, "added_holes") or {}
        items = data.get("added_holes") or []
        if items:
            holes_extras = holes_dxf_extras(items)

    if with_notes:
        data = store.read_phase4(sid, fid, "notes") or {}
        items = data.get("notes") or []
        if items:
            notes_extras = notes_dxf_extras(items)

    if with_bridges:
        data = store.read_phase4(sid, fid, "bridges") or {}
        items = data.get("bridges") or []
        if items and saved_outer and saved_outer.get("loop"):
            try:
                payload = parse_file(
                    sf.path,
                    file_id=fid,
                    name=sf.name,
                    outer_ids=list(saved_outer["loop"]),
                )
                bridges_extras = bridges_dxf_extras(items, payload, list(saved_outer["loop"]))
            except Exception as exc:  # noqa: BLE001 — advisory; never block export
                log.warning("bridge render failed for %s/%s: %s", sid, fid, exc)

    if with_edits:
        data = store.read_phase4(sid, fid, "edits") or {}
        items = data.get("edits") or []
        if items:
            edits_extras = items

    out_dir = Path(tempfile.mkdtemp(prefix="cutflow-export-"))
    base = Path(sf.name).stem
    suffix_parts = ["clean"]
    if with_offset:
        suffix_parts.append("offset")
    if with_chamfer:
        suffix_parts.append("chamfer")
    if with_dimensions:
        suffix_parts.append("dim")
    if with_added_holes:
        suffix_parts.append("holes")
    if with_notes:
        suffix_parts.append("notes")
    if with_bridges:
        suffix_parts.append("bridges")
    if with_edits:
        suffix_parts.append("edits")
    suffix = "_" + "_".join(suffix_parts)

    if format == "dxf":
        dest = out_dir / f"{base}{suffix}.dxf"
        try:
            export_clean_dxf(
                sf.path,
                deleted,
                dest,
                extra_polylines=extra,
                chamfer_annotations=chamfer_extras,
                dimensions=dims_extras,
                edits=edits_extras,
                added_holes=holes_extras,
                notes=notes_extras,
                bridges=bridges_extras,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("export failed for %s", sf.path)
            raise HTTPException(status_code=500, detail=f"export failed: {exc}") from exc

        return FileResponse(
            path=str(dest),
            filename=f"{base}{suffix}.dxf",
            media_type="application/dxf",
            background=BackgroundTask(shutil.rmtree, str(out_dir), ignore_errors=True),
        )

    # PDF path.
    dest = out_dir / f"{base}{suffix}.pdf"
    # Pull metadata from the offset summary if available; otherwise leave None.
    perimeter_mm: float | None = None
    plate_size: str | None = None
    if saved_outer and saved_outer.get("perimeter"):
        try:
            perimeter_mm = float(saved_outer["perimeter"])
        except (TypeError, ValueError):
            perimeter_mm = None
    saved_offset = store.read_offset(sid, fid)
    if saved_offset and saved_offset.get("result"):
        result = saved_offset["result"]
        try:
            if with_offset and result.get("perimeter"):
                perimeter_mm = float(result["perimeter"])
        except (TypeError, ValueError):
            pass
        if result.get("plate_size"):
            plate_size = str(result["plate_size"])

    # When Phase-4 overlays are requested, materialise a temporary DXF
    # carrying every overlay through ``export_clean_dxf`` and feed that
    # to the PDF renderer — keeps PDF rendering logic out of sync risk
    # with the 5 new tool layers.
    pdf_source: Path = sf.path
    if any([dims_extras, holes_extras, notes_extras, bridges_extras, edits_extras]):
        try:
            tmp_dxf = out_dir / f"{base}_pdf_overlay.dxf"
            export_clean_dxf(
                sf.path,
                deleted,
                tmp_dxf,
                extra_polylines=extra,
                chamfer_annotations=chamfer_extras,
                dimensions=dims_extras,
                edits=edits_extras,
                added_holes=holes_extras,
                notes=notes_extras,
                bridges=bridges_extras,
            )
            pdf_source = tmp_dxf
            # The deletion + overlays are now baked into pdf_source so
            # the PDF renderer must NOT re-apply them.
            pdf_deleted: set[str] = set()
            pdf_extra: list[dict] | None = None
            pdf_chamfer: list[dict] | None = None
        except Exception as exc:  # noqa: BLE001
            log.warning("Phase-4 PDF overlay bake failed for %s: %s", sf.path, exc)
            pdf_deleted = deleted
            pdf_extra = extra
            pdf_chamfer = chamfer_extras
    else:
        pdf_deleted = deleted
        pdf_extra = extra
        pdf_chamfer = chamfer_extras

    try:
        export_pdf(
            pdf_source,
            dest,
            deleted_ids=pdf_deleted,
            extra_polylines=pdf_extra or [],
            chamfer_annotations=pdf_chamfer or [],
            title=Path(sf.name).stem,
            material=material if material and material.strip() else None,
            perimeter_mm=perimeter_mm,
            plate_size=plate_size,
            frame=with_frame,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf export failed for %s", sf.path)
        raise HTTPException(status_code=500, detail=f"pdf export failed: {exc}") from exc

    return FileResponse(
        path=str(dest),
        filename=f"{base}{suffix}.pdf",
        media_type="application/pdf",
        background=BackgroundTask(shutil.rmtree, str(out_dir), ignore_errors=True),
    )
