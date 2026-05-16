"""Per-file endpoints: parse, delete reservation, outer detection, offset, export."""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from models import (
    ChamferRequest,
    ChamferResponse,
    CornersResponse,
    DeleteRequest,
    DeleteResponse,
    FileEntities,
    FrameCleanupResponse,
    OffsetRequest,
    OffsetResult,
    OuterDetectionResult,
    OuterManualRequest,
)
from services import graph as gmod
from services.chamfer import (
    build_annotations,
    chamfer_dxf_extras,
    list_corners,
)
from services.dxf_parser import parse_file
from services.dxf_writer import export_clean_dxf
from services.frame_cleanup import detect_frame_entities
from services.offset import OffsetError, compute_offset
from services.outer_detector import detect_outer, evaluate_manual
from services.pdf_export import export_pdf
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


@router.get("/export")
async def export(
    sid: str,
    fid: str,
    format: str = "dxf",
    with_offset: bool = Query(False),
    with_chamfer: bool = Query(False),
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

    out_dir = Path(tempfile.mkdtemp(prefix="cutflow-export-"))
    base = Path(sf.name).stem
    suffix_parts = ["clean"]
    if with_offset:
        suffix_parts.append("offset")
    if with_chamfer:
        suffix_parts.append("chamfer")
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

    try:
        export_pdf(
            sf.path,
            dest,
            deleted_ids=deleted,
            extra_polylines=extra or [],
            chamfer_annotations=chamfer_extras or [],
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
