"""ジョブステータス読み取り + 結果取得 — Phase 5."""

from __future__ import annotations

import io
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from models import JobStatus, NestResultEnvelope
from services.job_queue import get_queue

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _rec_to_status(rec) -> JobStatus:
    from datetime import datetime, timezone

    def _parse(ts: str | None) -> datetime | None:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            return None

    return JobStatus(
        job_id=rec.job_id,
        kind=rec.kind,
        status=rec.status,
        progress=float(rec.progress or 0.0),
        created_at=_parse(rec.created_at) or datetime.now(timezone.utc),
        started_at=_parse(rec.started_at),
        completed_at=_parse(rec.completed_at),
        result=rec.result,
        error=rec.error,
    )


@router.get("/{job_id}", response_model=JobStatus)
async def get_job(job_id: str) -> JobStatus:
    rec = get_queue().get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _rec_to_status(rec)


def _result_envelope(rec) -> dict:
    """Job result から ``{sheets, unplaced, warnings, utilization}`` の envelope を組む.

    Phase 5 C3: Frontend ``getNestResult`` が期待する形に整える。``unplaced``
    は file_id list の長さを数値で返す (FE 側 ``NestResult.unplaced: number``)。
    """

    res = rec.result or {}
    sheets = res.get("sheets", []) or []
    unplaced_ids = res.get("unplaced_file_ids", []) or []
    warnings = res.get("warnings", []) or []
    utilization = float(res.get("total_efficiency") or 0.0)
    return {
        "sheets": sheets,
        "unplaced": len(unplaced_ids) if isinstance(unplaced_ids, list) else int(unplaced_ids or 0),
        "warnings": list(warnings),
        "utilization": utilization,
    }


@router.get("/{job_id}/result")
async def get_job_result(job_id: str) -> JSONResponse:
    """完了ジョブの結果 envelope を JSON で返す (C3).

    Frontend (``getNestResult``) のデフォルト取得経路。``Content-Disposition``
    は付けず inline JSON として返却する。
    """

    rec = get_queue().get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="job not found")
    if rec.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"job not finished (status={rec.status})",
        )
    if not rec.result:
        raise HTTPException(status_code=404, detail="no result on job")
    return JSONResponse(content=_result_envelope(rec))


@router.get("/{job_id}/result/sheets")
async def get_job_result_sheets(job_id: str) -> StreamingResponse:
    """配置結果の JSON 配列をシート別に返す (ダウンロード用).

    Phase 5 では JSON シートサマリを返却。シート別 DXF は今後の拡張余地
    として残し、まずは「どこに何が配置されたか」を呼び出し側に渡す。
    """

    rec = get_queue().get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="job not found")
    if rec.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"job not finished (status={rec.status})",
        )
    if not rec.result or "sheets" not in rec.result:
        raise HTTPException(status_code=404, detail="no sheet result on job")

    payload = json.dumps({"sheets": rec.result["sheets"]}, ensure_ascii=False, indent=2)
    buf = io.BytesIO(payload.encode("utf-8"))
    return StreamingResponse(
        buf,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="job-{job_id[:8]}-sheets.json"'},
    )


@router.get("/{job_id}/result/sheets/{sheet_index}/export")
async def export_nest_sheet(job_id: str, sheet_index: int, format: str = "dxf") -> StreamingResponse:
    """シート別 DXF 出力 (H4).

    Phase 5 は簡易実装: 各部品の bbox を ``LWPOLYLINE`` (閉) として配置
    位置に描画 + ``TEXT`` 注記 (ファイル名) を添える。Phase 6 で部品の
    実ジオメトリを ``INSERT`` 化する予定。
    """

    if format != "dxf":
        raise HTTPException(status_code=400, detail=f"unsupported format: {format}")

    rec = get_queue().get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="job not found")
    if rec.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"job not finished (status={rec.status})",
        )
    if not rec.result or "sheets" not in rec.result:
        raise HTTPException(status_code=404, detail="no sheet result on job")

    sheets = rec.result["sheets"]
    target = None
    for s in sheets:
        if int(s.get("sheet_index", -1)) == int(sheet_index):
            target = s
            break
    if target is None:
        raise HTTPException(status_code=404, detail=f"sheet {sheet_index} not found")

    dxf_text = _render_sheet_dxf(target)
    buf = io.BytesIO(dxf_text.encode("utf-8"))
    return StreamingResponse(
        buf,
        media_type="application/dxf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="nest-{job_id[:8]}-sheet{sheet_index}.dxf"'
            )
        },
    )


def _render_sheet_dxf(sheet: dict) -> str:
    """Render a placeholder DXF showing each placement as a closed LWPOLYLINE.

    最小限の R12-ish ASCII DXF を出力する。``LWPOLYLINE`` は flag=1 (closed) で
    4 頂点。``TEXT`` で部品 file_id を注記。シート外周も描画する。"""

    lines: list[str] = []
    lines.extend(["0", "SECTION", "2", "ENTITIES"])

    sheet_w = float(sheet.get("width_mm") or 0.0)
    sheet_h = float(sheet.get("height_mm") or 0.0)
    # シート外周 (width_mm/height_mm が設定されていれば描画)
    if sheet_w > 0 and sheet_h > 0:
        lines.extend([
            "0", "LWPOLYLINE",
            "8", "NEST_SHEET",
            "90", "4",
            "70", "1",
            "10", "0.000", "20", "0.000",
            "10", f"{sheet_w:.3f}", "20", "0.000",
            "10", f"{sheet_w:.3f}", "20", f"{sheet_h:.3f}",
            "10", "0.000", "20", f"{sheet_h:.3f}",
        ])

    placements = sheet.get("placements", []) or []
    for p in placements:
        x = float(p.get("x_mm") or 0.0)
        y = float(p.get("y_mm") or 0.0)
        w = float(p.get("width_mm") or 0.0)
        h = float(p.get("height_mm") or 0.0)
        # LWPOLYLINE — closed rectangle
        lines.extend([
            "0", "LWPOLYLINE",
            "8", "NEST",
            "90", "4",
            "70", "1",  # closed
            "10", f"{x:.3f}", "20", f"{y:.3f}",
            "10", f"{x + w:.3f}", "20", f"{y:.3f}",
            "10", f"{x + w:.3f}", "20", f"{y + h:.3f}",
            "10", f"{x:.3f}", "20", f"{y + h:.3f}",
        ])
        # TEXT — file_id at centre
        label = str(p.get("file_id", ""))[:32]
        lines.extend([
            "0", "TEXT",
            "8", "NEST_LABEL",
            "10", f"{x + w / 2:.3f}",
            "20", f"{y + h / 2:.3f}",
            "40", "3.5",
            "1", label,
        ])
    lines.extend(["0", "ENDSEC", "0", "EOF"])
    return "\n".join(lines) + "\n"
