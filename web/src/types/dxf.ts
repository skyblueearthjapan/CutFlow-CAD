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
