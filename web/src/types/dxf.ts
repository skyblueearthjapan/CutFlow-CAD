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
  /** Optional material text rendered on the PDF header band (H4). */
  material?: string;
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
