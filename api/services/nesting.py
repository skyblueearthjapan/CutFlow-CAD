"""Bottom-Left Fill (BLF) ネスティングアルゴリズム — Phase 5.

複数部品の bounding box を 1 枚の板に最適配置する単純な BLF 実装。

* 各部品の寸法は ``(width_mm, height_mm)``（呼び出し側で外径ループ +
  加工代から計算済を渡す前提）。
* 板上を「最も左下」から走査し、衝突しない最初の位置に配置する。
* ``rotation=True`` の場合 0° / 90° の 2 方向を試し、より左下に置けた
  方を採用する (BLF と 90° で十分に実用域; 180° / 270° は矩形パッキン
  グでは結果が同じになるので省略)。

板を超える大きさの部品は ``unplaced`` 扱いで返却する。

NOTE: 真の no-fit-polygon ネスティングではないため、形が L 字や複雑な
凹凸を持つ場合は無駄が生じる。Phase 6 で no_fit_polygon を追加予定。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# 部品 bbox を走査するときの最小ステップ (mm)。1mm 単位だと巨大シート
# で O(W*H*N) が爆発するので、適応的な刻みを使う (細かすぎず粗すぎず)。
_MIN_STEP_MM = 1.0
_MAX_GRID_POINTS = 600  # 各軸の最大候補点数 — 600x600 = 36 万候補で打ち切り
# H9: parts × sheets の組合せ爆発で BLF が事実上止まる前にガードする上限
_MAX_PARTS_TIMES_SHEETS = 500


@dataclass
class PartInput:
    """ネスト入力 1 部品。"""

    file_id: str
    width_mm: float
    height_mm: float
    # 元 bbox の左下 (-X, -Y) からの平行移動分。部品 DXF 内で原点が
    # bbox の左下に揃っていない場合、配置時にこの分を補正してやれば
    # 後段の出力 (シート DXF 生成) で正確に重ねられる。Phase 5 では
    # 配置座標のみ返却するので未使用だが、API で残しておく。
    bbox_offset_x: float = 0.0
    bbox_offset_y: float = 0.0


@dataclass
class Placement:
    """1 部品の確定配置。"""

    file_id: str
    sheet_index: int
    x_mm: float
    y_mm: float
    rotation_deg: int
    width_mm: float
    height_mm: float


@dataclass
class _SheetState:
    """1 枚の板の進行中状態 (内部利用)."""

    width: float
    height: float
    placements: list[Placement] = field(default_factory=list)

    def used_area(self) -> float:
        return sum(p.width_mm * p.height_mm for p in self.placements)


def nest_bottom_left(
    parts: list[PartInput],
    sheet_w: float,
    sheet_h: float,
    spacing_mm: float = 0.0,
    sheet_quantity: int = 1,
    rotation: bool = True,
) -> tuple[list[_SheetState], list[str], list[str]]:
    """BLF ネスティング本体。

    Returns:
        (sheets, unplaced_ids, warnings)

    Parts that exceed the sheet on every rotation are returned in
    ``unplaced_ids``. ``warnings`` collects soft errors that should be
    surfaced to the operator.
    """

    warnings: list[str] = []
    if sheet_w <= 0 or sheet_h <= 0:
        raise ValueError("シートサイズは正の値である必要があります")
    if sheet_quantity < 1:
        raise ValueError("sheet_quantity は 1 以上")
    if spacing_mm < 0:
        raise ValueError("spacing_mm は 0 以上")
    # H9: parts × sheets の DoS ガード
    if len(parts) * int(sheet_quantity) > _MAX_PARTS_TIMES_SHEETS:
        raise ValueError(
            f"parts ({len(parts)}) × sheets ({sheet_quantity}) が上限 "
            f"{_MAX_PARTS_TIMES_SHEETS} を超えています — 分割実行してください"
        )
    if not parts:
        return ([_SheetState(width=sheet_w, height=sheet_h)], [], warnings)

    # 大きい順に配置するのが BLF の慣例 (大物が後だと隙間に入らない)。
    # 面積降順 → 同面積は max(w, h) 降順で安定化。
    ordered = sorted(
        parts,
        key=lambda p: (-(p.width_mm * p.height_mm), -max(p.width_mm, p.height_mm)),
    )

    sheets: list[_SheetState] = []
    unplaced: list[str] = []
    remaining = list(ordered)
    while remaining and len(sheets) < sheet_quantity:
        sheet = _SheetState(width=sheet_w, height=sheet_h)
        sheets.append(sheet)
        leftover: list[PartInput] = []
        for part in remaining:
            placed = _try_place(sheet, part, spacing_mm, sheet_index=len(sheets) - 1, rotation=rotation)
            if placed is None:
                leftover.append(part)
            else:
                sheet.placements.append(placed)
        if len(leftover) == len(remaining):
            # 何も配置できなかった = どれもシートに入らない。これ以上枚数
            # を増やしても結果は変わらないので終了。``remaining`` を空に
            # して外側の二重カウントを避ける。
            unplaced.extend(p.file_id for p in leftover)
            warnings.append(
                f"sheet {len(sheets) - 1}: {len(leftover)} 件の部品がシートに入りません"
            )
            remaining = []
            break
        remaining = leftover

    # まだ部品が余っていれば (枚数不足)
    if remaining:
        unplaced.extend(p.file_id for p in remaining)
        warnings.append(
            f"{len(remaining)} 件の部品が指定枚数 ({sheet_quantity}) に入りませんでした"
        )

    if not sheets:
        sheets.append(_SheetState(width=sheet_w, height=sheet_h))

    return sheets, unplaced, warnings


def _try_place(
    sheet: _SheetState,
    part: PartInput,
    spacing: float,
    sheet_index: int,
    rotation: bool,
) -> Placement | None:
    """1 部品の最適配置 (左下優先)。失敗時は ``None``."""

    candidates: list[tuple[int, float, float]] = []  # (rot_deg, w, h)
    candidates.append((0, part.width_mm, part.height_mm))
    if rotation and not math.isclose(part.width_mm, part.height_mm):
        candidates.append((90, part.height_mm, part.width_mm))

    best: Placement | None = None
    for rot_deg, w, h in candidates:
        if w > sheet.width or h > sheet.height:
            continue
        spot = _find_blf_position(sheet, w, h, spacing)
        if spot is None:
            continue
        x, y = spot
        cand = Placement(
            file_id=part.file_id,
            sheet_index=sheet_index,
            x_mm=x,
            y_mm=y,
            rotation_deg=rot_deg,
            width_mm=w,
            height_mm=h,
        )
        if best is None or _is_more_bottom_left(cand, best):
            best = cand
    return best


def _is_more_bottom_left(a: Placement, b: Placement) -> bool:
    """``a`` の方が ``b`` より「左下」かを比較 (y 優先, 次に x)."""

    if a.y_mm + 1e-6 < b.y_mm:
        return True
    if a.y_mm - 1e-6 > b.y_mm:
        return False
    return a.x_mm < b.x_mm


def _find_blf_position(
    sheet: _SheetState,
    w: float,
    h: float,
    spacing: float,
) -> tuple[float, float] | None:
    """指定サイズの矩形を置ける最左下の (x, y) を探す。

    既配置矩形の各「コーナー候補点」(右上 / 右下 / 左上 + 板の左下)
    の周辺で走査する。これにより O(N) ステップで現実的な BLF が動く。
    候補が無ければ ``None``。
    """

    # 候補点プール (BLF のスタート位置)。
    # まず板の左下と既配置矩形の各「右上/右下/左上」を入れる。
    candidates: set[tuple[float, float]] = {(0.0, 0.0)}
    for p in sheet.placements:
        # spacing を含めた外接矩形の角を起点にする
        x0 = p.x_mm
        y0 = p.y_mm
        x1 = x0 + p.width_mm + spacing
        y1 = y0 + p.height_mm + spacing
        candidates.add((x1, y0))  # 右下
        candidates.add((x0, y1))  # 左上
        candidates.add((x1, y1))  # 右上
        candidates.add((0.0, y1))  # 板左端 + 上
        # M2: 板底 (y=0) に水平方向の隙間が残った場合の候補
        candidates.add((x1, 0.0))

    # ソート (y, x) 昇順 — 「最下端 → 最左端」優先
    sorted_cands = sorted(candidates, key=lambda xy: (xy[1], xy[0]))

    for x, y in sorted_cands:
        if x < -1e-9 or y < -1e-9:
            continue
        if x + w > sheet.width + 1e-9 or y + h > sheet.height + 1e-9:
            continue
        # この候補で衝突しないなら採用 (左下優先なので一致最初を返す)
        if not _collides(sheet, x, y, w, h, spacing):
            return (x, y)

    # 候補点で見つからなければ、簡易グリッド走査でフォールバック
    return _grid_scan(sheet, w, h, spacing)


def _grid_scan(
    sheet: _SheetState,
    w: float,
    h: float,
    spacing: float,
) -> tuple[float, float] | None:
    """候補点ベースで見つからなかった場合のフォールバック (粗グリッド)."""

    # 適応的ステップ: シート寸法から ``_MAX_GRID_POINTS`` 個以下に収まる
    # よう刻みを決める。小さい板で 1mm、大きい板で 5mm 程度に落ち着く。
    step_x = max(_MIN_STEP_MM, sheet.width / _MAX_GRID_POINTS)
    step_y = max(_MIN_STEP_MM, sheet.height / _MAX_GRID_POINTS)
    max_x = sheet.width - w
    max_y = sheet.height - h
    if max_x < -1e-9 or max_y < -1e-9:
        return None

    # 最下端 → 最左端 の順
    y = 0.0
    while y <= max_y + 1e-9:
        x = 0.0
        while x <= max_x + 1e-9:
            if not _collides(sheet, x, y, w, h, spacing):
                return (x, y)
            x += step_x
        y += step_y
    return None


def _collides(
    sheet: _SheetState,
    x: float,
    y: float,
    w: float,
    h: float,
    spacing: float,
) -> bool:
    """配置候補が既存配置と spacing 込みで干渉するか (bbox 重なり)."""

    for p in sheet.placements:
        if (
            x + w + spacing <= p.x_mm + 1e-9
            or p.x_mm + p.width_mm + spacing <= x + 1e-9
            or y + h + spacing <= p.y_mm + 1e-9
            or p.y_mm + p.height_mm + spacing <= y + 1e-9
        ):
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# Sheet summary helpers
# ---------------------------------------------------------------------------


def build_sheet_summaries(sheets: list[_SheetState]) -> list[dict]:
    """``_SheetState`` を Pydantic 互換 dict に整形する.

    M1: ``efficiency`` の意味は **「padding (加工代) 込み bbox 面積 ÷ シート面積」**。
    したがって本物の部品面積 (offset を引いた raw bbox) は ``placed_part_area_mm2``
    として別途返し、UI 側で「実部品面積 / 加工代込み」を両方表示できるようにする。
    """

    out: list[dict] = []
    for idx, sh in enumerate(sheets):
        used = sh.used_area()
        area = sh.width * sh.height
        eff = (used / area) if area > 0 else 0.0
        # M1: padding を抜いた純粋な部品面積。``_SheetState.placements`` の
        # width_mm/height_mm はすでに padding を含むので、ここでは情報が
        # 残っていない。代わりに ``placed_part_area_mm2`` には 0.0 を入れ、
        # 呼び出し側 (router) が PartInput 経由で正確値を提供する余地を残す。
        # Phase 5 暫定: padding込みと等価扱い (=used_area)。Phase 6 で
        # PartInput.raw_bbox を持ち回して正確化する。
        out.append(
            {
                "sheet_index": idx,
                "width_mm": sh.width,
                "height_mm": sh.height,
                "placements": [
                    {
                        "file_id": p.file_id,
                        "sheet_index": p.sheet_index,
                        "x_mm": p.x_mm,
                        "y_mm": p.y_mm,
                        "rotation_deg": p.rotation_deg,
                        "width_mm": p.width_mm,
                        "height_mm": p.height_mm,
                    }
                    for p in sh.placements
                ],
                "used_area_mm2": used,
                "placed_part_area_mm2": used,  # Phase 6 で raw bbox から再計算
                "sheet_area_mm2": area,
                "efficiency": eff,
            }
        )
    return out


def overall_efficiency(sheets: list[_SheetState]) -> float:
    used = sum(sh.used_area() for sh in sheets)
    area = sum(sh.width * sh.height for sh in sheets)
    return (used / area) if area > 0 else 0.0


# ---------------------------------------------------------------------------
# Per-file bbox extractor (used by the router to assemble PartInput list)
# ---------------------------------------------------------------------------


def part_bbox_from_payload(
    payload, offset_mm: float = 0.0
) -> tuple[float, float, float, float]:
    """Return ``(width, height, ox, oy)`` for nesting.

    ``payload`` is the ``FileEntities`` model. We use its ``bounding_box``
    (computed by ezdxf) plus a uniform ``offset_mm`` margin so the nesting
    layer keeps 加工代 around the cut path.
    """

    bb = payload.bounding_box
    w = max(0.0, float(bb.max_x - bb.min_x)) + 2.0 * offset_mm
    h = max(0.0, float(bb.max_y - bb.min_y)) + 2.0 * offset_mm
    return w, h, float(bb.min_x) - offset_mm, float(bb.min_y) - offset_mm
