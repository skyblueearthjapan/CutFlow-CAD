<script setup lang="ts">
/**
 * Renders a single DXF entity inside the canvas SVG.
 *
 * - Emits the proper SVG element based on `entity.type`.
 * - Applies the category CSS class (`ent outer/hole/dim/...`) so the v3
 *   `body[data-mode]` selectors continue to drive colour states.
 * - Sets `data-entity-id` so the canvas click handler can identify hits.
 * - Adds an `.is-selected` class when the entity is in `selectedForDelete`,
 *   producing the blue highlight required by the Phase 1 brief.
 *
 * `geom` is type-narrowed inline with `as` casts (the field stays `any` in
 * types/dxf.ts — see TODO comment there). All geometry reads use the
 * `?? 0` defensive fallback so a single malformed entity does not crash
 * the whole renderer (M5).
 *
 * TEXT/MTEXT (H4): the parent `<g transform="...scale(1,-1)">` flips Y for
 * geometry; without compensation that mirrors text upside-down. We render
 * text inside a per-text `<g transform="scale(1,-1) translate(0, -2y)">` so
 * the visible character orientation is upright even though the parent flip
 * is in effect.
 */
import { computed } from 'vue';
import type { Entity } from '../types/dxf';
import { categoryClass } from '../stores/session';

const props = defineProps<{
  entity: Entity;
  selected: boolean;
  /** Optional: not used for TEXT (per-text local flip is self-contained). */
  flipYBase?: number;
}>();

const cls = computed(() => {
  const base = categoryClass(props.entity);
  return props.selected ? `${base} is-selected` : base;
});

/** Safe number coercion — returns `fallback` for null/undefined/NaN. */
function num(v: unknown, fallback = 0): number {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string') {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return fallback;
}

/** Build a polyline `d` attribute, honouring bulge between vertices.
 *  Bulge ≠ 0 means the segment is an arc; we convert using the standard
 *  formula `radius = chord / (2 * sin(2 * atan(bulge)))`. */
function polylinePath(vertices: number[][], closed: boolean): string {
  if (!vertices?.length) return '';
  const first = vertices[0];
  let d = `M ${num(first[0])} ${num(first[1])}`;
  const seg = (a: number[], b: number[]): string => {
    const x1 = num(a[0]);
    const y1 = num(a[1]);
    const x2 = num(b[0]);
    const y2 = num(b[1]);
    const bulge = num(a[2]);
    if (!bulge) return ` L ${x2} ${y2}`;
    // Arc from a → b via bulge. theta = 4*atan(|b|).
    const theta = 4 * Math.atan(Math.abs(bulge));
    const chord = Math.hypot(x2 - x1, y2 - y1);
    if (chord === 0) return ` L ${x2} ${y2}`;
    const r = chord / (2 * Math.sin(theta / 2));
    const largeArc = theta > Math.PI ? 1 : 0;
    // Bulge sign: + = CCW in DXF; the parent flip(1,-1) flips that, so we
    // pass sweep=0 for positive bulge and sweep=1 for negative bulge.
    const sweep = bulge > 0 ? 0 : 1;
    return ` A ${r} ${r} 0 ${largeArc} ${sweep} ${x2} ${y2}`;
  };
  for (let i = 1; i < vertices.length; i++) {
    d += seg(vertices[i - 1], vertices[i]);
  }
  if (closed && vertices.length > 1) {
    d += seg(vertices[vertices.length - 1], vertices[0]);
    d += ' Z';
  }
  return d;
}

/** Build the ARC `d` path attribute (sweep ccw, matching DXF convention). */
function arcPath(cx: number, cy: number, r: number, a1: number, a2: number): string {
  const rad = (deg: number) => (deg * Math.PI) / 180;
  const x1 = cx + r * Math.cos(rad(a1));
  const y1 = cy + r * Math.sin(rad(a1));
  const x2 = cx + r * Math.cos(rad(a2));
  const y2 = cy + r * Math.sin(rad(a2));
  let sweep = a2 - a1;
  while (sweep < 0) sweep += 360;
  const largeArc = sweep > 180 ? 1 : 0;
  // sweep-flag 1 = CCW in screen coords; the parent `<g scale(1,-1)>` flips
  // this back to CCW in DXF space.
  return `M ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2}`;
}

/** ELLIPSE rendering: use the SVG path A command with the major axis vector
 *  to determine size + rotation. Phase 1 ignores start/end params for
 *  simplicity — a full ellipse covers the most common drafting case. */
function ellipsePath(g: Record<string, unknown>): {
  cx: number; cy: number; rx: number; ry: number; angle: number;
} {
  const cx = num(g.cx);
  const cy = num(g.cy);
  const mx = num(g.major_x);
  const my = num(g.major_y);
  const ratio = num(g.ratio, 1);
  const rx = Math.hypot(mx, my);
  const ry = rx * ratio;
  const angle = (Math.atan2(my, mx) * 180) / Math.PI;
  return { cx, cy, rx, ry, angle };
}

/** SPLINE: Phase 1 approximation — connect the control points with straight
 *  segments. Real B-spline evaluation is deferred to Phase 2; this still
 *  shows the user *something* useful at the right location. */
function splinePath(controlPoints: number[][]): string {
  if (!controlPoints?.length) return '';
  const first = controlPoints[0];
  let d = `M ${num(first[0])} ${num(first[1])}`;
  for (let i = 1; i < controlPoints.length; i++) {
    d += ` L ${num(controlPoints[i][0])} ${num(controlPoints[i][1])}`;
  }
  return d;
}

/** LEADER: render the polyline of vertices as-is. */
function leaderPath(vertices: number[][]): string {
  if (!vertices?.length) return '';
  const first = vertices[0];
  let d = `M ${num(first[0])} ${num(first[1])}`;
  for (let i = 1; i < vertices.length; i++) {
    d += ` L ${num(vertices[i][0])} ${num(vertices[i][1])}`;
  }
  return d;
}

/** SOLID (filled triangle/quad) → SVG polygon "x,y x,y ...". */
function solidPoints(vertices: number[][]): string {
  if (!vertices?.length) return '';
  return vertices.map((v) => `${num(v[0])},${num(v[1])}`).join(' ');
}

/** TEXT/MTEXT: counter-flip transform.
 *  The parent <g> applies translate(0, max_y+min_y) scale(1,-1), so any text
 *  child would be mirrored. We apply a local `scale(1,-1) translate(0, -2y)`
 *  to undo the mirror around the entity's own Y, leaving characters upright
 *  while keeping the entity at its DXF position. */
function textTransform(y: number): string {
  return `scale(1, -1) translate(0, ${-2 * y})`;
}
</script>

<template>
  <!-- LINE -->
  <line
    v-if="entity.type === 'LINE'"
    :class="cls"
    :data-entity-id="entity.id"
    :x1="num(entity.geom?.x1)"
    :y1="num(entity.geom?.y1)"
    :x2="num(entity.geom?.x2)"
    :y2="num(entity.geom?.y2)"
  />

  <!-- CIRCLE -->
  <circle
    v-else-if="entity.type === 'CIRCLE'"
    :class="cls"
    :data-entity-id="entity.id"
    :cx="num(entity.geom?.cx)"
    :cy="num(entity.geom?.cy)"
    :r="num(entity.geom?.r)"
  />

  <!-- ARC -->
  <path
    v-else-if="entity.type === 'ARC'"
    :class="cls"
    :data-entity-id="entity.id"
    :d="arcPath(num(entity.geom?.cx), num(entity.geom?.cy), num(entity.geom?.r), num(entity.geom?.start_angle), num(entity.geom?.end_angle))"
  />

  <!-- LWPOLYLINE / POLYLINE -->
  <path
    v-else-if="entity.type === 'LWPOLYLINE' || entity.type === 'POLYLINE'"
    :class="cls"
    :data-entity-id="entity.id"
    :d="polylinePath(entity.geom?.vertices ?? [], !!entity.geom?.closed)"
  />

  <!-- ELLIPSE (Phase 1: render the full ellipse) -->
  <ellipse
    v-else-if="entity.type === 'ELLIPSE'"
    :class="cls"
    :data-entity-id="entity.id"
    :cx="ellipsePath(entity.geom ?? {}).cx"
    :cy="ellipsePath(entity.geom ?? {}).cy"
    :rx="ellipsePath(entity.geom ?? {}).rx"
    :ry="ellipsePath(entity.geom ?? {}).ry"
    :transform="`rotate(${ellipsePath(entity.geom ?? {}).angle} ${ellipsePath(entity.geom ?? {}).cx} ${ellipsePath(entity.geom ?? {}).cy})`"
  />

  <!-- SPLINE (Phase 1: straight-segment approximation through control points) -->
  <path
    v-else-if="entity.type === 'SPLINE'"
    :class="cls"
    :data-entity-id="entity.id"
    :d="splinePath(entity.geom?.control_points ?? [])"
  />

  <!-- DIMENSION (simplified: draw anchor-to-anchor lines) -->
  <g
    v-else-if="entity.type === 'DIMENSION'"
    :class="cls"
    :data-entity-id="entity.id"
  >
    <template v-if="Array.isArray(entity.geom?.anchors) && entity.geom.anchors.length >= 2">
      <line
        :x1="num(entity.geom.anchors[0][0])"
        :y1="num(entity.geom.anchors[0][1])"
        :x2="num(entity.geom.anchors[1][0])"
        :y2="num(entity.geom.anchors[1][1])"
      />
    </template>
  </g>

  <!-- LEADER -->
  <path
    v-else-if="entity.type === 'LEADER'"
    :class="cls"
    :data-entity-id="entity.id"
    :d="leaderPath(entity.geom?.vertices ?? [])"
  />

  <!-- INSERT — mocked as a small circle marker (balloon/tap) -->
  <g
    v-else-if="entity.type === 'INSERT'"
    :class="cls"
    :data-entity-id="entity.id"
  >
    <circle
      class="balloon-circle"
      :cx="num(entity.geom?.x)"
      :cy="num(entity.geom?.y)"
      :r="num(entity.geom?.radius, 8)"
    />
  </g>

  <!-- TEXT / MTEXT — counter-flipped so characters stay upright (H4) -->
  <g
    v-else-if="entity.type === 'TEXT' || entity.type === 'MTEXT'"
    :data-entity-id="entity.id"
    :transform="textTransform(num(entity.geom?.y))"
  >
    <text
      class="lbl-am"
      :x="num(entity.geom?.x)"
      :y="num(entity.geom?.y)"
      :font-size="num(entity.geom?.height, 9) || 9"
    >{{ entity.geom?.text ?? '' }}</text>
  </g>

  <!-- POINT — render as a tiny circle so it's visible -->
  <circle
    v-else-if="entity.type === 'POINT'"
    :class="cls"
    :data-entity-id="entity.id"
    :cx="num(entity.geom?.x)"
    :cy="num(entity.geom?.y)"
    r="0.5"
  />

  <!-- SOLID — filled polygon (triangle/quad) -->
  <polygon
    v-else-if="entity.type === 'SOLID'"
    :class="cls"
    :data-entity-id="entity.id"
    :points="solidPoints(entity.geom?.vertices ?? [])"
  />

  <!-- HATCH: Phase 1 explicitly skips rendering (see task brief) -->
</template>

<style scoped>
/* Selected highlight (blue overlay) for delete mode. Tuned to stay readable
   on both cyan (kept) and amber (candidate) base strokes. */
:deep(.is-selected),
.is-selected {
  stroke: #6aa3ff !important;
  stroke-width: 2.4 !important;
  opacity: 1 !important;
  filter: drop-shadow(0 0 4px rgba(106, 163, 255, 0.7));
}
:deep(.is-selected) :is(line, circle, path, ellipse, polygon) {
  stroke: #6aa3ff !important;
}
</style>
