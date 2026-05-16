"""材質・板厚・加工代テンプレートエンドポイント — Phase 5.

* ``GET /api/templates``                                   — 一覧
* ``POST /api/sessions/{sid}/apply-template/{template_id}`` — セッション
  内の全ファイルに ``default_offset_mm`` を伝搬 (再計算はクライアントが
  行う想定; 本 API は「次回オフセット計算時に使う既定値」だけ書き込む)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from models import ApplyTemplateResponse, Template, TemplateList
from services.templates import find_template, list_templates
from storage import SessionExpired, SessionNotFound, get_store

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["templates"])


@router.get("/templates", response_model=TemplateList)
async def get_templates() -> TemplateList:
    """テンプレート一覧 (固定 JSON).

    H1: ``model_dump(by_alias=True)`` で Frontend が要求する alias キー
    (``template_id`` / ``spacing_mm``) を含めて返却。Pydantic の通常の
    response_model シリアライズは ``by_alias=False`` のため、JSONResponse
    に明示的に詰め直す。
    """

    items = [Template(**t) for t in list_templates()]
    payload = {"templates": [t.model_dump(by_alias=True) for t in items]}
    return JSONResponse(content=payload)


@router.post(
    "/sessions/{sid}/apply-template/{template_id}",
)
async def apply_template(sid: str, template_id: str) -> JSONResponse:
    """セッション内の全ファイルにテンプレ既定値を保存する.

    実装方針: 既存の outer-loop / offset 計算フローを邪魔しないよう、
    テンプレ既定値は ``state/{fid}/template.json`` に保存しておくだけ。
    オフセット計算リクエストが明示 ``default_mm`` を指定しない場合の
    フォールバック値として、後段でクライアントが ``GET /templates``
    と組み合わせて使う想定。

    Phase 5 修正:
    - C5: full ``template`` を含めて返却し Frontend が UI 既定値を一度に同期可
    - H7: 全件 skipped (applied_to が空) かつ 1 件以上 skipped の場合 207 を返却
    """

    tpl = find_template(template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail=f"template not found: {template_id}")

    store = get_store()
    try:
        sess = store.get(sid)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except SessionExpired as exc:
        raise HTTPException(status_code=410, detail="session expired") from exc

    applied: list[str] = []
    skipped: list[str] = []
    for f in sess.files:
        try:
            store.write_template_for_file(sid, f.file_id, tpl)
            applied.append(f.file_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("apply_template skipped %s/%s: %s", sid, f.file_id, exc)
            skipped.append(f.file_id)

    template_obj = Template(**tpl)
    resp = ApplyTemplateResponse(
        template_id=template_id,
        session_id=sid,
        applied_to=applied,
        skipped=skipped,
        default_offset_mm=float(tpl.get("default_offset_mm") or 0.0),
        template=template_obj,
    )
    # H1: alias を含めた dump (template_id / spacing_mm)
    body = resp.model_dump(by_alias=True)

    # H7: 全件失敗 → 207 Multi-Status
    if not applied and skipped:
        return JSONResponse(content=body, status_code=207)
    return JSONResponse(content=body)
