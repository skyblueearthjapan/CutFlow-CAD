<script setup lang="ts">
/**
 * Canvas area.
 *
 * - When **no DXF is loaded** the v3 mockup SVG content is rendered verbatim
 *   so the design lives untouched at startup (1px-perfect with v3).
 * - When a DXF **is** loaded, the inner SVG body is replaced with real
 *   entities from the session store. The viewBox is derived from the file's
 *   bounding_box, and we wrap entities in `<g transform="...scale(1,-1)">`
 *   to flip DXF's Y-up coordinate system into SVG's Y-down.
 * - Entity clicks (in delete mode) toggle membership of `selectedForDelete`.
 *
 * The surrounding chrome — floating toolbar, banner, bottom-left meta and
 * bottom-right summary — is preserved in both states so the v3 layout is
 * never disturbed.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useActiveTool } from '../stores/activeTool';
import { useSession } from '../stores/session';
import { entityBbox } from '../utils/entityBbox';
import EntityRenderer from './EntityRenderer.vue';

const { showBanner, activeTool } = useActiveTool();
const {
  currentFile,
  currentFileId,
  visibleEntities,
  selectedForDelete,
  selectEntity,
  rectSelectMode,
  rectSelectInvert,
  selectByRect,
  lastError,
  remainingAfterDelete,
  isLoadingFile,
  // Phase 2
  outerEntityIdSet,
  outerDetection,
  offsetResult,
  manualMode,
  manualSelection,
  addToManual,
  // Phase 3
  corners,
  chamferGeometry,
  chamferSpecByCorner,
  chamferDefaultSize,
  setChamferSpec,
  removeChamferSpec,
  // Phase 4
  dimensions,
  vertexEdits,
  addedHoles,
  notes,
  bridges,
  pendingDimStart,
  setPendingDimStart,
  setDimTwoPointMode,
  addDimension,
  editSelection,
  editSnapEnabled,
  selectEditTarget,
  snapPoint,
  applyVertexEdit,
  addHole,
  notePreset,
  notePendingAnchor,
  setNotePendingAnchor,
  addNote,
  addBridge,
  // Phase 5
  nestingResult,
  // Phase 6 — server-rendered SVG (背景レイヤー)
  renderedSvg,
  renderMode,
  isLoadingRenderedSvg,
  loadRenderedSvg,
  toggleRenderMode,
} = useSession();

const cursorX = ref('412.0');
const cursorY = ref('218.5');
let timer: number | undefined;
let t = 0;
onMounted(() => {
  // v3 mockup keeps a slow cursor wobble for "live feel"; preserved for the
  // empty (pre-upload) state. Once a file is loaded the canvas mousemove
  // handler takes over via `onCanvasMouseMove`.
  timer = window.setInterval(() => {
    if (currentFile.value) return;
    t += 0.05;
    cursorX.value = (412 + Math.sin(t) * 0.4).toFixed(1);
    cursorY.value = (218.5 + Math.cos(t * 1.3) * 0.3).toFixed(1);
  }, 80);
});
onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer);
});

/** Bounding box that *ignores* annotation/frame entities so the viewBox
 *  zooms to the actual part rather than the surrounding production frame.
 *  Falls back to the server-reported bbox when nothing usable is found. */
const partBoundingBox = computed(() => {
  const f = currentFile.value;
  if (!f) return null;
  // Categories that represent the real part geometry. We exclude `dim`,
  // `balloon`, `tap`, and `frame` so the title block / dimension lines
  // can't blow up the viewBox.
  const partCats = new Set(['outer', 'hole', 'other']);
  let mnx = Infinity, mny = Infinity, mxx = -Infinity, mxy = -Infinity;
  let used = 0;
  for (const e of f.entities) {
    if (!partCats.has(e.category)) continue;
    // Skip TEXT/MTEXT/INSERT entities even when they happen to be 'other' —
    // a free-floating note must not stretch the viewBox.
    if (e.type === 'TEXT' || e.type === 'MTEXT' || e.type === 'INSERT' ||
        e.type === 'DIMENSION' || e.type === 'LEADER') continue;
    const bb = entityBbox(e);
    if (!bb) continue;
    if (bb.min_x < mnx) mnx = bb.min_x;
    if (bb.min_y < mny) mny = bb.min_y;
    if (bb.max_x > mxx) mxx = bb.max_x;
    if (bb.max_y > mxy) mxy = bb.max_y;
    used++;
  }
  if (used === 0 || !Number.isFinite(mnx) || mxx - mnx <= 0 || mxy - mny <= 0) {
    return f.bounding_box; // fallback
  }
  return { min_x: mnx, min_y: mny, max_x: mxx, max_y: mxy };
});

/** viewBox + Y-flip transform derived from the part bbox (zoomed to part). */
const viewBox = computed(() => {
  const f = currentFile.value;
  if (!f) return '0 0 1200 800';
  const bb = partBoundingBox.value ?? f.bounding_box;
  const w = bb.max_x - bb.min_x;
  const h = bb.max_y - bb.min_y;
  // Add 8% margin around the drawing.
  const mx = w * 0.08;
  const my = h * 0.08;
  return `${bb.min_x - mx} ${bb.min_y - my} ${w + 2 * mx} ${h + 2 * my}`;
});

/** Y-flip: translate by (max_y + min_y) so the flipped result is still in
 *  the bounding box, then scale(1,-1) to make Y point down on the screen. */
const flipTransform = computed(() => {
  const f = currentFile.value;
  if (!f) return '';
  const bb = f.bounding_box;
  return `translate(0 ${bb.max_y + bb.min_y}) scale(1 -1)`;
});

/** Y offset used by text entities to counter-flip the parent scale(1,-1).
 *  Equivalent to `max_y + min_y` above; exposed so EntityRenderer can
 *  produce upright TEXT/MTEXT (otherwise the parent scale mirrors them). */
const flipYBase = computed(() => {
  const f = currentFile.value;
  if (!f) return 0;
  return f.bounding_box.max_y + f.bounding_box.min_y;
});

/** When live data is showing, summary numbers come from the file stats.
 *  Counts use `visibleEntities` so server-deleted IDs don't inflate the total
 *  the user sees in the bottom-right HUD. */
const liveSummary = computed(() => {
  const f = currentFile.value;
  if (!f) return null;
  return {
    total: visibleEntities.value.length,
    remaining: remainingAfterDelete.value,
    selected: selectedForDelete.value.size,
  };
});

/** Whether the canvas should show live entities (vs the v3 demo). */
const hasFile = computed(() => currentFile.value !== null);

/* -------------------- Phase 6 — server-rendered SVG (背景レイヤー) -------- */

/** Show the ezdxf-rendered background only when the operator has chosen the
 *  リアル表示 mode, a file is loaded, the nest preview is NOT taking over the
 *  canvas, and we actually have a rendered payload to inject. */
const showBackgroundLayer = computed(() =>
  renderMode.value === 'real' && hasFile.value && !!renderedSvg.value,
);

/** Inner SVG content extracted from the server payload — the outer `<svg>`
 *  tag is stripped so we can re-host the children inside our own foreground-
 *  aligned wrapper with the canvas viewBox. ezdxf emits an `<svg>` root with
 *  its own viewBox; injecting that root directly would create a nested
 *  coordinate system and break alignment with the foreground operation
 *  layer. By unwrapping to children only and replaying our ``effectiveViewBox``
 *  on the host `<svg>`, both layers share the exact same DXF coord space. */
const backgroundInnerSvg = computed<string>(() => {
  const r = renderedSvg.value;
  if (!r || !r.svg) return '';
  // Strip the outermost `<svg ...>` opening + closing tags. We keep the
  // children verbatim — they retain whatever inner transforms ezdxf set up
  // (typically a translate + scale(1,-1) for Y-flip, just like our own).
  const open = r.svg.indexOf('<svg');
  if (open < 0) return r.svg;
  const openEnd = r.svg.indexOf('>', open);
  const closeIdx = r.svg.lastIndexOf('</svg>');
  if (openEnd < 0 || closeIdx < 0) return r.svg;
  return r.svg.slice(openEnd + 1, closeIdx);
});

/** Lazy-load the rendered SVG when entering real mode, when a fresh file
 *  becomes active, or after the cache was invalidated by a geometry mutation
 *  (delete / edit / cleanup-frame all call ``clearRenderedSvg`` which empties
 *  the per-file entry — the watcher below sees ``renderedSvg`` flip to null
 *  and triggers a refetch transparently). We deliberately do NOT prefetch in
 *  simple mode so a user who never flips the toggle pays no backend cost. */
watch(
  [() => currentFile.value?.file_id ?? null, renderMode, renderedSvg],
  ([fid, mode, current]) => {
    if (!fid || mode !== 'real') return;
    if (current) return;
    // Fire-and-forget; loadRenderedSvg handles its own errors → lastError.
    loadRenderedSvg(fid);
  },
  { immediate: true },
);

/** Banner shows either the manual one (outer) OR a session error. */
const showWarningBanner = computed(() => showBanner.value || !!lastError.value);
const bannerText = computed(() =>
  lastError.value
    ? lastError.value
    : '外径に閉ループ未確認の箇所が1ヶ所あります。手動で線を選択してください。',
);
const bannerTitle = computed(() => (lastError.value ? 'エラー' : '注意'));

/* -------------------- Phase 4 — coord conversion + drag state ----------- */

const svgRef = ref<SVGSVGElement | null>(null);
/** Live cursor position in DXF coordinates (Y-up). Updated on mousemove so
 *  the dim 1-point preview and snap indicator render in real time. */
const liveCursor = ref<[number, number] | null>(null);
const editDragOrigin = ref<[number, number] | null>(null);

/* -------------------- Rect-select (delete mode) ------------------------- */
/** Drag origin (DXF coords) for the delete rect-select tool. When set, the
 *  next mousemove updates ``dragRect`` and mouseup commits the rect to the
 *  store via ``selectByRect``. The 4-px click-vs-drag threshold lives in
 *  ``RECT_DRAG_THRESHOLD_PX``: anything below it is treated as a normal
 *  click so the existing per-entity toggle behaviour keeps working. */
const rectDragOrigin = ref<[number, number] | null>(null);
const rectDragOriginScreen = ref<[number, number] | null>(null);
const dragRect = ref<{
  min_x: number;
  min_y: number;
  max_x: number;
  max_y: number;
} | null>(null);
const RECT_DRAG_THRESHOLD_PX = 4;
/** True once the cursor has moved beyond the click threshold during a
 *  rect-select drag — used in mouseup to decide whether to commit the rect
 *  or fall through to the normal click handler. */
const rectDragActive = ref<boolean>(false);

/** Convert a DOM click/mouse event to DXF (Y-up) coordinates by walking the
 *  inverse of the SVG CTM and undoing the parent ``scale(1,-1)`` flip. */
function eventToDxf(e: MouseEvent): [number, number] | null {
  const svg = svgRef.value;
  const f = currentFile.value;
  if (!svg || !f) return null;
  const pt = svg.createSVGPoint();
  pt.x = e.clientX;
  pt.y = e.clientY;
  const ctm = svg.getScreenCTM();
  if (!ctm) return null;
  const inv = ctm.inverse();
  const local = pt.matrixTransform(inv);
  // Undo Y-flip applied by flipTransform — see flipTransform() above. The
  // wrapper translates by (max_y + min_y) then scales (1,-1), so the
  // SVG-space Y maps back to DXF Y as ``(max_y + min_y) - svgY``.
  const dxfY = f.bounding_box.max_y + f.bounding_box.min_y - local.y;
  return [local.x, dxfY];
}

/** Walk up the DOM to find an attribute (data-entity-id / data-corner-id). */
function findAttr(el: EventTarget | null, name: string): string | null {
  let node: SVGElement | null = el as SVGElement;
  while (node && node instanceof SVGElement) {
    const v = node.getAttribute(name);
    if (v) return v;
    node = node.parentNode as SVGElement | null;
  }
  return null;
}

/** Canvas-level click handler — propagates entity / corner hits to the store.
 *  - delete mode: toggles the entity in/out of the delete selection.
 *  - outer mode + manual chain mode: appends the entity to the manual chain
 *    (clicking the most-recent entity again pops it for one-step undo).
 *  - chamfer mode: a corner marker carries `data-corner-id` — clicking it
 *    toggles the spec via the current default size/angle. Corner clicks
 *    take precedence over entity clicks so users don't accidentally select
 *    underlying lines.
 *  - Phase 4 modes (dim / edit / hole / note / bridge): translate the click
 *    to DXF coordinates and dispatch to the appropriate store action. */
function onCanvasClick(e: MouseEvent) {
  // Chamfer mode: handle corner-id hits first (the marker sits on top of
  // entities so the user's click intent is to toggle the corner).
  if (activeTool.value === 'chamfer') {
    const cid = findAttr(e.target, 'data-corner-id');
    if (cid) {
      if (chamferSpecByCorner.value.has(cid)) {
        removeChamferSpec(cid);
      } else {
        // H6: canvas corner click is C面 only — angle is fixed 45°.
        setChamferSpec({
          corner_id: cid,
          size_mm: chamferDefaultSize.value,
          angle_deg: 45,
          type: 'C',
        });
      }
    }
    return;
  }

  /* -------------------- Phase 4 dispatch ------------------------------- */

  // dim mode — 2-click placement. Snap-assist via /snap so endpoints stick.
  if (activeTool.value === 'dim') {
    if (!currentFile.value) return;
    const p = eventToDxf(e);
    if (!p) return;
    snapPoint(p).then((snap) => {
      // C1: SnapResult shape is {snapped, type}; fall back to the raw
      // cursor when nothing matched within tolerance.
      const point: [number, number] = (snap.snapped ?? p) as [number, number];
      if (!pendingDimStart.value) {
        setPendingDimStart(point);
        setDimTwoPointMode(true);
      } else {
        // 2nd click: commit and reset.
        addDimension(pendingDimStart.value, point);
        setPendingDimStart(null);
        setDimTwoPointMode(false);
      }
    });
    return;
  }

  // edit mode — entity click selects, then drag (handled by mousedown) moves.
  if (activeTool.value === 'edit') {
    const id = findAttr(e.target, 'data-entity-id');
    if (id) {
      // Pick vertex 0 for now (LINE start); polyline vertex picking lives in
      // the drag handlers attached to per-vertex handles (see template).
      const vIdx = Number(findAttr(e.target, 'data-vertex-index') ?? 0);
      selectEditTarget(id, vIdx);
    } else {
      selectEditTarget(null);
    }
    return;
  }

  // hole mode — click adds a CIRCLE at the cursor.
  if (activeTool.value === 'hole') {
    if (!currentFile.value) return;
    const p = eventToDxf(e);
    if (!p) return;
    addHole(p);
    return;
  }

  // note mode — click opens the text-input modal anchored at this point.
  if (activeTool.value === 'note') {
    if (!currentFile.value) return;
    const p = eventToDxf(e);
    if (!p) return;
    setNotePendingAnchor(p);
    return;
  }

  // bridge mode — only outer-loop edges accept a bridge.
  // C1: addBridge takes (edge_id, position_ratio); we derive the ratio
  // from where the click landed on the chosen edge segment so the
  // backend can place the tab without us sending raw XY.
  if (activeTool.value === 'bridge') {
    const id = findAttr(e.target, 'data-entity-id');
    if (!id || !outerEntityIdSet.value.has(id)) return;
    const p = eventToDxf(e);
    if (!p) return;
    const loop = outerDetection.value?.outer_loop ?? [];
    const idx = loop.indexOf(id);
    if (idx < 0) return;
    const edge_id = `E${idx + 1}`;
    // Project click onto the LINE segment of the clicked entity (if it's
    // a LINE) for a real ratio; fall back to 0.5 for ARC/POLYLINE which
    // need server-side geometry to do precisely.
    const ent = currentFile.value?.entities.find((x) => x.id === id);
    let ratio = 0.5;
    if (ent && ent.type === 'LINE') {
      const x1 = Number(ent.geom?.x1 ?? 0);
      const y1 = Number(ent.geom?.y1 ?? 0);
      const x2 = Number(ent.geom?.x2 ?? 0);
      const y2 = Number(ent.geom?.y2 ?? 0);
      const dx = x2 - x1;
      const dy = y2 - y1;
      const len2 = dx * dx + dy * dy;
      if (len2 > 1e-12) {
        ratio = ((p[0] - x1) * dx + (p[1] - y1) * dy) / len2;
        ratio = Math.max(0, Math.min(1, ratio));
      }
    }
    addBridge(edge_id, ratio);
    return;
  }

  if (activeTool.value !== 'delete' && !(activeTool.value === 'outer' && manualMode.value)) {
    return;
  }
  // If the user just finished a rect-select drag, swallow the click so the
  // entity under the cursor isn't also toggled. ``rectDragActive`` is reset
  // in mouseup but the click event fires immediately after, so we read the
  // most recently committed rect to detect this case.
  if (
    activeTool.value === 'delete' &&
    rectSelectMode.value &&
    _rectJustCommitted
  ) {
    _rectJustCommitted = false;
    return;
  }
  const id = findAttr(e.target, 'data-entity-id');
  if (id) {
    if (activeTool.value === 'delete') {
      selectEntity(id);
    } else {
      addToManual(id);
    }
  }
}

/** Set by ``onCanvasMouseUp`` when a rect was committed; consumed by the
 *  next ``onCanvasClick`` so the trailing click event doesn't also toggle
 *  the entity under the cursor. Plain module-local mutable to avoid an
 *  extra reactive ref — the value lives for ≤1 event loop tick. */
let _rectJustCommitted = false;

/** Mouse move — drives the live cursor + dim preview + snap indicator.
 *  M5: snap requests are throttled to ~5 Hz during drags so a fast
 *  mousemove doesn't fire dozens of /snap POSTs per second (each call
 *  re-parses the DXF on the backend). The drag preview itself updates
 *  every frame for smooth visual feedback. */
let _snapThrottleTs = 0;
function onCanvasMouseMove(e: MouseEvent) {
  const p = eventToDxf(e);
  if (!p) return;
  liveCursor.value = p;
  cursorX.value = p[0].toFixed(1);
  cursorY.value = p[1].toFixed(1);
  // Delete-mode rect-select: extend the rubber-band rect while dragging.
  // We only flip ``rectDragActive`` once the cursor crosses the click
  // threshold so a quick mousedown→mouseup at the same spot still falls
  // through to the entity-click toggle.
  if (
    activeTool.value === 'delete' &&
    rectSelectMode.value &&
    rectDragOrigin.value &&
    rectDragOriginScreen.value
  ) {
    const [sx, sy] = rectDragOriginScreen.value;
    const dxPx = Math.abs(e.clientX - sx);
    const dyPx = Math.abs(e.clientY - sy);
    if (!rectDragActive.value &&
        (dxPx > RECT_DRAG_THRESHOLD_PX || dyPx > RECT_DRAG_THRESHOLD_PX)) {
      rectDragActive.value = true;
    }
    if (rectDragActive.value) {
      const [ox, oy] = rectDragOrigin.value;
      dragRect.value = {
        min_x: Math.min(ox, p[0]),
        max_x: Math.max(ox, p[0]),
        min_y: Math.min(oy, p[1]),
        max_y: Math.max(oy, p[1]),
      };
    }
    return;
  }
  // Active drag (edit mode): apply snap + ortho, redraw the moving vertex.
  if (activeTool.value === 'edit' && editDragOrigin.value && editSelection.value) {
    let next: [number, number] = p;
    if (editSnapEnabled.value) {
      const now = performance.now();
      if (now - _snapThrottleTs > 200) {
        _snapThrottleTs = now;
        snapPoint(p); // populates lastSnap for the overlay; non-blocking.
      }
    }
    if ((e.shiftKey || /* ortho toggle */ false) && editDragOrigin.value) {
      const dx = Math.abs(p[0] - editDragOrigin.value[0]);
      const dy = Math.abs(p[1] - editDragOrigin.value[1]);
      next = dx >= dy
        ? [p[0], editDragOrigin.value[1]]
        : [editDragOrigin.value[0], p[1]];
    }
    // Buffered preview only — the commit happens on mouseup so we don't spam
    // the backend with one PUT per pixel.
    dragPreview.value = next;
  }
}

/** Edit-mode drag: track preview, commit on mouseup. */
const dragPreview = ref<[number, number] | null>(null);

function onCanvasMouseDown(e: MouseEvent) {
  // Delete-mode rect-select takes precedence over the entity click handler.
  // We record both the DXF origin (for the SVG <rect> preview) and the raw
  // screen coords (so the px-based drag threshold is independent of the
  // current zoom — a 4 mm drag on a small part should still count as a
  // click, while a 4 px drag on a huge part shouldn't be counted as click
  // simply because 4 px happens to be a few mm).
  if (activeTool.value === 'delete' && rectSelectMode.value && currentFile.value) {
    const p = eventToDxf(e);
    if (p) {
      rectDragOrigin.value = p;
      rectDragOriginScreen.value = [e.clientX, e.clientY];
      rectDragActive.value = false;
      dragRect.value = null;
    }
    return;
  }
  if (activeTool.value !== 'edit') return;
  // Allow first-click-and-drag: if the user pressed on a vertex handle, select
  // it now so the subsequent mousemove drags it without requiring a separate
  // click-then-drag sequence.
  const id = findAttr(e.target, 'data-entity-id');
  if (id) {
    const vIdx = Number(findAttr(e.target, 'data-vertex-index') ?? 0);
    selectEditTarget(id, vIdx);
  }
  if (!editSelection.value && !id) return;
  const p = eventToDxf(e);
  if (!p) return;
  editDragOrigin.value = p;
  dragPreview.value = p;
}

function onCanvasMouseUp() {
  // Delete-mode rect-select: commit the rect if the drag exceeded the
  // click threshold. Otherwise leave the click handler to do its job (the
  // browser still fires `click` after mouseup for in-place releases).
  if (
    activeTool.value === 'delete' &&
    rectSelectMode.value &&
    rectDragOrigin.value
  ) {
    if (rectDragActive.value && dragRect.value && currentFileId.value) {
      selectByRect(currentFileId.value, dragRect.value, rectSelectInvert.value);
      _rectJustCommitted = true;
    }
    rectDragOrigin.value = null;
    rectDragOriginScreen.value = null;
    rectDragActive.value = false;
    dragRect.value = null;
    return;
  }
  if (
    activeTool.value === 'edit' &&
    editSelection.value &&
    editDragOrigin.value &&
    dragPreview.value
  ) {
    applyVertexEdit(
      editSelection.value.entity_id,
      editSelection.value.vertex_index,
      editDragOrigin.value,
      dragPreview.value,
    );
  }
  editDragOrigin.value = null;
  dragPreview.value = null;
}

/** Note text-input modal: bound to a Vue ref so the input renders inline. */
const noteText = ref('');
function onNoteConfirm() {
  if (!notePendingAnchor.value) return;
  addNote(notePendingAnchor.value, noteText.value);
  noteText.value = '';
}
function onNoteCancel() {
  noteText.value = '';
  setNotePendingAnchor(null);
}

/* -------------------- Phase 2 — outer / offset overlay ------------------- */

/** Set of ids currently selected via the manual chain — used by the renderer
 *  to apply the `.is-manual` highlight class. */
const manualSelectionSet = computed<Set<string>>(
  () => new Set(manualSelection.value),
);

/** SVG path `d` for the offset preview loop. The backend returns
 *  `[x, y, bulge]` triples; for the preview we draw bulge=0 segments only
 *  (full bulge handling lives in EntityRenderer for actual entities). */
const offsetPreviewPath = computed<string>(() => {
  const r = offsetResult.value;
  if (!r) return '';
  const verts = r.offset_loop.vertices;
  if (!verts || verts.length === 0) return '';
  let d = `M ${verts[0][0]} ${verts[0][1]}`;
  for (let i = 1; i < verts.length; i++) {
    d += ` L ${verts[i][0]} ${verts[i][1]}`;
  }
  if (r.offset_loop.closed) d += ' Z';
  return d;
});

/** Show the offset preview overlay only when in offset mode and we have a
 *  computed loop (avoids a stale preview leaking into other tools). */
const showOffsetPreview = computed(
  () => activeTool.value === 'offset' && !!offsetPreviewPath.value,
);

/* -------------------- Phase 3 — chamfer markers / glyphs ----------------- */

/** Show the chamfer markers only in chamfer mode (avoids purple dots leaking
 *  into other tools' canvases). */
const showChamfer = computed(
  () => activeTool.value === 'chamfer' && corners.value.length > 0,
);

/** Marker radius scaled to the file's bounding box so the corner chip stays
 *  visible regardless of part size. Min/max clamp keeps it sensible. */
const cornerMarkerRadius = computed<number>(() => {
  const f = currentFile.value;
  if (!f) return 6;
  const bb = f.bounding_box;
  const span = Math.max(bb.max_x - bb.min_x, bb.max_y - bb.min_y, 1);
  return Math.max(4, Math.min(span * 0.012, 12));
});

/** Glyph font-size, scaled with the file. */
const chamferGlyphSize = computed<number>(() => {
  const f = currentFile.value;
  if (!f) return 10;
  const bb = f.bounding_box;
  const span = Math.max(bb.max_x - bb.min_x, bb.max_y - bb.min_y, 1);
  return Math.max(8, Math.min(span * 0.018, 14));
});

/* -------------------- Phase 4 — rendering helpers ----------------------- */

/** Show dim/hole/note/bridge overlays only when the relevant tool is active
 *  OR when there is at least one item (so a switched-away tool's work stays
 *  visible in delete/outer/offset/chamfer where it makes sense as context). */
const showDimOverlay = computed(
  () => dimensions.value.length > 0 || activeTool.value === 'dim',
);
const showHoleOverlay = computed(
  () => addedHoles.value.length > 0 || activeTool.value === 'hole',
);
const showNoteOverlay = computed(
  () => notes.value.length > 0 || activeTool.value === 'note',
);
const showBridgeOverlay = computed(
  () => bridges.value.length > 0 || activeTool.value === 'bridge',
);
const showEditHandles = computed(() => activeTool.value === 'edit');

/** Tick / glyph size keyed off the file bbox so they read well at any scale. */
const overlayScale = computed<number>(() => {
  const f = currentFile.value;
  if (!f) return 1;
  const bb = f.bounding_box;
  const span = Math.max(bb.max_x - bb.min_x, bb.max_y - bb.min_y, 1);
  return Math.max(span * 0.008, 1);
});

/** Snap indicator: small ring at the snap point (only in dim/edit modes). */
const showSnapIndicator = computed(
  () =>
    (activeTool.value === 'dim' || activeTool.value === 'edit') &&
    !!liveCursor.value &&
    !!currentFile.value,
);

/** Active dim preview (rubber band) — only while waiting for the 2nd click. */
const dimPreviewStart = computed(() => pendingDimStart.value);
const dimPreviewEnd = computed(() => liveCursor.value);

/** Edited vertex lookup: entity_id+vertex_index → new position.
 *  C1: backend field is ``new_position`` (was ``position`` legacy). */
const editedVertexMap = computed<Map<string, [number, number]>>(() => {
  const map = new Map<string, [number, number]>();
  for (const e of vertexEdits.value) {
    map.set(
      `${e.entity_id}#${e.vertex_index}`,
      [e.new_position[0], e.new_position[1]],
    );
  }
  return map;
});

/** Vertex handles for the edit tool — collect (x,y) per LINE endpoint of
 *  every visible entity. Polylines / arcs are skipped for now to keep the
 *  overlay readable; expanding to other types is a follow-up (T39 spec). */
interface VertexHandle {
  entity_id: string;
  vertex_index: number;
  x: number;
  y: number;
}
const vertexHandles = computed<VertexHandle[]>(() => {
  if (!showEditHandles.value) return [];
  const out: VertexHandle[] = [];
  for (const e of visibleEntities.value) {
    if (e.type !== 'LINE') continue;
    const eMap0 = editedVertexMap.value.get(`${e.id}#0`);
    const eMap1 = editedVertexMap.value.get(`${e.id}#1`);
    out.push({
      entity_id: e.id,
      vertex_index: 0,
      x: eMap0 ? eMap0[0] : Number(e.geom?.x1 ?? 0),
      y: eMap0 ? eMap0[1] : Number(e.geom?.y1 ?? 0),
    });
    out.push({
      entity_id: e.id,
      vertex_index: 1,
      x: eMap1 ? eMap1[0] : Number(e.geom?.x2 ?? 0),
      y: eMap1 ? eMap1[1] : Number(e.geom?.y2 ?? 0),
    });
  }
  return out;
});

/** "Fit" button — for now just resets to the bounding-box viewBox (no zoom
 *  state to clear yet). Kept as a stub so the button is wired and visible. */
function onFit() {
  // No-op until pan/zoom lands in Phase 2; viewBox already follows the file.
}

/* -------------------- Phase 5 — nesting result preview ----------------- */

/** Show the nesting preview only when in nest mode AND we actually have
 *  a result (so other modes never get the layered sheet overlay). */
const showNestPreview = computed(
  () => activeTool.value === 'nest' && !!nestingResult.value,
);

/** Layout sheets side-by-side with a fixed 80 mm gap, derive a viewBox that
 *  covers everything plus an 8% margin. The result drives both the
 *  positioned `<g>` blocks and the canvas SVG viewBox while nest mode is
 *  active so the sheets are always visible without manual zoom. */
interface SheetLayout {
  index: number;
  width: number;
  height: number;
  utilization: number;
  /** X offset of the sheet's bottom-left in layout space. */
  x: number;
  /** Y offset of the sheet's bottom-left in layout space (0 here). */
  y: number;
  placements: { x: number; y: number; w: number; h: number; name: string; rotated: boolean }[];
}
const SHEET_GAP_MM = 80;
const nestLayout = computed<{ sheets: SheetLayout[]; viewBox: string }>(() => {
  const r = nestingResult.value;
  if (!r || r.sheets.length === 0) {
    return { sheets: [], viewBox: '0 0 1200 800' };
  }
  let cursor = 0;
  let maxH = 0;
  // C4: Sheet/Placement の wire field 名が BE と揃った
  // (sheet_index / width_mm / height_mm / efficiency, placement.x_mm / y_mm /
  // width_mm / height_mm / rotation_deg)。display 用 ``name`` は
  // ``file_id`` (短縮表示) で代用する。
  const sheets: SheetLayout[] = r.sheets.map((s) => {
    // Display index is 1-based for human readability; backend is 0-based.
    const displayIndex = (s.sheet_index ?? 0) + 1;
    const layout: SheetLayout = {
      index: displayIndex,
      width: s.width_mm,
      height: s.height_mm,
      utilization: s.efficiency,
      x: cursor,
      y: 0,
      placements: s.placements.map((p) => ({
        x: p.x_mm,
        y: p.y_mm,
        w: p.width_mm,
        h: p.height_mm,
        name: p.file_id.length > 12 ? `${p.file_id.slice(0, 10)}…` : p.file_id,
        rotated: (p.rotation_deg ?? 0) % 180 !== 0,
      })),
    };
    cursor += s.width_mm + SHEET_GAP_MM;
    if (s.height_mm > maxH) maxH = s.height_mm;
    return layout;
  });
  const totalW = cursor - SHEET_GAP_MM;
  const mx = totalW * 0.04;
  const my = maxH * 0.08;
  return {
    sheets,
    viewBox: `${-mx} ${-my} ${totalW + 2 * mx} ${maxH + 2 * my}`,
  };
});

/** While the nest preview is active we replace the file's bounding-box
 *  viewBox with the multi-sheet layout. Falls back to the regular file
 *  viewBox in every other mode. */
const effectiveViewBox = computed(() =>
  showNestPreview.value ? nestLayout.value.viewBox : viewBox.value,
);

/** Display-side font size — keep labels readable at any sheet size. */
function nestLabelSize(layout: SheetLayout): number {
  return Math.max(10, Math.min(layout.width * 0.02, 32));
}
function nestPlacementLabelSize(layout: SheetLayout): number {
  return Math.max(8, Math.min(layout.width * 0.012, 18));
}
</script>

<template>
  <section class="canvas-area">
    <!-- floating toolbar (kept 1:1 with v3) -->
    <div class="c-tools">
      <button title="パン (Space)"><svg><use href="#i-pan" /></svg></button>
      <button class="active" title="選択 (V)"><svg><use href="#i-select" /></svg></button>
      <div class="sep"></div>
      <button title="ズームアウト">−</button>
      <span class="zv">82%</span>
      <button title="ズームイン">+</button>
      <button title="全体表示 (F)" @click="onFit"><svg><use href="#i-fit" /></svg></button>
      <div class="sep"></div>
      <button title="元に戻す (⌘Z)"><svg><use href="#i-undo" /></svg></button>
      <button title="やり直し (⌘⇧Z)"><svg><use href="#i-redo" /></svg></button>
      <!-- Phase 6 — リアル/シンプル表示モード切替。背景に ezdxf 完全描画 SVG を
           被せるかどうかを操作する。クリックで toggle、状態は localStorage
           に永続化されるので リロードしても直前のモードを覚えている。 -->
      <div class="sep"></div>
      <button
        class="render-mode-btn"
        :class="{ active: renderMode === 'real' }"
        :title="renderMode === 'real' ? 'シンプル表示に切替 (背景OFF)' : 'リアル表示に切替 (ezdxf 完全描画)'"
        @click="toggleRenderMode"
      >
        <span class="rm-label">{{ renderMode === 'real' ? 'リアル' : 'シンプル' }}</span>
        <span v-if="renderMode === 'real' && isLoadingRenderedSvg" class="rm-spin">…</span>
      </button>
    </div>

    <div class="c-banner" :class="{ show: showWarningBanner }">
      <svg><use href="#i-warning" /></svg>
      <span><b>{{ bannerTitle }}:</b> {{ bannerText }}</span>
    </div>

    <!-- ====== Phase 6 — 背景レイヤー (ezdxf 完全描画) ======
         The server returns a full ezdxf-rendered SVG (dimensions / hatches /
         block contents all expanded). We strip its outer <svg> tag and
         re-host the children with our own viewBox so the background is
         pixel-aligned with the foreground operation layer. ``pointer-events:
         none`` keeps clicks flowing to the foreground entities. The layer
         is hidden in nest preview mode (the multi-sheet layout uses its own
         viewBox and would clash with the per-file background). -->
    <svg
      v-if="showBackgroundLayer && !showNestPreview"
      class="canvas-svg bg-layer"
      :viewBox="effectiveViewBox"
      preserveAspectRatio="xMidYMid meet"
      aria-hidden="true"
      v-html="backgroundInnerSvg"
    ></svg>

    <!-- CANVAS SVG (foreground / operation layer) -->
    <svg
      ref="svgRef"
      class="canvas-svg fg-layer"
      :class="{ 'over-real': showBackgroundLayer }"
      :viewBox="effectiveViewBox"
      preserveAspectRatio="xMidYMid meet"
      @click="onCanvasClick"
      @mousemove="onCanvasMouseMove"
      @mousedown="onCanvasMouseDown"
      @mouseup="onCanvasMouseUp"
      @mouseleave="onCanvasMouseUp"
    >
      <defs>
        <marker id="arr-am" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M0,0 L10,5 L0,10 z" fill="var(--am)" />
        </marker>
      </defs>

      <!-- ========== PHASE 5 NEST PREVIEW ========== -->
      <!-- Rendered above the regular file path so the nest result obscures
           other entities while in nest mode. The layout is in mm-space
           (matching the sheet sizes) with the viewBox swap above. -->
      <template v-if="showNestPreview">
        <g
          v-for="sh in nestLayout.sheets"
          :key="`sh-${sh.index}`"
          class="nest-sheet"
          :transform="`translate(${sh.x} ${sh.y})`"
        >
          <!-- Sheet rectangle. Y-flipped so we keep visual Y-up like the file
               canvas does: text/labels are placed inside this flipped group
               with their own counter-flip. -->
          <g :transform="`translate(0 ${sh.height}) scale(1 -1)`">
            <rect
              class="nest-sheet-bg"
              x="0"
              y="0"
              :width="sh.width"
              :height="sh.height"
            />
            <!-- Placed parts -->
            <rect
              v-for="(pl, i) in sh.placements"
              :key="`sh-${sh.index}-pl-${i}`"
              class="nest-placement"
              :x="pl.x"
              :y="pl.y"
              :width="pl.w"
              :height="pl.h"
            />
            <!-- Per-part label (counter-flipped). -->
            <g
              v-for="(pl, i) in sh.placements"
              :key="`sh-${sh.index}-lbl-${i}`"
              :transform="`scale(1, -1) translate(0, ${-2 * (pl.y + pl.h / 2)})`"
            >
              <text
                class="nest-placement-label"
                :x="pl.x + pl.w / 2"
                :y="pl.y + pl.h / 2"
                text-anchor="middle"
                dominant-baseline="middle"
                :font-size="nestPlacementLabelSize(sh)"
              >{{ pl.name }}{{ pl.rotated ? ' ↻' : '' }}</text>
            </g>
          </g>
          <!-- Sheet caption rendered above (no Y-flip, naturally upright). -->
          <text
            class="nest-sheet-caption"
            :x="0"
            :y="-10"
            :font-size="nestLabelSize(sh)"
          >シート {{ sh.index }} — {{ sh.width }} × {{ sh.height }} mm</text>
          <text
            class="nest-sheet-util"
            :x="sh.width"
            :y="-10"
            text-anchor="end"
            :font-size="nestLabelSize(sh)"
          >歩留 {{ (sh.utilization * 100).toFixed(1) }}%</text>
        </g>
      </template>

      <!-- ========== LIVE FILE PATH ========== -->
      <template v-if="hasFile && !showNestPreview">
        <!-- Y-flip wrapper: DXF is Y-up, SVG is Y-down. The list is
             `visibleEntities` so server-deleted ids never render — see
             stores/session.ts. -->
        <g :transform="flipTransform">
          <EntityRenderer
            v-for="ent in visibleEntities"
            :key="ent.id"
            :entity="ent"
            :selected="selectedForDelete.has(ent.id)"
            :is-outer="outerEntityIdSet.has(ent.id)"
            :is-manual="manualSelectionSet.has(ent.id)"
            :flip-y-base="flipYBase"
          />

          <!-- Offset preview: dashed cyan loop drawn outside the outer.
               `class="ent offset"` reuses the v3 dashed style (4dcfe0,
               stroke-dasharray 5 4); the fill rectangle uses .offset-fill
               so the body[data-mode="offset"] CSS keeps it subtle. -->
          <template v-if="showOffsetPreview">
            <path class="offset-fill" :d="offsetPreviewPath" />
            <path class="ent offset" :d="offsetPreviewPath" />
          </template>

          <!-- Chamfer annotation glyphs from the backend `chamfer/geometry`.
               Backend shape (H2): `{ corner_id, position, label, kind }`.
               C面 (kind='C')   → small purple dot + label next to the corner.
               開先 (kind='bevel') → label only, anchored at the edge midpoint.
               Only drawn in chamfer mode so it doesn't litter other tools. -->
          <template v-if="showChamfer && chamferGeometry && chamferGeometry.items.length > 0">
            <g
              v-for="ann in chamferGeometry.items"
              :key="ann.corner_id + ':ann'"
              class="chamfer-annotation"
              :class="ann.kind === 'bevel' ? 'bevel' : 'c-face'"
            >
              <circle
                v-if="ann.kind === 'C'"
                :cx="ann.position[0]"
                :cy="ann.position[1]"
                :r="cornerMarkerRadius * 0.45"
              />
              <g :transform="`scale(1, -1) translate(0, ${-2 * ann.position[1]})`">
                <text
                  class="chamfer-glyph-label"
                  :x="ann.position[0] + cornerMarkerRadius + 4"
                  :y="ann.position[1] + 4"
                  :font-size="chamferGlyphSize"
                >{{ ann.label }}</text>
              </g>
            </g>
          </template>

          <!-- Corner markers (purple chips) for clickable 角 targets. Only
               rendered in chamfer mode (showChamfer) so the dots don't litter
               other tools. Each marker carries data-corner-id so the canvas
               click handler can resolve it back to a corner. -->
          <template v-if="showChamfer">
            <g
              v-for="c in corners"
              :key="c.corner_id"
              :data-corner-id="c.corner_id"
              class="chamfer-marker"
              :class="{ on: chamferSpecByCorner.has(c.corner_id) }"
              style="cursor:pointer"
            >
              <circle
                :cx="c.position[0]"
                :cy="c.position[1]"
                :r="cornerMarkerRadius"
              />
            </g>
          </template>

          <!-- ============== Phase 4 overlays ============== -->

          <!-- Added holes: CIRCLE drawn with the v3 .ent.hole style. -->
          <template v-if="showHoleOverlay">
            <circle
              v-for="h in addedHoles"
              :key="h.id"
              class="ent hole added-hole"
              :cx="h.position[0]"
              :cy="h.position[1]"
              :r="h.diameter / 2"
            />
          </template>

          <!-- Dimensions: line with arrowheads + counter-flipped text. -->
          <template v-if="showDimOverlay">
            <g v-for="d in dimensions" :key="d.id" class="added-dim">
              <line
                class="ent dim"
                :x1="d.p1[0]"
                :y1="d.p1[1]"
                :x2="d.p2[0]"
                :y2="d.p2[1]"
                marker-end="url(#arr-am)"
                marker-start="url(#arr-am)"
              />
              <g :transform="`scale(1, -1) translate(0, ${-(d.p1[1] + d.p2[1])})`">
                <text
                  class="lbl-am"
                  :x="(d.p1[0] + d.p2[0]) / 2"
                  :y="(d.p1[1] + d.p2[1]) / 2 - overlayScale * 4"
                  text-anchor="middle"
                  :font-size="overlayScale * 10"
                >{{ d.text_override ?? Math.hypot(d.p2[0] - d.p1[0], d.p2[1] - d.p1[1]).toFixed(1) }}</text>
              </g>
            </g>
            <!-- Rubber band: 1st-point → live cursor preview. -->
            <line
              v-if="dimPreviewStart && dimPreviewEnd && activeTool === 'dim'"
              class="dim-preview"
              :x1="dimPreviewStart[0]"
              :y1="dimPreviewStart[1]"
              :x2="dimPreviewEnd[0]"
              :y2="dimPreviewEnd[1]"
            />
            <circle
              v-if="dimPreviewStart && activeTool === 'dim'"
              class="dim-anchor"
              :cx="dimPreviewStart[0]"
              :cy="dimPreviewStart[1]"
              :r="overlayScale * 2"
            />
          </template>

          <!-- Notes: counter-flipped text glyphs. -->
          <template v-if="showNoteOverlay">
            <g
              v-for="n in notes"
              :key="n.id"
              :transform="`scale(1, -1) translate(0, ${-2 * n.position[1]})`"
              class="added-note"
            >
              <text
                :class="['note-glyph', `note-${n.preset}`]"
                :x="n.position[0]"
                :y="n.position[1]"
                :font-size="overlayScale * n.font_size_mm * 4"
              >{{ n.text }}</text>
            </g>
          </template>

          <!-- Bridges: short gap-marker (white square) sitting on the edge.
               C1: ``position`` is server-computed; for bridges that have
               not been enriched yet (no outer confirmed) we skip the glyph
               so the canvas doesn't render at (0, 0). -->
          <template v-if="showBridgeOverlay">
            <rect
              v-for="b in bridges"
              v-show="b.position"
              :key="b.id"
              class="added-bridge"
              :x="(b.position?.[0] ?? 0) - b.width_mm / 2"
              :y="(b.position?.[1] ?? 0) - overlayScale * 2"
              :width="b.width_mm"
              :height="overlayScale * 4"
            />
          </template>

          <!-- Edit mode: vertex handles (green dots) for visible LINE entities. -->
          <template v-if="showEditHandles">
            <circle
              v-for="vh in vertexHandles"
              :key="`${vh.entity_id}#${vh.vertex_index}`"
              class="vertex-handle"
              :class="{
                on:
                  editSelection &&
                  editSelection.entity_id === vh.entity_id &&
                  editSelection.vertex_index === vh.vertex_index,
              }"
              :data-entity-id="vh.entity_id"
              :data-vertex-index="vh.vertex_index"
              :cx="vh.x"
              :cy="vh.y"
              :r="overlayScale * 2.5"
            />
            <!-- Drag preview: a ghost circle at the next-position. -->
            <circle
              v-if="dragPreview && editSelection"
              class="vertex-preview"
              :cx="dragPreview[0]"
              :cy="dragPreview[1]"
              :r="overlayScale * 3"
            />
          </template>

          <!-- Snap indicator: tiny ring at the snap point (cursor-following). -->
          <circle
            v-if="showSnapIndicator && liveCursor"
            class="snap-ring"
            :cx="liveCursor[0]"
            :cy="liveCursor[1]"
            :r="overlayScale * 3"
          />

          <!-- Rect-select rubber band (delete mode only). The wrapping
               Y-flip group is the same one entity rendering lives in, so the
               rect drawn here is in DXF (Y-up) coordinates and aligns with
               the entities the operator is sweeping over. The ``invert``
               variant uses a different stroke colour so the operator can
               tell at a glance whether the rect is "select inside" or
               "select outside". -->
          <rect
            v-if="dragRect"
            class="selection-box"
            :class="{ invert: rectSelectInvert }"
            :x="dragRect.min_x"
            :y="dragRect.min_y"
            :width="dragRect.max_x - dragRect.min_x"
            :height="dragRect.max_y - dragRect.min_y"
          />
        </g>
      </template>

      <!-- ========== EMPTY-STATE: v3 demo SVG (unchanged) ========== -->
      <template v-else-if="!showNestPreview">
        <!-- Origin -->
        <g transform="translate(220, 660)">
          <line x1="0" y1="0" x2="18" y2="0" stroke="var(--cy)" stroke-width="1" />
          <line x1="0" y1="0" x2="0" y2="-18" stroke="var(--cy)" stroke-width="1" />
          <circle cx="0" cy="0" r="2.5" fill="var(--cy)" />
          <text x="-6" y="14" font-family="IBM Plex Mono" font-size="9" fill="var(--cy)" text-anchor="end">0,0</text>
        </g>

        <!-- Offset preview -->
        <path
          class="offset-fill"
          d="M 196 167 L 856 167 Q 893 167 893 204 L 893 596 Q 893 633 856 633 L 196 633 Q 160 633 160 596 L 160 204 Q 160 167 196 167 Z"
          fill="rgba(77,207,224,0.06)"
        />
        <path
          class="ent offset"
          d="M 196 167 L 856 167 Q 893 167 893 204 L 893 596 Q 893 633 856 633 L 196 633 Q 160 633 160 596 L 160 204 Q 160 167 196 167 Z"
        />

        <!-- Outer -->
        <path
          class="ent outer"
          d="M 220 200 L 840 200 Q 860 200 860 220 L 860 580 Q 860 600 840 600 L 220 600 Q 200 600 200 580 L 200 220 Q 200 200 220 200 Z"
        />
        <path
          class="outer-anim"
          d="M 220 200 L 840 200 Q 860 200 860 220 L 860 580 Q 860 600 840 600 L 220 600 Q 200 600 200 580 L 200 220 Q 200 200 220 200 Z"
        />

        <!-- Chamfer -->
        <path class="ent chamfer" d="M 845 200 L 860 215" />
        <g class="ent chamfer-glyph">
          <line x1="855" y1="195" x2="865" y2="195" />
          <text x="870" y="208" font-family="IBM Plex Mono" font-size="10" fill="var(--chamfer)" stroke="none">C2</text>
        </g>

        <!-- Holes -->
        <circle class="ent hole" cx="290" cy="290" r="14" />
        <circle class="ent hole" cx="770" cy="290" r="14" />
        <circle class="ent hole" cx="290" cy="510" r="14" />
        <circle class="ent hole" cx="770" cy="510" r="14" />
        <circle class="ent hole" cx="530" cy="400" r="40" />
        <g stroke="var(--cy)" stroke-width="0.6" opacity="0.4">
          <line x1="285" y1="290" x2="295" y2="290" /><line x1="290" y1="285" x2="290" y2="295" />
          <line x1="765" y1="290" x2="775" y2="290" /><line x1="770" y1="285" x2="770" y2="295" />
          <line x1="285" y1="510" x2="295" y2="510" /><line x1="290" y1="505" x2="290" y2="515" />
          <line x1="765" y1="510" x2="775" y2="510" /><line x1="770" y1="505" x2="770" y2="515" />
          <line x1="520" y1="400" x2="540" y2="400" /><line x1="530" y1="390" x2="530" y2="410" />
        </g>
        <text class="lbl" x="304" y="280" fill="rgba(77,207,224,0.5)">φ9</text>
        <text class="lbl" x="784" y="280" fill="rgba(77,207,224,0.5)">φ9</text>
        <text class="lbl" x="304" y="500" fill="rgba(77,207,224,0.5)">φ9</text>
        <text class="lbl" x="784" y="500" fill="rgba(77,207,224,0.5)">φ9</text>
        <text class="lbl" x="558" y="378" fill="rgba(77,207,224,0.5)">φ80</text>

        <!-- Taps -->
        <g><circle class="ent tap" cx="430" cy="250" r="6" /><text class="lbl-am" x="440" y="246">M8</text></g>
        <g><circle class="ent tap" cx="630" cy="250" r="6" /><text class="lbl-am" x="640" y="246">M8</text></g>
        <g><circle class="ent tap" cx="430" cy="550" r="6" /><text class="lbl-am" x="440" y="546">M8</text></g>
        <g><circle class="ent tap" cx="630" cy="550" r="6" /><text class="lbl-am" x="640" y="546">M8</text></g>

        <!-- Dimensions -->
        <g>
          <line class="ent dim" x1="200" y1="660" x2="860" y2="660" marker-end="url(#arr-am)" marker-start="url(#arr-am)" />
          <line class="ent dim" x1="200" y1="650" x2="200" y2="670" />
          <line class="ent dim" x1="860" y1="650" x2="860" y2="670" />
          <text class="lbl-am" x="530" y="678" text-anchor="middle">440</text>
        </g>
        <g>
          <line class="ent dim" x1="130" y1="200" x2="130" y2="600" marker-end="url(#arr-am)" marker-start="url(#arr-am)" />
          <line class="ent dim" x1="120" y1="200" x2="140" y2="200" />
          <line class="ent dim" x1="120" y1="600" x2="140" y2="600" />
          <text class="lbl-am" x="118" y="404" text-anchor="end">280</text>
        </g>
        <g>
          <line class="ent dim" x1="530" y1="400" x2="690" y2="180" />
          <line class="ent dim" x1="690" y1="180" x2="730" y2="180" />
          <text class="lbl-am" x="734" y="178">φ80</text>
        </g>
        <g>
          <line class="ent dim" x1="290" y1="120" x2="770" y2="120" marker-end="url(#arr-am)" marker-start="url(#arr-am)" />
          <line class="ent dim" x1="290" y1="110" x2="290" y2="130" />
          <line class="ent dim" x1="770" y1="110" x2="770" y2="130" />
          <text class="lbl-am" x="530" y="138" text-anchor="middle">480</text>
        </g>

        <!-- Balloons -->
        <g>
          <line class="ent dim" x1="290" y1="290" x2="170" y2="100" />
          <circle class="balloon-circle" cx="160" cy="92" r="14" />
          <text class="lbl-am" x="160" y="96" text-anchor="middle" font-size="10" font-weight="600">1</text>
        </g>
        <g>
          <line class="ent dim" x1="530" y1="400" x2="950" y2="320" />
          <circle class="balloon-circle" cx="966" cy="316" r="14" />
          <text class="lbl-am" x="966" y="320" text-anchor="middle" font-size="10" font-weight="600">2</text>
        </g>

        <!-- Title frame -->
        <g>
          <rect class="ent frame" x="80" y="70" width="1040" height="660" rx="2" />
          <rect class="ent frame" x="900" y="700" width="220" height="30" />
          <line class="ent frame" x1="900" y1="715" x2="1120" y2="715" />
          <line class="ent frame" x1="1010" y1="700" x2="1010" y2="730" />
          <text class="lbl-am" x="908" y="711" font-size="9">25057-P1-03 センタープレート</text>
          <text class="lbl-am" x="908" y="726" font-size="9">SS400 t9</text>
          <text class="lbl-am" x="1018" y="711" font-size="9">SCALE 1:1</text>
          <text class="lbl-am" x="1018" y="726" font-size="9">REV. 03</text>
        </g>

        <!-- Cut sequence nodes -->
        <g class="cut-node"><circle cx="220" cy="200" r="9" /><text x="220" y="203" text-anchor="middle">1</text></g>
        <g class="cut-node"><circle cx="290" cy="290" r="8" /><text x="290" y="293" text-anchor="middle">2</text></g>
        <g class="cut-node"><circle cx="770" cy="290" r="8" /><text x="770" y="293" text-anchor="middle">3</text></g>
        <g class="cut-node"><circle cx="530" cy="400" r="8" /><text x="530" y="403" text-anchor="middle">4</text></g>
        <g class="cut-node"><circle cx="290" cy="510" r="8" /><text x="290" y="513" text-anchor="middle">5</text></g>
        <g class="cut-node"><circle cx="770" cy="510" r="8" /><text x="770" y="513" text-anchor="middle">6</text></g>
      </template>
    </svg>

    <!-- Note text-input modal (Phase 4 — note mode click). Positioned
         absolutely so it overlays the canvas without disturbing the v3 grid. -->
    <div v-if="notePendingAnchor" class="note-modal-mask" @click="onNoteCancel">
      <div class="note-modal" @click.stop>
        <h6 class="lbl">注記を追加</h6>
        <p class="ksub">
          {{ notePendingAnchor[0].toFixed(1) }}, {{ notePendingAnchor[1].toFixed(1) }}
          ·
          {{ notePreset === 'roughness' ? '面粗さ' : notePreset === 'welding' ? '溶接' : '一般' }}
        </p>
        <input
          v-model="noteText"
          type="text"
          class="note-input"
          placeholder="例: Ra 3.2 / SS400 t9 / 溶接 PL ×3"
          autofocus
          @keydown.enter="onNoteConfirm"
          @keydown.escape="onNoteCancel"
        />
        <div class="action-row">
          <button class="action-btn" @click="onNoteCancel">キャンセル</button>
          <button
            class="action-btn cy"
            :disabled="!noteText.trim()"
            @click="onNoteConfirm"
          >
            <svg><use href="#i-arrow-right" /></svg>追加
          </button>
        </div>
      </div>
    </div>

    <!-- bottom-left: meta -->
    <div class="c-meta">
      <span>x <b>{{ cursorX }}</b></span>
      <span>y <b>{{ cursorY }}</b></span>
      <span class="sep">·</span>
      <span class="cy">1 : 1</span>
      <span>mm</span>
      <template v-if="isLoadingFile">
        <span class="sep">·</span>
        <span class="cy">読込中…</span>
      </template>
    </div>

    <!-- bottom-right: live summary -->
    <div class="c-summary">
      <template v-if="liveSummary">
        <div class="item">
          <span class="lbl">エンティティ</span>
          <span class="val">{{ liveSummary.total }}</span>
        </div>
        <div class="sep"></div>
        <div class="item">
          <span class="lbl">選択中</span>
          <span class="val">{{ liveSummary.selected }}</span>
        </div>
        <div class="sep"></div>
        <div class="item time">
          <span class="lbl">削除後</span>
          <span class="val">{{ liveSummary.remaining }}</span>
        </div>
      </template>
      <template v-else>
        <div class="item">
          <span class="lbl">外周</span>
          <span class="val">1,847<span class="u">mm</span></span>
        </div>
        <div class="sep"></div>
        <div class="item">
          <span class="lbl">ピアス</span>
          <span class="val">5</span>
        </div>
        <div class="sep"></div>
        <div class="item time">
          <span class="lbl">推定加工</span>
          <span class="val">02:47</span>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
/* Phase 6 — 背景レイヤー (ezdxf 完全描画 SVG)。
   - bg-layer: 背面に配置、クリック透過。z-index は base の .canvas-svg より
     1 段下に置く (components.css 側の .canvas-svg は absolute / inset:0 のみ
     なので z-index 未指定 → 0 扱い)。
   - fg-layer.over-real: 背景がアクティブな時は前景の "other" 系エンティティ
     を半透明に落として、CAD 完全描画と二重に出ないようにする。
     outer/hole/tap などの「操作対象として強調したい」ものはそのまま残す。 */
.canvas-svg.bg-layer {
  z-index: 0;
  pointer-events: none;
  /* ezdxf rendering carries its own colour palette (white-on-dark via the
     dark_theme flag) — keep it untouched so the operator sees a 1:1 CAD
     software preview. */
  opacity: 0.95;
}
.canvas-svg.fg-layer {
  z-index: 1;
  background: transparent;
}
/* リアル表示中は前景の汎用 entity (other / dim / balloon / frame / tap) を
   半透明に落として「重複表示で読みづらい」状態を避ける。逆に削除候補や外径・
   穴は強調表示として残しておきたいので、削除選択中 (.is-selected) や outer/
   hole はフル不透明に戻す。 */
.canvas-svg.fg-layer.over-real :deep(.ent.other),
.canvas-svg.fg-layer.over-real :deep(.ent.dim),
.canvas-svg.fg-layer.over-real :deep(.ent.balloon),
.canvas-svg.fg-layer.over-real :deep(.ent.frame),
.canvas-svg.fg-layer.over-real :deep(.ent.tap) {
  opacity: 0.18;
}
.canvas-svg.fg-layer.over-real :deep(.ent.is-selected),
.canvas-svg.fg-layer.over-real :deep(.ent.is-manual),
.canvas-svg.fg-layer.over-real :deep(.ent.outer),
.canvas-svg.fg-layer.over-real :deep(.ent.hole),
.canvas-svg.fg-layer.over-real :deep(.ent.chamfer),
.canvas-svg.fg-layer.over-real :deep(.added-hole),
.canvas-svg.fg-layer.over-real :deep(.added-dim),
.canvas-svg.fg-layer.over-real :deep(.added-bridge),
.canvas-svg.fg-layer.over-real :deep(.added-note),
.canvas-svg.fg-layer.over-real :deep(.vertex-handle),
.canvas-svg.fg-layer.over-real :deep(.snap-ring),
.canvas-svg.fg-layer.over-real :deep(.chamfer-marker),
.canvas-svg.fg-layer.over-real :deep(.chamfer-annotation) {
  opacity: 1;
}

/* リアル/シンプル表示モードトグル。c-tools の他ボタンと同じ高さ・角丸で
   揃え、active 時は cyan (--cy) で点灯。 */
.render-mode-btn {
  font-family: var(--f-mono);
  font-size: 10.5px;
  letter-spacing: 0.04em;
  padding: 0 8px !important;
  min-width: 56px;
  display: inline-flex !important;
  align-items: center;
  justify-content: center;
  gap: 4px;
}
.render-mode-btn .rm-spin {
  font-family: var(--f-mono);
  color: var(--cy);
  font-size: 10px;
  opacity: 0.85;
}

/* Phase 3 — chamfer corner marker. Purple ring on top of the outer loop,
   filled when the corner has a spec. Matches the .corner-chip palette so
   the inspector and canvas read as one selection state. */
:deep(.chamfer-marker) circle {
  fill: rgba(167, 139, 250, 0.18);
  stroke: var(--chamfer);
  stroke-width: 1.4;
  transition: fill .15s, stroke-width .15s;
}
:deep(.chamfer-marker:hover) circle {
  fill: rgba(167, 139, 250, 0.32);
  stroke-width: 2;
}
:deep(.chamfer-marker.on) circle {
  fill: var(--chamfer);
  stroke: var(--chamfer);
  filter: drop-shadow(0 0 4px rgba(167, 139, 250, 0.6));
}
:deep(.chamfer-glyph-label) {
  fill: var(--chamfer);
  font-family: var(--f-mono);
  stroke: none;
  pointer-events: none;
}
/* H2 — backend-driven annotation glyph dots (C面: small purple dot beside
   the corner; bevel: label-only anchored at the edge midpoint). The bevel
   glyph reads as 注記-style, not as an interactive chip. */
:deep(.chamfer-annotation) circle {
  fill: var(--chamfer);
  stroke: none;
  pointer-events: none;
}
:deep(.chamfer-annotation.bevel) text {
  font-style: italic;
}

/* Phase 4 — added entities (dim/hole/note/bridge) and edit handles.
   Each overlay reuses v3 color tokens so the palette stays consistent. */
:deep(.added-hole) {
  fill: none;
  stroke: var(--cy);
  stroke-width: 1.5;
  stroke-dasharray: 3 2;
  opacity: 0.95;
}
:deep(.dim-preview) {
  stroke: var(--am);
  stroke-width: 0.8;
  stroke-dasharray: 4 3;
  opacity: 0.7;
  pointer-events: none;
}
:deep(.dim-anchor) {
  fill: var(--am);
  stroke: none;
  pointer-events: none;
}
:deep(.note-glyph) {
  fill: var(--chamfer);
  font-family: var(--f-mono);
  stroke: none;
  pointer-events: none;
}
:deep(.note-glyph.note-roughness) { fill: var(--cy); }
:deep(.note-glyph.note-welding) { fill: var(--am); }
:deep(.added-bridge) {
  fill: var(--bg-0);
  stroke: var(--chamfer);
  stroke-width: 1;
  opacity: 0.95;
  pointer-events: none;
}
:deep(.vertex-handle) {
  fill: var(--ok);
  stroke: var(--bg-1);
  stroke-width: 1;
  cursor: pointer;
  transition: fill .12s, r .12s;
}
:deep(.vertex-handle:hover) {
  fill: #6aa3ff;
}
:deep(.vertex-handle.on) {
  fill: #6aa3ff;
  filter: drop-shadow(0 0 4px rgba(106, 163, 255, 0.7));
}
:deep(.vertex-preview) {
  fill: rgba(106, 163, 255, 0.3);
  stroke: #6aa3ff;
  stroke-width: 1;
  pointer-events: none;
}
:deep(.snap-ring) {
  fill: none;
  stroke: var(--cy);
  stroke-width: 0.8;
  opacity: 0.8;
  pointer-events: none;
}

/* 削除モード — 矩形範囲選択のラバーバンド。
   - 既定 (範囲内モード): cyan 破線 + 薄い cyan の塗りつぶし
   - invert (範囲外モード): amber 破線 + 薄い amber 塗りつぶし
   どちらも pointer-events:none なので、ドラッグ中のヒットテストには影響しない。
   stroke-dasharray は v3 の dim-preview と同じ 4 3 パターンに揃えて、CAD らしい
   選択ボックスの見た目になる。 */
:deep(.selection-box) {
  fill: rgba(77, 207, 224, 0.08);
  stroke: var(--cy);
  stroke-width: 0.8;
  stroke-dasharray: 4 3;
  vector-effect: non-scaling-stroke;
  pointer-events: none;
}
:deep(.selection-box.invert) {
  fill: rgba(245, 166, 35, 0.08);
  stroke: var(--am);
}

/* Note text-input modal — anchored mid-canvas, matches v3 .editor cadence
   (background, border, radius). */
.note-modal-mask {
  position: absolute;
  inset: 0;
  background: rgba(8, 9, 13, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 12;
}
.note-modal {
  width: 320px;
  background: var(--bg-2);
  border: 1px solid var(--line-3);
  border-radius: var(--r-md);
  padding: 16px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.6);
}
.note-modal h6.lbl {
  font-family: var(--f-mono);
  font-size: 9.5px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--t-3);
  margin: 0 0 4px;
}
.note-modal .ksub {
  font-family: var(--f-mono);
  font-size: 10px;
  color: var(--t-4);
  margin: 0 0 10px;
}
.note-input {
  width: 100%;
  height: 32px;
  background: var(--bg-3);
  border: 1px solid var(--line-2);
  border-radius: var(--r-md);
  color: var(--t-1);
  font-family: inherit;
  font-size: 12px;
  padding: 0 10px;
  outline: none;
  box-sizing: border-box;
}
.note-input:focus { border-color: var(--cy); }

/* Phase 5 — nest preview. Sheets render as subtle bg rectangles; each placed
   part is a cyan-tinted rectangle so the operator can read at-a-glance how
   the parts pack. Labels stay legible at any scale via the sizing helpers
   in the script (nestLabelSize / nestPlacementLabelSize). */
:deep(.nest-sheet-bg) {
  fill: rgba(77, 207, 224, 0.04);
  stroke: var(--cy);
  stroke-width: 1.2;
  opacity: 0.9;
}
:deep(.nest-placement) {
  fill: rgba(77, 207, 224, 0.18);
  stroke: var(--cy);
  stroke-width: 0.8;
}
:deep(.nest-placement-label) {
  fill: var(--cy);
  font-family: var(--f-mono);
  stroke: none;
  pointer-events: none;
}
:deep(.nest-sheet-caption) {
  fill: var(--t-1);
  font-family: var(--f-mono);
  font-weight: 600;
  stroke: none;
  pointer-events: none;
}
:deep(.nest-sheet-util) {
  fill: var(--ok);
  font-family: var(--f-mono);
  font-weight: 600;
  stroke: none;
  pointer-events: none;
}
</style>
