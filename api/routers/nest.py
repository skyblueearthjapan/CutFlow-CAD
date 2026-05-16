"""ネスティングエンドポイント — Phase 5.

``POST /api/session/{sid}/nest`` は重い計算なのでジョブキュー (非同期)
に流す。即時に ``job_id`` を返し、進捗は ``GET /api/jobs/{id}`` で確認。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from models import JobCreated, NestRequest
from services.dxf_parser import parse_file
from services.job_queue import get_queue
from services.metrics import incr
from services.nesting import (
    PartInput,
    build_sheet_summaries,
    nest_bottom_left,
    overall_efficiency,
    part_bbox_from_payload,
)
from storage import SessionExpired, SessionNotFound, get_store

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/session/{sid}", tags=["nest"])


@router.post("/nest", response_model=JobCreated, status_code=202)
async def post_nest(sid: str, body: NestRequest) -> JobCreated:
    """ネスティングジョブを投入し ``job_id`` を返す."""

    if body.algorithm != "bottom_left":
        # Phase 5 では BLF のみ
        raise HTTPException(
            status_code=400,
            detail=f"未対応のアルゴリズムです: {body.algorithm} (Phase 5 は bottom_left のみ)",
        )

    store = get_store()
    try:
        sess = store.get(sid)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except SessionExpired as exc:
        raise HTTPException(status_code=410, detail="session expired") from exc

    valid_ids = {f.file_id: f for f in sess.files}
    unknown = [fid for fid in body.file_ids if fid not in valid_ids]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown file_id(s): {unknown[:5]}",
        )

    # Snapshot the data needed by the job NOW so the job func is self-contained
    # — it must NOT depend on TestClient teardown order or the singleton store
    # being alive when it eventually runs.
    parts_data: list[tuple[str, str, str, dict]] = []
    for fid in body.file_ids:
        sf = valid_ids[fid]
        # 既存テンプレ設定があれば加工代を加算 (歩留まりへの寄与)
        tpl = store.read_template_for_file(sid, fid) or {}
        offset_mm = float(tpl.get("default_offset_mm") or 0.0)
        parts_data.append((fid, str(sf.path), sf.name, {"offset_mm": offset_mm}))

    sheet_w = body.sheet.width_mm
    sheet_h = body.sheet.height_mm
    spacing = body.spacing_mm
    quantity = body.sheet.quantity
    rotation = body.rotation

    async def _job(progress_cb) -> dict[str, Any]:
        import asyncio

        loop = asyncio.get_running_loop()

        def _sync_compute() -> dict[str, Any]:
            parts: list[PartInput] = []
            total = max(1, len(parts_data))
            for i, (fid, path, name, meta) in enumerate(parts_data):
                payload = parse_file(path, file_id=fid, name=name)
                off = float(meta.get("offset_mm") or 0.0)
                w, h, ox, oy = part_bbox_from_payload(payload, offset_mm=off)
                parts.append(
                    PartInput(
                        file_id=fid,
                        width_mm=w,
                        height_mm=h,
                        bbox_offset_x=ox,
                        bbox_offset_y=oy,
                    )
                )
                # parse 進捗 (全体の前半 50%)
                progress_cb(0.5 * (i + 1) / total)

            sheets, unplaced, warns = nest_bottom_left(
                parts,
                sheet_w=sheet_w,
                sheet_h=sheet_h,
                spacing_mm=spacing,
                sheet_quantity=quantity,
                rotation=rotation,
            )
            progress_cb(0.95)
            return {
                "sheets": build_sheet_summaries(sheets),
                "placed_count": sum(len(s.placements) for s in sheets),
                "unplaced_file_ids": unplaced,
                "total_efficiency": overall_efficiency(sheets),
                "warnings": warns,
            }

        result = await loop.run_in_executor(None, _sync_compute)
        progress_cb(1.0)
        return result

    job_id = get_queue().submit(
        "nest",
        _job,
        meta={
            "session_id": sid,
            "file_ids": body.file_ids,
            "sheet": body.sheet.model_dump(),
            "spacing_mm": body.spacing_mm,
        },
    )
    incr("jobs.submitted.nest")
    return JobCreated(job_id=job_id, status="pending")
