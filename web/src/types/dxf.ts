/**
 * Type definitions for the DXF session API contract (Phase 1).
 *
 * The backend returns geometry already classified by category so the frontend
 * can render with the v3 colour system (cyan = keep, amber = delete candidate,
 * purple = chamfer/bevel) without re-running classification.
 *
 * NOTE: `Entity.geom` intentionally stays `any` for now — each EntityType has
 * a different shape and a discriminated union would balloon the type surface
 * before we have stable geometry hooks. Phase 2 will tighten this once the
 * geometry rendering settles.
 * TODO (Phase 2): replace `any` with a proper discriminated union keyed on `type`.
 */

export type EntityCategory =
  | 'outer'
  | 'hole'
  | 'dim'
  | 'balloon'
  | 'tap'
  | 'frame'
  | 'other';

export type EntityType =
  | 'LINE'
  | 'CIRCLE'
  | 'ARC'
  | 'LWPOLYLINE'
  | 'POLYLINE'
  | 'ELLIPSE'
  | 'SPLINE'
  | 'TEXT'
  | 'MTEXT'
  | 'INSERT'
  | 'DIMENSION'
  | 'LEADER'
  | 'HATCH'
  | 'POINT'
  | 'SOLID';

export interface BoundingBox {
  min_x: number;
  min_y: number;
  max_x: number;
  max_y: number;
}

export interface Entity {
  id: string;
  type: EntityType;
  category: EntityCategory;
  color: number;
  layer: string;
  /** Geometry payload — shape depends on `type`. See backend serialiser. */
  geom: any;
}

/** Per-DXF delete candidate buckets (keys are upper-case for ezdxf parity). */
export interface DeleteCandidates {
  DIMENSION?: string[];
  BALLOON?: string[];
  TAP?: string[];
  FRAME?: string[];
  [key: string]: string[] | undefined;
}

export interface FileStats {
  total: number;
  by_category: Partial<Record<EntityCategory, number>>;
}

export interface FileData {
  file_id: string;
  name: string;
  bounding_box: BoundingBox;
  entities: Entity[];
  delete_candidates: DeleteCandidates;
  stats: FileStats;
  /**
   * Entity ids the server has flagged for deletion at export time. Renderers
   * must skip drawing these; the backend keeps the raw entity in the response
   * payload so undo (Phase 2) can be implemented without re-fetching.
   */
  deleted_ids?: string[];
}

/** Lightweight per-file record returned in the session payload. */
export interface SessionFile {
  file_id: string;
  name: string;
  size: number;
  status: 'ready' | 'parsing' | 'error';
}

export interface Session {
  session_id: string;
  files: SessionFile[];
  expires_at: string;
}

/** Returned by POST .../delete. */
export interface DeleteResult {
  deleted_count: number;
  remaining: number;
}

/* -------------------- Phase 2: outer detection / offset ------------------- */

/** Summary of a closed outer loop. Mirrors the backend `loop_summary` shape. */
export interface LoopSummary {
  closed: boolean;
  segments: number;
  lines: number;
  arcs: number;
  perimeter: number;
  area: number;
  bounding_box: BoundingBox;
}

/** One of the alternative loops the backend evaluated during detection. */
export interface OuterCandidate {
  loop: string[];
  confidence: number;
  area: number;
  /** Which strategy produced this candidate (M1). */
  method?: string;
}

/** Returned by POST .../detect-outer and .../outer-manual. */
export interface OuterDetectionResult {
  status: 'success' | 'low_confidence' | 'failed';
  confidence: number;
  /** Winning detection strategy (M1). */
  method?: string;
  /** Entity ids that form the outer loop, in traversal order. */
  outer_loop: string[];
  loop_summary: LoopSummary;
  warnings: string[];
  candidates: OuterCandidate[];
}

export type CornerJoin = 'arc' | 'miter';

/** Request body for POST .../offset. */
export interface OffsetRequest {
  default_mm: number;
  edge_overrides: Record<string, number>;
  corner_join: CornerJoin;
}

/** Polyline geometry returned for the offset result. */
export interface OffsetLoop {
  type: 'LWPOLYLINE';
  /** Vertices as `[x, y, bulge]` triples. */
  vertices: [number, number, number][];
  closed: boolean;
}

/** Returned by POST .../offset. */
export interface OffsetResult {
  offset_loop: OffsetLoop;
  perimeter: number;
  /** Area of the offset polygon (M1). */
  area?: number;
  bounding_box: BoundingBox;
  /** Pre-formatted display string, e.g. "446 × 286 mm". */
  plate_size: string;
  material_efficiency: number;
  warnings: string[];
}

/* -------------------- Phase 3: chamfer / PDF / frame cleanup -------------- */

/** Chamfer spec sent to the backend per corner. */
export interface ChamferSpec {
  corner_id: string;
  size_mm: number;
  angle_deg: number;
  type: 'C' | 'bevel';
}

/** Outer-loop corner returned by GET /corners (mirrors backend `CornerInfo`). */
export interface CornerInfo {
  corner_id: string;
  /** [x, y] in DXF coordinates (Y-up). */
  position: [number, number];
  /** Interior angle (degrees) at the corner. */
  angle_deg: number;
  /** Acute (< 90°). */
  is_acute: boolean;
  /** 凸 (convex, outward) when true; 凹 (concave) when false. */
  is_convex: boolean;
}

/** Outer-loop edge returned by GET /corners (mirrors backend `EdgeInfo`).
 *  Used for 開先 (bevel) UI targets — the operator picks an edge to mark up. */
export interface EdgeInfo {
  edge_id: string;
  /** [x, y] of the edge midpoint in DXF coordinates. */
  midpoint: [number, number];
  /** Edge length in mm. */
  length: number;
}

/** Per-corner / per-edge annotation returned by POST /chamfer.
 *  ``kind='C'`` ⇒ C面 (corner marker); ``kind='bevel'`` ⇒ 開先 (edge mid label). */
export interface ChamferAnnotation {
  /** Corner or edge id this annotation references (``C1``..``Cn`` / ``E1``..``En``). */
  corner_id: string;
  /** [x, y] anchor in DXF coordinates — for ``C`` this is the corner, for
   *  ``bevel`` this is the edge midpoint. */
  position: [number, number];
  /** Human label, e.g. ``"C2"`` or ``"開先 30°"``. */
  label: string;
  /** Discriminator matching :class:`ChamferSpec.type`. */
  kind: 'C' | 'bevel';
}

export interface ChamferGeometry {
  items: ChamferAnnotation[];
}

/** Frame option for PDF export.
 *   - 'auto':    keep the production frame if one was detected
 *   - 'none':    no frame (current default, paper-print clean output)
 *   - 'cutflow': overlay the CutFlow material-take frame
 */
export type PdfFrameOption = 'auto' | 'none' | 'cutflow';

export interface PdfExportOptions {
  frame: PdfFrameOption;
  with_offset: boolean;
  with_chamfer: boolean;
  /** Phase 4 overlay flags (C3) — when true the backend bakes the
   *  corresponding ``CUTFLOW_*`` layer into the export. */
  with_dimensions?: boolean;
  with_added_holes?: boolean;
  with_notes?: boolean;
  with_bridges?: boolean;
  with_edits?: boolean;
  /** Optional material text rendered on the PDF header band (H4). */
  material?: string;
}

/** Mirror of PdfExportOptions but for the DXF export — only the with_*
 *  flags matter on the wire (DXF ignores ``frame`` / ``material``). */
export interface DxfExportOptions {
  with_offset?: boolean;
  with_chamfer?: boolean;
  with_dimensions?: boolean;
  with_added_holes?: boolean;
  with_notes?: boolean;
  with_bridges?: boolean;
  with_edits?: boolean;
}

/** Result of POST /cleanup-frame. */
export interface CleanupFrameResult {
  removed_count: number;
  frame_entity_ids: string[];
}

/**
 * UI-side grouping for the delete inspector. The 4 categories the v3 mockup
 * surfaces are dim / balloon / tap / frame; "other" stays hidden.
 */
export type DeleteCategoryKey = 'dim' | 'balloon' | 'tap' | 'frame';

export interface DeleteCategoryRow {
  key: DeleteCategoryKey;
  /** Japanese display name (matches v3 mockup). */
  label: string;
  /** Mono sub-caption (matches v3 mockup). */
  sub: string;
  /** Entity ids belonging to this category in the current file. */
  ids: string[];
}

/* -------------------- Phase 4: dim / edit / hole / note / bridge ---------- */

/** ISO 寸法 種別 — mirrors backend ``models.DimensionType``. */
export type DimensionType = 'linear' | 'aligned' | 'diameter' | 'radius';

/** A dimension annotation added by the user via 2-click placement (tool 5).
 *  Persisted server-side and re-emitted into the exported DXF when the
 *  ``with_dimensions`` query flag is set.
 *
 *  Backend contract (C1): ``id`` (not ``dim_id``); ``text_override`` (not
 *  ``text``); ``style`` is a free-form string identifier (defaults to ``iso``).
 *  Precision / arrow_size are UI-only and stay client-side. */
export interface Dimension {
  id: string;
  type: DimensionType;
  /** Two anchor points in DXF coordinates (Y-up). */
  p1: [number, number];
  p2: [number, number];
  /** Optional override text; when absent the backend formats the length. */
  text_override?: string | null;
  /** Style configuration (defaults to ``iso``). */
  style?: string;
}

/** Edit applied to an existing entity vertex (tool 6 — line edit mode).
 *  Backend identity is the (entity_id, vertex_index) pair — no separate
 *  ``edit_id`` is allocated (C1). The frontend tracks the prior position
 *  client-side via the ``original`` cache used only for drag previews. */
export interface EditedVertex {
  /** Target entity (LINE / LWPOLYLINE etc). */
  entity_id: string;
  /** Vertex index within the entity (0/1 for LINE start/end, 0..n for polylines). */
  vertex_index: number;
  /** New DXF position after snapping (server stores this as ``new_position``). */
  new_position: [number, number];
}

/** Snap-point query result. Backend shape:
 *  ``{snapped, type, entity_id, distance}`` (C1). */
export type SnapKind =
  | 'endpoint'
  | 'midpoint'
  | 'intersection'
  | 'center'
  | 'quadrant'
  | 'grid';
export interface SnapResult {
  /** Snapped position; ``null`` when nothing matched within the tolerance. */
  snapped: [number, number] | null;
  /** Which snap rule fired (``null`` when no match). */
  type: SnapKind | null;
  /** Originating entity id, when the snap point came from a single entity. */
  entity_id?: string | null;
  /** Distance from the query cursor to the matched snap point (mm). */
  distance?: number | null;
}

/** Hole added on top of the parsed geometry (tool 7).
 *  Emitted into the exported DXF as ``CIRCLE`` entities when
 *  ``with_added_holes`` is true. Backend contract: ``id`` + ``position``
 *  (was ``hole_id`` + ``center`` in the legacy client). */
export interface AddedHole {
  id: string;
  /** Centre in DXF coordinates. */
  position: [number, number];
  /** Diameter in mm — UI display uses ``φ{diameter}``. */
  diameter: number;
  /** Optional tap-thread note (e.g. ``"M8"``) rendered next to the hole. */
  tap_note?: string | null;
}

/** Aligned-pattern request used by the H pattern modal. */
export interface HolePatternRequest {
  /** Bottom-left hole position (was ``origin`` in the legacy client). */
  anchor: [number, number];
  rows: number;
  cols: number;
  /** [dx, dy] pitch in mm (replaces separate ``pitch_x`` / ``pitch_y``). */
  spacing: [number, number];
  diameter: number;
  tap_note?: string | null;
}

/** Note (annotation text) added by the operator (tool 8). */
export type NotePreset = 'roughness' | 'welding' | 'general';
export interface Note {
  id: string;
  /** Anchor in DXF coordinates. */
  position: [number, number];
  /** Free-form text. */
  text: string;
  /** Preset bucket (drives the v3 colour token). */
  preset: NotePreset;
  /** Font size in mm (backend field name). */
  font_size_mm: number;
  /** Rotation in degrees (defaults to 0). */
  rotation_deg?: number;
}

/* -------------------- Phase 5: nesting / history / templates -------------- */

/** Nesting algorithm key. Phase 5 ships ``bottom_left`` only;
 *  ``no_fit_polygon`` is reserved for Phase 6. Wire values match the backend
 *  ``NestAlgorithm`` literal in ``api/models/schemas.py``. */
export type NestAlgorithm = 'bottom_left' | 'no_fit_polygon';

/** Sheet wrapper used inside ``NestRequest`` — mirrors backend ``Sheet``. */
export interface NestSheetSpec {
  width_mm: number;
  height_mm: number;
  /** Backend Sheet.quantity (Phase 5 cap=20). */
  quantity: number;
}

/** Request body for POST /api/session/{sid}/nest. The backend enqueues the
 *  request and returns a job_id; the frontend polls /api/jobs/{job_id}. */
export interface NestRequest {
  /** file_ids in the session that should be packed. */
  file_ids: string[];
  /** Sheet wrapper (width_mm / height_mm / quantity). */
  sheet: NestSheetSpec;
  /** Spacing between parts (加工代) in mm. */
  spacing_mm: number;
  /** Algorithm key — ``bottom_left`` for Phase 5. */
  algorithm: NestAlgorithm;
  /** Allow 0°/90° rotation per part to improve packing. */
  rotation: boolean;
}

/** A single placed part on a sheet returned by the nesting result.
 *  Phase 5 C4: BE-aligned field names (x_mm/y_mm/width_mm/height_mm/rotation_deg). */
export interface NestPlacement {
  file_id: string;
  sheet_index: number;
  x_mm: number;
  y_mm: number;
  width_mm: number;
  height_mm: number;
  rotation_deg: number;
}

/** A single sheet in the nesting result (BE-aligned). */
export interface Sheet {
  /** 0-based sheet index (backend ``sheet_index``). */
  sheet_index: number;
  width_mm: number;
  height_mm: number;
  placements: NestPlacement[];
  /** ``used_area_mm2 / sheet_area_mm2``. */
  efficiency: number;
  used_area_mm2: number;
  sheet_area_mm2: number;
}

/** Returned by GET /api/jobs/{job_id}/result — full nesting payload.
 *  Backend envelope: ``{sheets, unplaced, warnings, utilization}``. */
export interface NestResult {
  sheets: Sheet[];
  /** Overall material efficiency across all sheets (0..1). */
  utilization: number;
  /** Number of parts that did not fit (oversized for the chosen sheet). */
  unplaced: number;
  /** Free-form warnings (oversized parts, fallback rotation, ...). */
  warnings: string[];
}

/** Job status as returned by GET /api/jobs/{job_id}.
 *  Phase 5 C2: BE-aligned (``pending`` instead of ``queued``, ``completed``
 *  instead of ``success``). */
export type JobStatus = 'pending' | 'running' | 'completed' | 'failed';

export interface Job {
  job_id: string;
  status: JobStatus;
  /** 0..1 progress; only meaningful while status='running'. */
  progress: number;
  /** Short user-facing message ('シート 1/3 を配置中…'). */
  message?: string;
  /** Optional error detail when status='failed'. */
  error?: string;
}

/** A material/spacing/sheet preset. Returned by GET /api/templates.
 *
 *  Phase 5 H1: Backend exposes both alias keys (``template_id`` / ``spacing_mm``)
 *  AND canonical keys (``id`` / ``default_offset_mm``) thanks to Pydantic
 *  alias serialisation. The frontend reads the alias-keyed form. */
export interface Template {
  template_id: string;
  /** Display name ("SS400 t9 標準"). */
  name: string;
  /** Material identifier (e.g. ``"SS400"``). */
  material: string;
  /** Sheet thickness (mm). */
  thickness_mm: number;
  /** Short caption shown under the name in the Inspector. */
  description?: string;
  /** Default 加工代 (offset) in mm — applied to the offset tool's default. */
  spacing_mm: number;
  /** Optional alternative sheet size hint — Phase 5 templates do not yet
   *  carry sheet dimensions but the wire stays open for Phase 6 additions. */
  sheet_width?: number;
  sheet_height?: number;
}

/** Returned by POST /api/sessions/{sid}/apply-template/{template_id}. (C5) */
export interface ApplyTemplateResponse {
  template_id: string;
  session_id: string;
  applied_to: string[];
  skipped: string[];
  default_offset_mm: number;
  /** Full Template echoed back so the UI can sync defaults in one pass. */
  template?: Template;
}

/** A persisted session listing (GET /api/sessions/saved). */
export interface SavedSession {
  /** Stable identifier — the operator-chosen name acts as the key. */
  name: string;
  /** ISO timestamp of last save. */
  saved_at: string;
  /** Number of DXF files included. */
  file_count: number;
  /** Archive size on disk (bytes) — only present on the live backend. */
  size_bytes?: number;
  /** Free-form note (currently the source session's first file name). */
  note?: string;
}

/** A bridge (holding tab) left on an outer-loop edge (tool 9).
 *  Backend contract (C1): identified by ``id``; the bridge lives on
 *  ``edge_id`` (``E1..En``) at fractional ``position_ratio`` ∈ [0, 1]
 *  along the edge. ``position`` is a backend-computed convenience that
 *  the frontend uses for canvas placement. */
export interface Bridge {
  id: string;
  /** Outer edge id this bridge sits on (``E1``..``En`` from /corners). */
  edge_id: string;
  /** Fractional position along the edge (0 = start, 1 = end). */
  position_ratio: number;
  /** Bridge width in mm (backend field name; renamed from ``width``). */
  width_mm: number;
  /** Backend-computed midpoint of the bridge along the edge (DXF coords).
   *  Optional because the bare POST/PUT shape doesn't include it; the
   *  serializer attaches it before the response leaves the wire. */
  position?: [number, number];
}
