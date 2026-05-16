/**
 * Entity bounding-box helpers shared between the canvas (drag-select preview)
 * and the session store (rectangle selection logic for the delete tool).
 *
 * Extracted from CanvasArea.vue so the store can compute hit-testing without
 * importing Vue components. Behaviour is identical to the original inline
 * implementation — only the call-site moved.
 *
 * Coordinates are always in DXF (Y-up) space; intersection / containment
 * tests treat ``min/max`` as inclusive AABBs.
 */
import type { BoundingBox, Entity } from '../types/dxf';

/** Compute an axis-aligned bbox for a single entity. Returns null for shapes
 *  we don't have a closed-form bbox for (DIMENSION/LEADER/HATCH/SOLID etc.)
 *  so callers can decide how to treat the omission (rect-select inside-mode
 *  silently skips; outside-mode treats "null" as "doesn't fit inside the
 *  rect" → optionally still selectable). */
export function entityBbox(
  e: Pick<Entity, 'type' | 'geom'>,
): BoundingBox | null {
  const g = e.geom;
  if (!g) return null;
  const num = (v: unknown): number =>
    typeof v === 'number' && Number.isFinite(v) ? v : Number.NaN;
  switch (e.type) {
    case 'LINE': {
      const x1 = num(g.x1), x2 = num(g.x2), y1 = num(g.y1), y2 = num(g.y2);
      if ([x1, x2, y1, y2].some(Number.isNaN)) return null;
      return {
        min_x: Math.min(x1, x2), max_x: Math.max(x1, x2),
        min_y: Math.min(y1, y2), max_y: Math.max(y1, y2),
      };
    }
    case 'CIRCLE':
    case 'ARC': {
      const cx = num(g.cx), cy = num(g.cy), r = num(g.r);
      if ([cx, cy, r].some(Number.isNaN)) return null;
      return { min_x: cx - r, max_x: cx + r, min_y: cy - r, max_y: cy + r };
    }
    case 'LWPOLYLINE':
    case 'POLYLINE': {
      const vs = g.vertices;
      if (!Array.isArray(vs) || vs.length === 0) return null;
      let mnx = Infinity, mny = Infinity, mxx = -Infinity, mxy = -Infinity;
      for (const v of vs) {
        if (!Array.isArray(v) || v.length < 2) continue;
        const x = num(v[0]), y = num(v[1]);
        if (Number.isNaN(x) || Number.isNaN(y)) continue;
        if (x < mnx) mnx = x; if (x > mxx) mxx = x;
        if (y < mny) mny = y; if (y > mxy) mxy = y;
      }
      if (!Number.isFinite(mnx)) return null;
      return { min_x: mnx, min_y: mny, max_x: mxx, max_y: mxy };
    }
    case 'POINT':
    case 'INSERT':
    case 'TEXT':
    case 'MTEXT': {
      const x = num(g.x), y = num(g.y);
      if (Number.isNaN(x) || Number.isNaN(y)) return null;
      return { min_x: x, min_y: y, max_x: x, max_y: y };
    }
    case 'ELLIPSE': {
      const cx = num(g.cx), cy = num(g.cy);
      const mx = num(g.major_x ?? 0), my = num(g.major_y ?? 0);
      const ratio = num(g.ratio ?? 1);
      if ([cx, cy].some(Number.isNaN)) return null;
      const a = Math.hypot(mx, my);
      const b = a * (Number.isFinite(ratio) ? ratio : 1);
      const r = Math.max(a, b);
      return { min_x: cx - r, max_x: cx + r, min_y: cy - r, max_y: cy + r };
    }
    default:
      return null;
  }
}

/** True when two AABBs overlap (touching edges count as intersecting). */
export function bboxIntersects(a: BoundingBox, b: BoundingBox): boolean {
  return (
    a.min_x <= b.max_x &&
    a.max_x >= b.min_x &&
    a.min_y <= b.max_y &&
    a.max_y >= b.min_y
  );
}

/** True when ``inner`` is fully contained inside ``outer`` (edges count as in). */
export function bboxInside(inner: BoundingBox, outer: BoundingBox): boolean {
  return (
    inner.min_x >= outer.min_x &&
    inner.max_x <= outer.max_x &&
    inner.min_y >= outer.min_y &&
    inner.max_y <= outer.max_y
  );
}
