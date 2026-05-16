"""Pydantic models exchanged over the CutFlow CAD HTTP API.

All numeric values are in DXF native units (millimeters in our sample set).
Coordinates are returned in raw DXF space (Y axis pointing up); the frontend
is responsible for SVG axis inversion based on ``bounding_box``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Category taxonomy
# ---------------------------------------------------------------------------

EntityCategory = Literal["outer", "hole", "dim", "balloon", "tap", "frame", "other"]
"""SVG class hint used by the frontend (matches ``.ent.<category>``)."""


# ---------------------------------------------------------------------------
# Geometry containers
# ---------------------------------------------------------------------------


class BoundingBox(BaseModel):
    """Axis-aligned bounding box in DXF coordinates."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float


class EntityOut(BaseModel):
    """Single DXF entity serialised for the SVG canvas.

    ``geom`` is intentionally typed as ``dict[str, Any]`` because the shape
    depends on ``type`` (LINE / CIRCLE / ARC / LWPOLYLINE / TEXT / INSERT /
    DIMENSION / HATCH). See ``services.dxf_parser`` for the per-type schema.
    """

    id: str
    type: str
    category: EntityCategory = "other"
    color: int = 256  # 256 = BYLAYER
    layer: str = "0"
    geom: dict[str, Any] = Field(default_factory=dict)


class DeleteCandidates(BaseModel):
    """Entity IDs grouped by the delete-mode checkbox they correspond to."""

    model_config = ConfigDict(populate_by_name=True)

    DIMENSION: list[str] = Field(default_factory=list)
    BALLOON: list[str] = Field(default_factory=list)
    TAP: list[str] = Field(default_factory=list)
    FRAME: list[str] = Field(default_factory=list)


class Stats(BaseModel):
    total: int
    by_category: dict[str, int]


# ---------------------------------------------------------------------------
# Session / file payloads
# ---------------------------------------------------------------------------


class FileMeta(BaseModel):
    file_id: str
    name: str
    size: int
    status: Literal["ready", "error"] = "ready"
    error: str | None = None


class SessionInfo(BaseModel):
    session_id: str
    files: list[FileMeta]
    expires_at: datetime


class FileEntities(BaseModel):
    file_id: str
    name: str
    bounding_box: BoundingBox
    entities: list[EntityOut]
    delete_candidates: DeleteCandidates
    stats: Stats
    units: str = "mm"
    deleted_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Delete / export payloads
# ---------------------------------------------------------------------------


class DeleteRequest(BaseModel):
    entity_ids: list[str] = Field(default_factory=list)


class DeleteResponse(BaseModel):
    deleted_count: int
    remaining: int


# ---------------------------------------------------------------------------
# Outer-detection / offset payloads (Phase 2)
# ---------------------------------------------------------------------------


OuterStatus = Literal["success", "low_confidence", "failed"]


class OuterLoopSummary(BaseModel):
    """Geometric summary of an outer-loop candidate."""

    closed: bool
    segments: int
    lines: int
    arcs: int
    perimeter: float
    area: float
    bounding_box: BoundingBox


class OuterCandidate(BaseModel):
    """Single outer-loop candidate (the runner-ups surfaced to the UI)."""

    loop: list[str] = Field(default_factory=list)
    confidence: float
    area: float
    method: str = "graph"


class OuterDetectionResult(BaseModel):
    """Outer-shape detection response.

    ``status`` collapses confidence into one of:

    * ``success``         — c >= 0.80, frontend can auto-confirm
    * ``low_confidence``  — 0.50 <= c < 0.80, frontend nudges the user
    * ``failed``          — c < 0.50, frontend forces manual selection
    """

    status: OuterStatus
    confidence: float
    method: str = ""
    outer_loop: list[str] = Field(default_factory=list)
    loop_summary: OuterLoopSummary | None = None
    warnings: list[str] = Field(default_factory=list)
    candidates: list[OuterCandidate] = Field(default_factory=list)


class OuterManualRequest(BaseModel):
    """User-supplied entity-id chain claimed to form the outer loop."""

    entity_ids: list[str] = Field(default_factory=list)


class OffsetVertex(BaseModel):
    """LWPOLYLINE vertex with optional bulge."""

    x: float
    y: float
    bulge: float = 0.0


class OffsetLoop(BaseModel):
    """Result polyline of an outer-offset computation."""

    type: Literal["LWPOLYLINE"] = "LWPOLYLINE"
    vertices: list[list[float]] = Field(default_factory=list)
    closed: bool = True


class OffsetRequest(BaseModel):
    """Outer-offset (加工代) request body.

    ``edge_overrides`` keys are 1-based edge labels (``"E1"`` .. ``"En"``) in
    loop traversal order — they add ON TOP of ``default_mm`` rather than
    replacing it (matches the spec's "simple per-edge additive" decision).
    """

    default_mm: float = Field(3.0, ge=0.0, le=200.0)
    edge_overrides: dict[str, float] = Field(default_factory=dict)
    corner_join: Literal["arc", "miter"] = "arc"


class OffsetResult(BaseModel):
    offset_loop: OffsetLoop
    perimeter: float
    area: float
    bounding_box: BoundingBox
    plate_size: str  # e.g. "446 × 286 mm"
    material_efficiency: float
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Chamfer / bevel payloads (Phase 3)
# ---------------------------------------------------------------------------


ChamferType = Literal["C", "bevel"]
"""``C`` = 面取り (Cn); ``bevel`` = 開先 (角度指定)."""

ExportFormat = Literal["dxf", "pdf"]
ExportFrame = Literal["auto", "none", "cutflow"]


class ChamferSpec(BaseModel):
    """One chamfer (面取り) or bevel (開先) entry.

    ``corner_id`` follows the labels returned by ``GET /corners``:
    ``C1``..``Cn`` for outer-loop corners, ``E1``..``En`` for outer-loop edges.
    ``size_mm`` is the chamfer leg length (= ``Cn``, used by ``type="C"``);
    ``angle_deg`` is the bevel angle (e.g. 30°, used by ``type="bevel"``).

    Validation (M1)
    ---------------
    * ``size_mm`` > 0 and ≤ 20 mm — a 0 mm chamfer is meaningless and a
      >20 mm chamfer on a workshop plate is almost certainly a typo.
    * ``angle_deg`` ∈ (0°, 180°) — the boundary values are degenerate.
    * For ``type='bevel'`` we additionally require ``angle ≥ 5°`` since
      sub-5° bevels are below practical cutter resolution.
    """

    corner_id: str
    size_mm: float = Field(2.0, gt=0.0, le=20.0)
    angle_deg: float = Field(45.0, gt=0.0, lt=180.0)
    type: ChamferType = "C"

    @model_validator(mode="after")
    def _validate_bevel_angle(self) -> "ChamferSpec":
        if self.type == "bevel" and self.angle_deg < 5.0:
            raise ValueError(
                "bevel angle must be >= 5° (sub-5° bevels are not practical)"
            )
        return self


class ChamferAnnotationItem(BaseModel):
    """Visual hint for the canvas: where to draw a chamfer/bevel marker."""

    corner_id: str
    position: list[float] = Field(default_factory=list)  # [x, y]
    label: str
    kind: ChamferType


class ChamferGeometry(BaseModel):
    """Annotation payload returned alongside saved specs."""

    type: Literal["annotations"] = "annotations"
    items: list[ChamferAnnotationItem] = Field(default_factory=list)


class ChamferRequest(BaseModel):
    specs: list[ChamferSpec] = Field(default_factory=list)


class ChamferResponse(BaseModel):
    specs: list[ChamferSpec] = Field(default_factory=list)
    geometry: ChamferGeometry = Field(default_factory=ChamferGeometry)


class CornerInfo(BaseModel):
    """A single corner of the confirmed outer loop (C面 UI target)."""

    corner_id: str  # ``C1``, ``C2``, ...
    position: list[float] = Field(default_factory=list)  # [x, y]
    angle_deg: float = 180.0  # interior angle at the corner
    is_acute: bool = False
    is_convex: bool = True  # convex (凸) — outward bend in CCW order


class EdgeInfo(BaseModel):
    """A single edge of the confirmed outer loop (開先 UI target)."""

    edge_id: str  # ``E1``, ``E2``, ...
    midpoint: list[float] = Field(default_factory=list)  # [x, y]
    length: float = 0.0


class CornersResponse(BaseModel):
    corners: list[CornerInfo] = Field(default_factory=list)
    edges: list[EdgeInfo] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Frame cleanup payload (Phase 3)
# ---------------------------------------------------------------------------


class FrameCleanupResponse(BaseModel):
    removed_count: int = 0
    frame_entity_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 4 — dimension / edit / hole / note / bridge payloads
# ---------------------------------------------------------------------------


DimensionType = Literal["linear", "aligned", "diameter", "radius"]
"""ISO 寸法 種別 — DESIGN.md §2 ツール一覧 5 (寸法)."""

NotePreset = Literal["roughness", "welding", "general"]
"""注記プリセット — frontend が選択する3カテゴリ (面粗さ / 溶接 / 自由文)."""

SnapType = Literal["endpoint", "midpoint", "intersection", "center", "quadrant", "grid"]
"""線編集モードで利用可能な snap 種別."""


class Dimension(BaseModel):
    """寸法 1 件 (linear / aligned / diameter / radius)。

    Validation
    ----------
    * ``p1 == p2`` は拒否 (degenerate)。
    * diameter / radius は ``p1``=中心, ``p2``=円周上点を慣例とする。
    * ``text_override`` が指定された場合のみ実測値ではなくその文字列を使う。
    """

    id: str
    type: DimensionType = "linear"
    p1: list[float] = Field(default_factory=list)
    p2: list[float] = Field(default_factory=list)
    text_override: str | None = None
    style: str = "iso"

    @model_validator(mode="after")
    def _validate_points(self) -> "Dimension":
        if len(self.p1) < 2 or len(self.p2) < 2:
            raise ValueError("p1, p2 はそれぞれ [x, y] が必要です")
        if (
            abs(float(self.p1[0]) - float(self.p2[0])) < 1e-9
            and abs(float(self.p1[1]) - float(self.p2[1])) < 1e-9
        ):
            raise ValueError("p1 と p2 が一致しています (degenerate dimension)")
        return self


class DimensionRequest(BaseModel):
    dimensions: list[Dimension] = Field(default_factory=list)


class DimensionListResponse(BaseModel):
    dimensions: list[Dimension] = Field(default_factory=list)


class EditedVertex(BaseModel):
    """線編集 — 既存エンティティ頂点の移動予約 (export 時に反映)."""

    entity_id: str
    vertex_index: int = Field(0, ge=0, le=100000)
    new_position: list[float] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_pos(self) -> "EditedVertex":
        if len(self.new_position) < 2:
            raise ValueError("new_position は [x, y] が必要です")
        return self


class EditVertexRequest(BaseModel):
    edits: list[EditedVertex] = Field(default_factory=list)


class EditedVertexListResponse(BaseModel):
    edits: list[EditedVertex] = Field(default_factory=list)


class SnapRequest(BaseModel):
    position: list[float] = Field(default_factory=list)
    snap_types: list[SnapType] = Field(
        default_factory=lambda: ["endpoint", "midpoint", "intersection"]
    )
    tolerance: float = Field(1.0, gt=0.0, le=100.0)

    @model_validator(mode="after")
    def _validate_pos(self) -> "SnapRequest":
        if len(self.position) < 2:
            raise ValueError("position は [x, y] が必要です")
        return self


class SnapResponse(BaseModel):
    snapped: list[float] | None = None
    type: SnapType | None = None
    entity_id: str | None = None
    distance: float | None = None


class AddedHole(BaseModel):
    """穴追加 — CIRCLE エンティティとして export 時に実体化."""

    id: str
    position: list[float] = Field(default_factory=list)
    diameter: float = Field(..., gt=0.0, le=1000.0)
    tap_note: str | None = None

    @model_validator(mode="after")
    def _validate_position(self) -> "AddedHole":
        if len(self.position) < 2:
            raise ValueError("position は [x, y] が必要です")
        return self


class HoleAddRequest(BaseModel):
    holes: list[AddedHole] = Field(default_factory=list)


class HolePatternRequest(BaseModel):
    """整列パターンで一括追加."""

    anchor: list[float] = Field(default_factory=list)
    rows: int = Field(..., ge=1, le=200)
    cols: int = Field(..., ge=1, le=200)
    spacing: list[float] = Field(default_factory=list)
    diameter: float = Field(..., gt=0.0, le=1000.0)
    tap_note: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> "HolePatternRequest":
        if len(self.anchor) < 2:
            raise ValueError("anchor は [x, y] が必要です")
        if len(self.spacing) < 2:
            raise ValueError("spacing は [dx, dy] が必要です")
        return self


class HoleListResponse(BaseModel):
    holes: list[AddedHole] = Field(default_factory=list)


class Note(BaseModel):
    """注記 (面粗さ / 溶接 / 自由文)。 ``preset`` は UI のテンプレ識別子。"""

    id: str
    position: list[float] = Field(default_factory=list)
    text: str = Field(..., min_length=1, max_length=500)
    preset: NotePreset = "general"
    font_size_mm: float = Field(2.5, gt=0.0, le=50.0)
    rotation_deg: float = 0.0

    @model_validator(mode="after")
    def _validate_position(self) -> "Note":
        if len(self.position) < 2:
            raise ValueError("position は [x, y] が必要です")
        return self


class NoteRequest(BaseModel):
    notes: list[Note] = Field(default_factory=list)


class NoteListResponse(BaseModel):
    notes: list[Note] = Field(default_factory=list)


class Bridge(BaseModel):
    """外径エッジ上のブリッジ (保持タブ) 1 件。

    ``edge_id`` は ``GET /corners`` が返す ``E1..En`` (LWPOLYLINE 外径の
    場合は合成 ``En#k`` 形式も許容)。``position_ratio`` ∈ [0, 1] はエッジ長
    に対する中央位置。

    ``position`` (optional) — list endpoint serialiser が外径ジオメトリから
    計算した DXF 座標 (``[x, y]``) を埋めて返す。POST body では送らないこと
    が前提 (送られても無視)。"""

    id: str
    edge_id: str
    position_ratio: float = Field(0.5, ge=0.0, le=1.0)
    width_mm: float = Field(2.0, ge=0.5, le=10.0)
    position: list[float] | None = None


class BridgeRequest(BaseModel):
    bridges: list[Bridge] = Field(default_factory=list)


class BridgeAutoRequest(BaseModel):
    """自動配置: ``count`` 個を外周等間隔で配置."""

    count: int = Field(4, ge=1, le=32)
    width_mm: float = Field(2.0, ge=0.5, le=10.0)


class BridgeListResponse(BaseModel):
    bridges: list[Bridge] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Unified annotations payload (Phase 4)
# ---------------------------------------------------------------------------


class AnnotationsResponse(BaseModel):
    """5 種類のオーバーレイを 1 endpoint で返却 (フロントは差分描画)."""

    dimensions: list[Dimension] = Field(default_factory=list)
    notes: list[Note] = Field(default_factory=list)
    bridges: list[Bridge] = Field(default_factory=list)
    added_holes: list[AddedHole] = Field(default_factory=list)
    edits: list[EditedVertex] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 5 — nesting / jobs / saved sessions / templates
# ---------------------------------------------------------------------------


NestAlgorithm = Literal["bottom_left", "no_fit_polygon"]
"""ネスティングアルゴリズム。Phase 5 は ``bottom_left`` のみ実装。"""

JobStatusLiteral = Literal["pending", "running", "completed", "failed"]


class Sheet(BaseModel):
    """ネスティング用の板サイズ。``quantity`` は板の枚数 (将来複数板対応).

    Phase 5 H9: ``quantity`` の上限を 20 に下げ、極端な多枚数指定での
    BLF 走査時間爆発 (parts × sheets) を抑制する。
    """

    width_mm: float = Field(..., gt=0.0, le=10000.0)
    height_mm: float = Field(..., gt=0.0, le=10000.0)
    quantity: int = Field(1, ge=1, le=20)


class NestRequest(BaseModel):
    """``POST /api/session/{sid}/nest`` のリクエスト。

    ``file_ids`` は同じセッション内のファイル ID。各ファイルの外径
    (確定済) + 加工代を使用して矩形パッキングする。``rotation`` が True
    の場合 0°/90°/180°/270° を試行し最も歩留まりの良い向きを採用。
    """

    file_ids: list[str] = Field(default_factory=list, min_length=1, max_length=50)
    sheet: Sheet
    spacing_mm: float = Field(5.0, ge=0.0, le=200.0)
    algorithm: NestAlgorithm = "bottom_left"
    rotation: bool = True


class NestPlacement(BaseModel):
    """1 部品の配置結果 (シート上)."""

    file_id: str
    sheet_index: int = 0
    x_mm: float = 0.0
    y_mm: float = 0.0
    rotation_deg: int = 0
    width_mm: float = 0.0
    height_mm: float = 0.0


class NestSheetResult(BaseModel):
    """1 枚の板の結果サマリ.

    Phase 5 M1:
    - ``used_area_mm2`` / ``efficiency`` は **padding (加工代) 込み bbox 面積** ベース。
    - ``placed_part_area_mm2`` は padding を抜いた純粋な部品 bbox 面積 (raw)。
      Phase 5 では暫定的に ``used_area_mm2`` と同値で返している (router/service
      で raw を持ち回せない構造) — Phase 6 で正確化予定。
    """

    sheet_index: int = 0
    width_mm: float = 0.0
    height_mm: float = 0.0
    placements: list[NestPlacement] = Field(default_factory=list)
    used_area_mm2: float = 0.0
    placed_part_area_mm2: float = 0.0
    sheet_area_mm2: float = 0.0
    efficiency: float = 0.0  # used_area / sheet_area


class NestResult(BaseModel):
    """ネスティング全体の結果."""

    sheets: list[NestSheetResult] = Field(default_factory=list)
    placed_count: int = 0
    unplaced_file_ids: list[str] = Field(default_factory=list)
    total_efficiency: float = 0.0
    warnings: list[str] = Field(default_factory=list)


class NestResultEnvelope(BaseModel):
    """``GET /api/jobs/{job_id}/result`` のラッパ.

    Frontend (``getNestResult``) は ``{sheets, unplaced, warnings, utilization}``
    の形を期待しているため、サーバー側で完成ジョブ結果をこの形に整えて返す。
    ``unplaced`` は数値 (件数) を返却 (FE 側 ``NestResult.unplaced: number``)。
    """

    sheets: list[NestSheetResult] = Field(default_factory=list)
    unplaced: int = 0
    warnings: list[str] = Field(default_factory=list)
    utilization: float = 0.0


class JobCreated(BaseModel):
    """非同期ジョブ作成レスポンス."""

    job_id: str
    status: JobStatusLiteral = "pending"


class JobStatus(BaseModel):
    """``GET /api/jobs/{job_id}`` レスポンス."""

    job_id: str
    kind: str = "nest"
    status: JobStatusLiteral = "pending"
    progress: float = Field(0.0, ge=0.0, le=1.0)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


# ---- Saved sessions ------------------------------------------------------


class SaveSessionRequest(BaseModel):
    """``POST /api/sessions/save`` body."""

    name: str = Field(..., min_length=1, max_length=128)
    session_id: str = Field(..., min_length=1, max_length=64)


class SavedSessionMeta(BaseModel):
    """1 件の保存済みセッションメタ."""

    name: str
    size_bytes: int
    saved_at: datetime
    file_count: int = 0


class SavedSessionList(BaseModel):
    saved: list[SavedSessionMeta] = Field(default_factory=list)


class SavedSessionLoadResponse(BaseModel):
    """``POST /api/sessions/load/{name}`` の戻り値 — Session 形式で返却 (H3).

    Frontend ``loadSession`` は ``Session {session_id, files, expires_at}`` を
    期待しているため、復元した session の SessionInfo をそのまま返す。
    """

    session_id: str
    name: str = ""
    file_count: int = 0
    files: list[FileMeta] = Field(default_factory=list)
    expires_at: datetime | None = None


# ---- Templates -----------------------------------------------------------


class Template(BaseModel):
    """材質・板厚・加工代プリセット (api/data/templates.json).

    Phase 5 H1: Frontend は ``template_id`` / ``spacing_mm`` を期待するため
    alias を提供する。``populate_by_name=True`` により、JSON 読込側は従来通り
    ``id`` / ``default_offset_mm`` で書ける一方、レスポンスは
    ``model_dump(by_alias=True)`` で alias を含めた両キーが出る。
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., min_length=1, max_length=64, alias="template_id")
    name: str = Field(..., min_length=1, max_length=128)
    material: str = Field(..., min_length=1, max_length=64)
    thickness_mm: float = Field(..., gt=0.0, le=200.0)
    default_offset_mm: float = Field(3.0, ge=0.0, le=200.0, alias="spacing_mm")
    description: str | None = None


class TemplateList(BaseModel):
    templates: list[Template] = Field(default_factory=list)


class ApplyTemplateResponse(BaseModel):
    """テンプレ適用結果。各ファイルにオフセットを書き込んだか報告.

    Phase 5 C5: ``template`` フィールドに full Template を含めて返却し、
    Frontend が ``applyTemplate()`` で UI 既定値 (defaultOffsetMm /
    pdfMaterial / nestSpacingMm 等) を一度に同期できるようにする。
    """

    template_id: str
    session_id: str
    applied_to: list[str] = Field(default_factory=list)  # file_ids
    skipped: list[str] = Field(default_factory=list)
    default_offset_mm: float = 0.0
    template: Template | None = None


# ---- Metrics -------------------------------------------------------------


class MetricsSnapshot(BaseModel):
    """``GET /api/metrics`` の即時スナップショット."""

    uptime_sec: float = 0.0
    request_count: int = 0
    error_count: int = 0
    avg_response_ms: float = 0.0
    jobs_total: int = 0
    jobs_completed: int = 0
    jobs_failed: int = 0
    jobs_running: int = 0
    counters: dict[str, int] = Field(default_factory=dict)
