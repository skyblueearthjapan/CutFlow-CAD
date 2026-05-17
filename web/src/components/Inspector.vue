<script setup lang="ts">
/**
 * Right inspector (340px).
 *
 * Phase 1 changes:
 *  - The **delete** panel is backed by the session store
 *    (`deleteRows`, `selectedForDelete`, `toggleCategory`, `executeDelete`).
 *
 * Phase 2 changes:
 *  - The **outer** panel: 自動検出 / 手動指定 / 信頼度ピル / 構成サマリ /
 *    閉ループ未確認の警告ストリップ。Backed by `outerDetection`,
 *    `manualMode`, `manualSelection`.
 *  - The **offset** panel: デフォルト num-step (-/+ 0.5 mm step), 角の処理
 *    トグル (arc/miter), 辺ごとリスト (LINE/ARC の長さ + 個別 num-step),
 *    適用後サマリ (外周長 / 板取り寸法 / 材料効率). Backed by
 *    `offsetResult`, `defaultOffsetMm`, `edgeOverrides`, `cornerJoin`.
 *
 * Every other tool's body is kept as the Phase 0 placeholder so we don't
 * scope-creep into chamfer/dim/edit/...
 */
import { computed, ref, watch } from 'vue';
import { useActiveTool, toolMeta } from '../stores/activeTool';
import { useSession } from '../stores/session';
import type { ChamferSpec, DimensionType, Entity, NestAlgorithm, NotePreset } from '../types/dxf';

const { activeTool } = useActiveTool();
const {
  currentFile,
  deleteRows,
  selectedForDelete,
  remainingAfterDelete,
  totalDeleteCandidates,
  toggleCategory,
  isCategoryOn,
  clearSelection,
  rectSelectMode,
  rectSelectInvert,
  protectOuterFromRect,
  setRectSelectMode,
  setRectInvert,
  setProtectOuterFromRect,
  selectNonGeometricEntities,
  executeDelete,
  isDeleting,
  // Phase 2
  outerDetection,
  offsetResult,
  manualMode,
  manualSelection,
  isDetectingOuter,
  isComputingOffset,
  defaultOffsetMm,
  edgeOverrides,
  cornerJoin,
  detectOuter,
  setManualMode,
  clearManual,
  confirmManual,
  computeOffset,
  setDefaultOffset,
  setEdgeOverride,
  clearEdgeOverride,
  setCornerJoin,
  // Phase 3
  corners,
  edges,
  chamferSpecs,
  chamferSpecByCorner,
  chamferDefaultSize,
  chamferDefaultAngle,
  isApplyingChamfer,
  isCleaningFrame,
  lastFrameCleanup,
  loadCorners,
  setChamferSpec,
  removeChamferSpec,
  clearChamfer,
  setChamferDefaultSize,
  setChamferDefaultAngle,
  cleanupFrame,
  // Phase 4
  dimensions,
  vertexEdits,
  addedHoles,
  notes,
  bridges,
  dimType,
  dimPrecision,
  dimArrowSize,
  dimTwoPointMode,
  editSnapEnabled,
  editOrtho,
  editSelection,
  holeDiameter,
  holeContinuous,
  holePatternOpen,
  holePatternRows,
  holePatternCols,
  holePatternPitchX,
  holePatternPitchY,
  notePreset,
  noteHeight,
  bridgeWidth,
  bridgeRecommended,
  setDimPrecision,
  setDimType,
  removeDimension,
  addAutoOuterDimensions,
  setHoleDiameter,
  setHoleContinuous,
  setHolePatternOpen,
  addHolePattern,
  removeHole,
  setNotePreset,
  removeNote,
  setBridgeWidth,
  setBridgeRecommended,
  addBridgeAuto,
  removeBridge,
  setEditSnap,
  setEditOrtho,
  // Phase 5
  currentSession,
  nestingJob,
  nestingResult,
  nestSheetWidth,
  nestSheetHeight,
  nestSpacingMm,
  nestAlgorithm,
  nestAllowRotate,
  nestSelectedFileIds,
  isRunningNest,
  toggleNestFile,
  setNestSheetWidth,
  setNestSheetHeight,
  setNestSpacing,
  setNestAlgorithm,
  setNestAllowRotate,
  runNesting,
  exportNestSheet,
  templates,
  isLoadingTemplates,
  loadTemplates,
  applyTemplate,
} = useSession();

const meta = computed(() => toolMeta[activeTool.value]);

/** Total number of entities that would be deleted on confirm. */
const deleteCount = computed(() => selectedForDelete.value.size);

/** Pill (top-right of tool head) for delete mode: live candidate count. */
const deletePillText = computed(() => {
  if (!currentFile.value) return meta.value.pill.text;
  return `${totalDeleteCandidates.value} 件`;
});

/** 3-way selection mode for the delete panel:
 *  - 'single': click 1本ずつ追加/取消 (デフォルト)
 *  - 'rect-inside': ドラッグした矩形の内側を一括選択
 *  - 'rect-outside': 矩形の外側を一括選択 (部品本体だけ残す用途)
 */
function setSelectionMode(mode: 'single' | 'rect-inside' | 'rect-outside'): void {
  if (mode === 'single') {
    setRectSelectMode(false);
  } else if (mode === 'rect-inside') {
    setRectSelectMode(true);
    setRectInvert(false);
  } else {
    setRectSelectMode(true);
    setRectInvert(true);
  }
}

/* -------------------- Phase 2 — outer helpers ---------------------------- */

/** Pill for the outer tool head:
 *   - manual mode → `手動 N本`
 *   - detection success/low → `自動 XX%`
 *   - detection failed → `失敗`
 *   - no detection yet → fall back to the static meta */
const outerPillText = computed(() => {
  if (manualMode.value) {
    return `手動 ${manualSelection.value.length} 本`;
  }
  const det = outerDetection.value;
  if (!det) return meta.value.pill.text;
  if (det.status === 'failed') return '失敗';
  const pct = Math.round(det.confidence * 100);
  return `${det.status === 'low_confidence' ? '要確認' : '自動'} ${pct}%`;
});

const outerPillCls = computed(() => {
  if (manualMode.value) return 'cy';
  const det = outerDetection.value;
  if (!det) return meta.value.pill.cls;
  if (det.status === 'success') return 'ok';
  if (det.status === 'low_confidence') return 'am';
  return 'am';
});

/** Pill for the offset tool head — defaults to current default value. */
const offsetPillText = computed(() => `+${defaultOffsetMm.value.toFixed(1)} mm`);

const hasOuterLoop = computed(
  () => (outerDetection.value?.outer_loop?.length ?? 0) > 0,
);

const lowConfidence = computed(
  () => outerDetection.value?.status === 'low_confidence',
);

const detectionFailed = computed(
  () => outerDetection.value?.status === 'failed',
);

/* -------------------- Phase 2 — offset helpers --------------------------- */

interface EdgeRow {
  id: string;
  ix: string;      // "E1", "E2"...
  type: string;    // "LINE" / "ARC"
  name: string;    // e.g. "上辺" / "弧 R20"
  sub: string;     // "LINE · 660 mm"
  /** Effective offset value applied to this edge (default unless overridden). */
  value: number;
  /** Whether this edge has its own override (drives amber colour). */
  overridden: boolean;
}

/** Side label heuristic: classifies a LINE by its dominant axis + position.
 *  This is a UI-only nicety so the row label reads "上辺" instead of "E1". */
function sideLabel(e: Entity, file: { bounding_box: { min_x: number; min_y: number; max_x: number; max_y: number } }): string {
  if (e.type === 'ARC') {
    const r = Number(e.geom?.r ?? 0);
    return `弧 R${Math.round(r)}`;
  }
  if (e.type !== 'LINE') return 'セグメント';
  const x1 = Number(e.geom?.x1 ?? 0);
  const y1 = Number(e.geom?.y1 ?? 0);
  const x2 = Number(e.geom?.x2 ?? 0);
  const y2 = Number(e.geom?.y2 ?? 0);
  const dx = Math.abs(x2 - x1);
  const dy = Math.abs(y2 - y1);
  const bb = file.bounding_box;
  const cy = (y1 + y2) / 2;
  const cx = (x1 + x2) / 2;
  const midY = (bb.min_y + bb.max_y) / 2;
  const midX = (bb.min_x + bb.max_x) / 2;
  if (dx >= dy) {
    return cy >= midY ? '上辺' : '下辺';
  }
  return cx >= midX ? '右辺' : '左辺';
}

function edgeLengthMm(e: Entity): number {
  if (e.type === 'LINE') {
    const x1 = Number(e.geom?.x1 ?? 0);
    const y1 = Number(e.geom?.y1 ?? 0);
    const x2 = Number(e.geom?.x2 ?? 0);
    const y2 = Number(e.geom?.y2 ?? 0);
    return Math.hypot(x2 - x1, y2 - y1);
  }
  if (e.type === 'ARC') {
    const r = Number(e.geom?.r ?? 0);
    const a1 = Number(e.geom?.start_angle ?? 0);
    const a2 = Number(e.geom?.end_angle ?? 0);
    let sweep = a2 - a1;
    while (sweep < 0) sweep += 360;
    return (Math.PI * r * sweep) / 180;
  }
  return 0;
}

/** Per-edge rows derived from the outer-detection loop.
 *
 *  Row identity is the 1-based ``EN`` label — that is the canonical key
 *  the backend's ``edge_overrides`` dict expects (C1). The underlying
 *  ``id`` field is the DXF entity id used only for sideLabel/length
 *  lookups, not for keying overrides. */
const edgeRows = computed<EdgeRow[]>(() => {
  const file = currentFile.value;
  const det = outerDetection.value;
  if (!file || !det || det.outer_loop.length === 0) return [];
  const map = new Map(file.entities.map((e) => [e.id, e]));
  return det.outer_loop.map((id, i) => {
    const e = map.get(id);
    const ix = `E${i + 1}`;
    const overridden = ix in edgeOverrides.value;
    const value = overridden ? edgeOverrides.value[ix] : defaultOffsetMm.value;
    return {
      id,
      ix,
      type: e?.type ?? '?',
      name: e ? sideLabel(e, file) : 'セグメント',
      sub: e ? `${e.type} · ${Math.round(edgeLengthMm(e))} mm` : '—',
      value,
      overridden,
    };
  });
});

/** Inline-edit state: which edge id (if any) is in num-step input mode. */
const editingEdgeId = ref<string | null>(null);
function startEdgeEdit(id: string) {
  editingEdgeId.value = id;
}
function stopEdgeEdit() {
  editingEdgeId.value = null;
}

/** Number-formatter shared by the summary block. */
function fmtNum(n: number, digits = 0): string {
  return n.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

const summaryPerimeter = computed(() => {
  const r = offsetResult.value;
  if (!r) return '—';
  return fmtNum(r.perimeter, 0);
});

const summaryPlate = computed(() => offsetResult.value?.plate_size ?? '—');

const summaryEfficiency = computed(() => {
  const r = offsetResult.value;
  if (!r) return '—';
  return `${(r.material_efficiency * 100).toFixed(1)}%`;
});

/* -------------------- Outer panel actions -------------------------------- */

function onClickAutoDetect() {
  detectOuter();
}

function onClickManualMode() {
  if (manualMode.value) {
    // 確定: 「閉じる」 — server returns 422 if not closed.
    confirmManual();
  } else {
    clearManual();
    setManualMode(true);
  }
}

function onClickCancelManual() {
  clearManual();
  setManualMode(false);
}

/* -------------------- Offset panel actions ------------------------------- */

const STEP = 0.5; // mm

function bumpDefault(delta: number) {
  setDefaultOffset(defaultOffsetMm.value + delta);
}
function onDefaultInput(e: Event) {
  const v = Number((e.target as HTMLInputElement).value);
  if (Number.isFinite(v)) setDefaultOffset(v);
}

function bumpEdge(id: string, current: number, delta: number) {
  setEdgeOverride(id, current + delta);
}
function onEdgeInput(id: string, e: Event) {
  const v = Number((e.target as HTMLInputElement).value);
  if (Number.isFinite(v)) setEdgeOverride(id, v);
}

function toggleCorner2() {
  setCornerJoin(cornerJoin.value === 'arc' ? 'miter' : 'arc');
}

const cornerLabel = computed(() =>
  cornerJoin.value === 'arc' ? '円弧で連結' : '鋭角延長',
);

/* -------------------- Auto-trigger offset on params change ---------------- */

/** Recompute the offset preview whenever the inputs change while the user is
 *  in offset mode and the outer loop is known. Auto-detect outer when the
 *  user enters offset mode without having run detection yet. */
let offsetTimer: number | undefined;
function scheduleOffset() {
  if (offsetTimer !== undefined) window.clearTimeout(offsetTimer);
  // Small debounce so num-step spam doesn't fire one request per click.
  offsetTimer = window.setTimeout(() => {
    if (activeTool.value !== 'offset') return;
    if (!hasOuterLoop.value) return;
    computeOffset();
  }, 180);
}

watch(
  () => [defaultOffsetMm.value, JSON.stringify(edgeOverrides.value), cornerJoin.value],
  () => scheduleOffset(),
);

watch(
  () => activeTool.value,
  (mode) => {
    if (mode !== 'offset') return;
    if (!currentFile.value) return;
    if (!hasOuterLoop.value) {
      // Auto-detect the outer loop so the offset panel has something to work
      // with even if the user skipped the 外形 tool entirely.
      detectOuter().then(() => {
        if (hasOuterLoop.value) computeOffset();
      });
    } else if (!offsetResult.value) {
      computeOffset();
    }
  },
);

/* -------------------- Phase 3 — chamfer helpers ------------------------- */

const CHAMFER_SIZE_STEP = 0.5;
const CHAMFER_ANGLE_STEP = 5;

/** Pill text in the tool head: count of specified corners. */
const chamferPillText = computed(() => `${chamferSpecs.value.length} ヶ所`);

/** Display label for a corner — falls back to its id if no localised name. */
const CORNER_LABELS: Record<string, string> = {
  C1: '右上',
  C2: '左上',
  C3: '左下',
  C4: '右下',
};
function chamferCornerLabel(id: string): string {
  return CORNER_LABELS[id] ?? id;
}

/** Apply the inspector's current default size + angle to the clicked target.
 *  Clicking an already-specified target removes it (matches the v3 mockup
 *  "クリックで解除" affordance). ``kind`` chooses C面 (corner) vs 開先 (edge).
 *  H6: C面 angle is fixed to 45° by convention so we never thread the
 *  bevel-default angle into a C-面 spec. */
function toggleChamferTarget(targetId: string, kind: 'C' | 'bevel') {
  if (chamferSpecByCorner.value.has(targetId)) {
    removeChamferSpec(targetId);
    return;
  }
  const spec: ChamferSpec =
    kind === 'bevel'
      ? {
          corner_id: targetId,
          // Bevel uses angle; size carries the (currently unused) leg length.
          size_mm: chamferDefaultSize.value,
          angle_deg: chamferDefaultAngle.value,
          type: 'bevel',
        }
      : {
          corner_id: targetId,
          size_mm: chamferDefaultSize.value,
          angle_deg: 45,
          type: 'C',
        };
  setChamferSpec(spec);
}

function bumpChamferSize(delta: number) {
  setChamferDefaultSize(chamferDefaultSize.value + delta);
}
function bumpChamferAngle(delta: number) {
  setChamferDefaultAngle(chamferDefaultAngle.value + delta);
}
function onChamferSizeInput(e: Event) {
  const v = (e.target as HTMLInputElement).value.replace(/^C/i, '');
  const n = Number(v);
  if (Number.isFinite(n)) setChamferDefaultSize(n);
}
function onChamferAngleInput(e: Event) {
  const n = Number((e.target as HTMLInputElement).value);
  if (Number.isFinite(n)) setChamferDefaultAngle(n);
}

/** Auto-load the corner list when the user enters chamfer mode. The /corners
 *  endpoint is cheap so we can do this without debounce. */
watch(
  () => activeTool.value,
  (mode) => {
    if (mode !== 'chamfer') return;
    if (!currentFile.value) return;
    loadCorners();
  },
);

/* -------------------- Phase 4 — pill text + helpers --------------------- */

/** Pill text for each Phase 4 tool. Counts come from the live store lists. */
const dimPillText = computed(() => `${dimensions.value.length} 件`);
const dimPillCls = computed(() =>
  dimTwoPointMode.value ? 'cy' : dimensions.value.length > 0 ? 'ok' : 'gh',
);
const editPillText = computed(() =>
  editSelection.value ? '選択 1' : `${vertexEdits.value.length} 編集`,
);
const editPillCls = computed(() => (editSelection.value ? 'cy' : 'gh'));
const holePillText = computed(() => `+${addedHoles.value.length}`);
const holePillCls = computed(() => (addedHoles.value.length > 0 ? 'ok' : 'gh'));
const notePillText = computed(() => `${notes.value.length} 件`);
const notePillCls = computed(() => (notes.value.length > 0 ? 'ok' : 'gh'));
const bridgePillText = computed(
  () => `${bridges.value.length} / ${bridgeRecommended.value}`,
);
const bridgePillCls = computed(() =>
  bridges.value.length >= bridgeRecommended.value && bridges.value.length > 0 ? 'ok' : 'gh',
);

/* dim — num-step + 2-clear keyboard */
function bumpDimPrecision(d: number) { setDimPrecision(dimPrecision.value + d); }
function onDimPrecisionInput(e: Event) {
  const n = Number((e.target as HTMLInputElement).value);
  if (Number.isFinite(n)) setDimPrecision(n);
}

/** Cycle through the four dim types — H5: backend rejects an unknown
 *  type, so we constrain to its enum here. Diameter / radius work best
 *  when the operator's first click hits the circle centre and the
 *  second click lands on the rim. */
const DIM_TYPES: DimensionType[] = ['linear', 'aligned', 'diameter', 'radius'];
const DIM_TYPE_LABEL: Record<DimensionType, string> = {
  linear: '水平/垂直 (linear)',
  aligned: '平行 (aligned)',
  diameter: '直径 (diameter)',
  radius: '半径 (radius)',
};
function cycleDimType() {
  const i = DIM_TYPES.indexOf(dimType.value);
  setDimType(DIM_TYPES[(i + 1) % DIM_TYPES.length]);
}
function fmtCoord(p: [number, number]) {
  return `${p[0].toFixed(1)}, ${p[1].toFixed(1)}`;
}
function dimLength(p1: [number, number], p2: [number, number]) {
  return Math.hypot(p2[0] - p1[0], p2[1] - p1[1]);
}

/* edit — toggles */
function toggleEditSnap() { setEditSnap(!editSnapEnabled.value); }
function toggleEditOrtho() { setEditOrtho(!editOrtho.value); }

/* hole — num-step on diameter (accepts "φ9.0" or "9") */
const HOLE_STEP = 0.5;
function bumpHole(d: number) { setHoleDiameter(holeDiameter.value + d); }
function onHoleInput(e: Event) {
  const raw = (e.target as HTMLInputElement).value.replace(/^φ/, '').trim();
  const n = Number(raw);
  if (Number.isFinite(n)) setHoleDiameter(n);
}

/** Pattern modal — confirm uses the bbox centre as origin so the row/col grid
 *  lands inside the visible drawing. Operators tweak position via canvas
 *  click later (M6). */
function onConfirmPattern() {
  const f = currentFile.value;
  if (!f) return;
  const cx = (f.bounding_box.min_x + f.bounding_box.max_x) / 2;
  const cy = (f.bounding_box.min_y + f.bounding_box.max_y) / 2;
  // Centre the grid on the bbox centre.
  const ox = cx - (holePatternCols.value - 1) * holePatternPitchX.value / 2;
  const oy = cy - (holePatternRows.value - 1) * holePatternPitchY.value / 2;
  addHolePattern([ox, oy]);
}

/* note — preset cycling (mockup's '面粗さ / 溶接 / 一般' string is read-only;
 * we offer a click-to-cycle pill so the underlying preset becomes meaningful
 * without breaking the v3 layout). */
const NOTE_PRESETS: NotePreset[] = ['roughness', 'welding', 'general'];
const NOTE_PRESET_LABEL: Record<NotePreset, string> = {
  roughness: '面粗さ',
  welding: '溶接',
  general: '一般',
};
function cycleNotePreset() {
  const i = NOTE_PRESETS.indexOf(notePreset.value);
  const next = NOTE_PRESETS[(i + 1) % NOTE_PRESETS.length];
  setNotePreset(next);
}

/* bridge — width num-step */
const BRIDGE_STEP = 0.5;
function bumpBridge(d: number) { setBridgeWidth(bridgeWidth.value + d); }
function onBridgeInput(e: Event) {
  const n = Number((e.target as HTMLInputElement).value);
  if (Number.isFinite(n)) setBridgeWidth(n);
}
function bumpBridgeRec(d: number) { setBridgeRecommended(bridgeRecommended.value + d); }

/* hole pattern handlers — explicit setters keep the template tidy and avoid
 * relying on template-side ref auto-unwrap for assignment expressions. */
function setHolePatternRows(n: number) {
  if (!Number.isFinite(n)) return;
  holePatternRows.value = Math.max(1, Math.min(50, Math.round(n)));
}
function setHolePatternCols(n: number) {
  if (!Number.isFinite(n)) return;
  holePatternCols.value = Math.max(1, Math.min(50, Math.round(n)));
}
function setHolePatternPitchX(n: number) {
  if (!Number.isFinite(n)) return;
  holePatternPitchX.value = Math.max(1, Math.min(500, n));
}
function setHolePatternPitchY(n: number) {
  if (!Number.isFinite(n)) return;
  holePatternPitchY.value = Math.max(1, Math.min(500, n));
}
function onPatternRowsInput(e: Event) {
  setHolePatternRows(Number((e.target as HTMLInputElement).value));
}
function onPatternColsInput(e: Event) {
  setHolePatternCols(Number((e.target as HTMLInputElement).value));
}
function onPatternPitchXInput(e: Event) {
  setHolePatternPitchX(Number((e.target as HTMLInputElement).value));
}
function onPatternPitchYInput(e: Event) {
  setHolePatternPitchY(Number((e.target as HTMLInputElement).value));
}

/** Re-load corners when the active file changes (in case the user was already
 *  in chamfer mode and switched tabs). */
watch(
  () => currentFile.value?.file_id ?? null,
  () => {
    if (activeTool.value !== 'chamfer') return;
    loadCorners();
  },
);

/* -------------------- Phase 5 — nesting helpers ------------------------- */

const SHEET_STEP = 50;          // mm — sheet dim step
const NEST_SPACING_STEP = 0.5;  // mm — spacing step

/** Pill text for the nest tool head — switches between the job-progress
 *  state and the post-run sheet count. */
const nestPillText = computed(() => {
  if (isRunningNest.value || (nestingJob.value && nestingJob.value.status === 'running')) {
    return `${Math.round((nestingJob.value?.progress ?? 0) * 100)}%`;
  }
  if (nestingResult.value) {
    return `${nestingResult.value.sheets.length} シート`;
  }
  return meta.value.pill.text;
});
const nestPillCls = computed(() => {
  if (nestingJob.value?.status === 'failed') return 'am';
  if (nestingResult.value) return 'ok';
  if (isRunningNest.value || nestingJob.value?.status === 'running') return 'cy';
  return 'gh';
});

/** Files exposed to the nest checkbox list — defaults to the session list
 *  (empty array when no session is active). */
const nestSessionFiles = computed(() => currentSession.value?.files ?? []);

/** Effective selection — when the user has not ticked anything we treat
 *  it as "include every file". The store uses the same convention. */
function isNestFileOn(fid: string): boolean {
  if (nestSelectedFileIds.value.size === 0) return true;
  return nestSelectedFileIds.value.has(fid);
}

/** Per-sheet utilisation as a percentage string. */
function utilPct(u: number): string {
  return `${(u * 100).toFixed(1)}%`;
}

// Phase 5 C1: BE-aligned algorithm enum. ``no_fit_polygon`` is reserved for
// Phase 6 — keeping it in the rotation list lets the operator see "future"
// algorithms but ``runNesting`` will currently reject anything but
// ``bottom_left`` via a 400 from the backend.
const NEST_ALGORITHMS: NestAlgorithm[] = ['bottom_left', 'no_fit_polygon'];
const NEST_ALGORITHM_LABEL: Record<NestAlgorithm, string> = {
  bottom_left: 'BLF (Bottom-Left-Fill)',
  no_fit_polygon: 'No-Fit Polygon (Phase 6)',
};
function cycleNestAlgorithm() {
  const i = NEST_ALGORITHMS.indexOf(nestAlgorithm.value);
  setNestAlgorithm(NEST_ALGORITHMS[(i + 1) % NEST_ALGORITHMS.length]);
}

function bumpNestWidth(d: number) { setNestSheetWidth(nestSheetWidth.value + d); }
function bumpNestHeight(d: number) { setNestSheetHeight(nestSheetHeight.value + d); }
function bumpNestSpacing(d: number) { setNestSpacing(nestSpacingMm.value + d); }
function onNestWidthInput(e: Event) {
  const n = Number((e.target as HTMLInputElement).value);
  if (Number.isFinite(n)) setNestSheetWidth(n);
}
function onNestHeightInput(e: Event) {
  const n = Number((e.target as HTMLInputElement).value);
  if (Number.isFinite(n)) setNestSheetHeight(n);
}
function onNestSpacingInput(e: Event) {
  const n = Number((e.target as HTMLInputElement).value);
  if (Number.isFinite(n)) setNestSpacing(n);
}

/** Auto-load templates the first time the user enters nest mode so the
 *  template chips render without an extra round-trip on click. */
watch(
  () => activeTool.value,
  (mode) => {
    if (mode !== 'nest') return;
    if (templates.value.length === 0 && !isLoadingTemplates.value) {
      loadTemplates();
    }
  },
);
</script>

<template>
  <aside class="editor">
    <!-- tool-head: 現在ツールのアイデンティティ -->
    <div class="tool-head" id="toolHead">
      <div class="ti"><svg><use :href="`#${meta.icon}`" /></svg></div>
      <div>
        <div class="tname">{{ meta.name }}</div>
        <div class="tsub">{{ meta.sub }}</div>
      </div>
      <span
        class="pill tpill"
        :class="
          activeTool === 'outer'
            ? outerPillCls
            : activeTool === 'dim'
            ? dimPillCls
            : activeTool === 'edit'
            ? editPillCls
            : activeTool === 'hole'
            ? holePillCls
            : activeTool === 'note'
            ? notePillCls
            : activeTool === 'bridge'
            ? bridgePillCls
            : activeTool === 'nest'
            ? nestPillCls
            : meta.pill.cls
        "
      >{{
        activeTool === 'delete'
          ? deletePillText
          : activeTool === 'outer'
          ? outerPillText
          : activeTool === 'offset'
          ? offsetPillText
          : activeTool === 'chamfer'
          ? chamferPillText
          : activeTool === 'dim'
          ? dimPillText
          : activeTool === 'edit'
          ? editPillText
          : activeTool === 'hole'
          ? holePillText
          : activeTool === 'note'
          ? notePillText
          : activeTool === 'bridge'
          ? bridgePillText
          : activeTool === 'nest'
          ? nestPillText
          : meta.pill.text
      }}</span>
    </div>

    <!-- mode-body: 各ツールごとの本文 -->
    <div class="mode-body" id="modeBody">
      <!-- ========== outer (Phase 2 — wired to session store) ========== -->
      <template v-if="activeTool === 'outer'">
        <div class="section-block">
          <!-- 1. Lead copy: switches between idle / detected / manual / failed -->
          <p v-if="manualMode" class="lead">
            キャンバスで <em>外径を構成する線</em> をクリックして連結してください。
            最後の線まで選んだら「閉じる」で確定します。
          </p>
          <p v-else-if="outerDetection && hasOuterLoop" class="lead">
            トポロジ再構築で
            <em>外径 ({{ outerDetection.loop_summary.lines ? `LINE×${outerDetection.loop_summary.lines}` : '' }}{{ outerDetection.loop_summary.lines && outerDetection.loop_summary.arcs ? ' + ' : '' }}{{ outerDetection.loop_summary.arcs ? `ARC×${outerDetection.loop_summary.arcs}` : '' }})</em>
            を検出しました。誤検出があればキャンバスから線をクリックして修正。
          </p>
          <p v-else-if="detectionFailed" class="lead">
            自動検出に失敗しました。<em>線を手動指定</em> モードでキャンバスから外径の線を順にクリックしてください。
          </p>
          <p v-else class="lead">
            <em>自動検出</em> ボタンで外径の閉ループを抽出します。誤検出があればキャンバスから線をクリックして手動修正できます。
          </p>

          <!-- 2. Composition / perimeter / area: live values when detected -->
          <template v-if="outerDetection && hasOuterLoop">
            <div class="kv">
              <div>
                <div class="k">外径構成</div>
                <div class="ksub">
                  {{ outerDetection.loop_summary.closed ? 'CLOSED' : 'OPEN' }} ·
                  {{ outerDetection.loop_summary.segments }} segments
                </div>
              </div>
              <span class="v">
                LINE {{ outerDetection.loop_summary.lines }} / ARC {{ outerDetection.loop_summary.arcs }}
              </span>
            </div>
            <div class="kv">
              <div><div class="k">外周長</div><div class="ksub">PERIMETER</div></div>
              <span class="v">{{ fmtNum(outerDetection.loop_summary.perimeter, 0) }} mm</span>
            </div>
            <div class="kv">
              <div><div class="k">面積</div><div class="ksub">AREA</div></div>
              <span class="v">{{ fmtNum(outerDetection.loop_summary.area, 0) }} mm²</span>
            </div>
          </template>
          <template v-else-if="manualMode">
            <div class="kv">
              <div><div class="k">選択中の線</div><div class="ksub">MANUAL-CHAIN</div></div>
              <span class="v">{{ manualSelection.length }} 本</span>
            </div>
          </template>
          <template v-else>
            <!-- Empty-state placeholder so the panel keeps its v3 cadence -->
            <div class="kv">
              <div><div class="k">状態</div><div class="ksub">STATUS</div></div>
              <span class="v">未検出</span>
            </div>
          </template>

          <!-- 3. Warning strip: shown for low_confidence + when detection
               has surfaced an explicit warning. -->
          <div v-if="lowConfidence || detectionFailed" class="warn-strip">
            <svg><use href="#i-warning" /></svg>
            <div>
              <b>
                {{ detectionFailed ? '自動検出に失敗' : '信頼度が低い検出' }}
                ({{ outerDetection?.warnings.length ?? 0 }})
              </b><br />
              {{ outerDetection?.warnings[0] ?? '線を手動で指定するか、削除モードで不要なエンティティを除去してください。' }}
            </div>
          </div>

          <!-- 4. Action row: layout depends on manual mode -->
          <div class="action-row">
            <template v-if="manualMode">
              <button
                class="action-btn"
                :disabled="isDetectingOuter"
                @click="onClickCancelManual"
              >キャンセル</button>
              <button
                class="action-btn cy"
                :disabled="manualSelection.length < 3 || isDetectingOuter"
                @click="onClickManualMode"
              >
                <svg><use href="#i-arrow-right" /></svg>
                {{ isDetectingOuter ? '確定中…' : '閉じる' }}
              </button>
            </template>
            <template v-else>
              <button
                class="action-btn"
                :disabled="!currentFile || isDetectingOuter"
                @click="onClickManualMode"
              >線を手動指定</button>
              <button
                class="action-btn cy"
                :disabled="!currentFile || isDetectingOuter"
                @click="onClickAutoDetect"
              >
                <svg><use href="#i-arrow-right" /></svg>
                {{ isDetectingOuter ? '検出中…' : '自動検出' }}
              </button>
            </template>
          </div>
        </div>
      </template>

      <!-- ========== delete (Phase 1 — wired to session store) ========== -->
      <template v-else-if="activeTool === 'delete'">
        <div class="section-block">
          <p class="lead">
            DXF由来の <em>製図情報</em> を出力前に取り除きます。種類別にトグル、またはキャンバスで個別選択。
          </p>

          <!-- File loaded → real rows from the parsed entity payload -->
          <template v-if="currentFile">
            <div class="entity-list">
              <div
                v-for="row in deleteRows"
                :key="row.key"
                class="ent-row"
                :class="{ on: isCategoryOn(row.key) }"
                @click="toggleCategory(row.key)"
              >
                <span class="cb"></span>
                <div>
                  <div class="nm">{{ row.label }}</div>
                  <div class="ns">{{ row.sub }}</div>
                </div>
                <span class="cnt">{{ row.ids.length }}</span>
              </div>
            </div>
          </template>

          <!-- No file → v3 mockup placeholder rows (read-only) -->
          <template v-else>
            <div class="entity-list">
              <div class="ent-row on">
                <span class="cb"></span>
                <div><div class="nm">寸法線</div><div class="ns">DIMENSION</div></div>
                <span class="cnt">5</span>
              </div>
              <div class="ent-row on">
                <span class="cb"></span>
                <div><div class="nm">バルーン</div><div class="ns">BALLOON</div></div>
                <span class="cnt">2</span>
              </div>
              <div class="ent-row on">
                <span class="cb"></span>
                <div><div class="nm">タップ穴マーク</div><div class="ns">TAP-MARK · M8 × 4</div></div>
                <span class="cnt">4</span>
              </div>
              <div class="ent-row">
                <span class="cb"></span>
                <div><div class="nm">図枠 / 表題欄</div><div class="ns">PRODUCTION-FRAME</div></div>
                <span class="cnt">1</span>
              </div>
            </div>
          </template>

          <!-- Phase 3: 製作図枠 auto-detect & cleanup -->
          <div v-if="currentFile" class="action-row" style="margin-top:10px">
            <button
              class="action-btn"
              :disabled="isCleaningFrame"
              @click="cleanupFrame"
            >
              {{ isCleaningFrame ? '検出中…' : '製作図枠を自動検出して削除' }}
            </button>
          </div>

          <!-- 選択モード切替 — 単一クリック (デフォルト) / 矩形範囲内 /
               矩形範囲外 の 3-way ラジオ。CanvasArea.vue が activeTool +
               rectSelectMode + rectSelectInvert を見て分岐する。 -->
          <div v-if="currentFile" class="section-block rect-block">
            <h6 class="lbl">選択モード</h6>
            <div class="rect-mode-switch select-mode-switch">
              <label>
                <input
                  type="radio"
                  name="select-mode"
                  :checked="!rectSelectMode"
                  @change="setSelectionMode('single')"
                />
                単一クリック選択 <small>(デフォルト)</small>
              </label>
              <label>
                <input
                  type="radio"
                  name="select-mode"
                  :checked="rectSelectMode && !rectSelectInvert"
                  @change="setSelectionMode('rect-inside')"
                />
                矩形範囲を選択
              </label>
              <label>
                <input
                  type="radio"
                  name="select-mode"
                  :checked="rectSelectMode && rectSelectInvert"
                  @change="setSelectionMode('rect-outside')"
                />
                矩形範囲外を選択 <small>(部品だけ残す)</small>
              </label>
              <label v-if="rectSelectMode && rectSelectInvert" class="rect-protect">
                <input
                  type="checkbox"
                  :checked="protectOuterFromRect"
                  @change="setProtectOuterFromRect(
                    ($event.target as HTMLInputElement).checked
                  )"
                />
                外径を保護 (推奨)
              </label>
            </div>
            <p class="lead" v-if="!rectSelectMode">
              キャンバスで線をクリック → 1本ずつ選択リストに追加・取り消し
            </p>
            <p class="lead" v-else>
              キャンバスでドラッグ →
              <em>{{ rectSelectInvert ? '矩形の外' : '矩形の内' }}</em>
              の要素を選択リストに追加
            </p>
          </div>
          <div v-if="currentFile" class="section-block">
            <h6 class="lbl">一括選択</h6>
            <button class="action-btn cy" @click="selectNonGeometricEntities">
              <svg><use href="#i-delete" /></svg>
              図形以外をすべて選択
            </button>
            <p class="lead">
              LINE / CIRCLE / ARC / LWPOLYLINE 等の「図形」を残し、TEXT・寸法・
              注記・図枠などを一括選択 (外形検出済み + 手動選択は保護)
            </p>
          </div>
          <div
            v-if="lastFrameCleanup"
            class="warn-strip"
            style="margin-top:8px;background:rgba(52,211,153,0.06);color:var(--ok);border:1px solid rgba(52,211,153,0.25)"
          >
            <svg style="fill:var(--ok)"><use href="#i-warning" /></svg>
            <div>
              <b style="color:var(--ok)">{{ lastFrameCleanup.removed_count }} 件の製作図枠を削除予約</b><br />
              書き出し時にDXFから除外されます。
            </div>
          </div>
        </div>

        <div class="summary">
          <h6>削除後プレビュー</h6>
          <template v-if="currentFile">
            <div class="summary-row">
              <span class="k">残るエンティティ</span>
              <span class="v big">{{ remainingAfterDelete }}</span>
            </div>
            <div class="summary-row">
              <span class="k">選択中</span>
              <span class="v">{{ deleteCount }} 件</span>
            </div>
            <div class="summary-row">
              <span class="k">削除候補 合計</span>
              <span class="v">{{ totalDeleteCandidates }} 件</span>
            </div>
          </template>
          <template v-else>
            <div class="summary-row"><span class="k">残るエンティティ</span><span class="v big">150</span></div>
            <div class="summary-row"><span class="k">外径</span><span class="v">12 / 12 OK</span></div>
            <div class="summary-row"><span class="k">推定切断長</span><span class="v">1,847.3<span class="u">mm</span></span></div>
          </template>
        </div>

        <div class="action-row">
          <button
            class="action-btn"
            :disabled="!currentFile || deleteCount === 0"
            @click="clearSelection"
          >選択解除</button>
          <button
            class="action-btn danger"
            :disabled="!currentFile || deleteCount === 0 || isDeleting"
            @click="executeDelete"
          >
            <svg><use href="#i-delete" /></svg>
            {{ isDeleting ? '削除中…' : `${deleteCount || (currentFile ? 0 : 12)}件を削除` }}
          </button>
        </div>
      </template>

      <!-- ========== offset (Phase 2 — wired to session store) ========== -->
      <template v-else-if="activeTool === 'offset'">
        <div class="section-block">
          <p class="lead">
            外径から外側に <em>加工代</em> を付加します。デフォルト値の後、辺ごとに個別調整できます。
          </p>

          <div class="kv">
            <div><div class="k">デフォルト</div><div class="ksub">外周全体に適用</div></div>
            <div class="num-step">
              <button :disabled="!currentFile" @click="bumpDefault(-STEP)">−</button>
              <input
                type="text"
                :value="defaultOffsetMm.toFixed(1)"
                @change="onDefaultInput"
              />
              <span class="unit">mm</span>
              <button :disabled="!currentFile" @click="bumpDefault(STEP)">+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">角の処理</div><div class="ksub">CORNER-TYPE</div></div>
            <span class="v" style="cursor:pointer" @click="toggleCorner2">{{ cornerLabel }}</span>
          </div>
        </div>

        <div class="section-block">
          <h6 class="lbl">辺ごとの個別設定 <span class="right">クリック で 編集</span></h6>
          <div v-if="edgeRows.length === 0" class="placeholder-card">
            <div class="ic"><svg><use href="#i-shape" /></svg></div>
            <h5>外径が未検出です</h5>
            <p>
              先に <b style="color:var(--t-2)">外形</b> モードで自動検出を実行するか、ファイルをアップロードしてください。
            </p>
            <span class="meta">辺 0 件</span>
          </div>
          <div v-else class="edge-list">
            <template v-for="row in edgeRows" :key="row.ix">
              <div
                v-if="editingEdgeId !== row.ix"
                class="edge-row"
                @click="startEdgeEdit(row.ix)"
              >
                <span class="ix">{{ row.ix }}</span>
                <div>
                  <div class="nm">{{ row.name }}</div>
                  <div class="ns">{{ row.sub }}</div>
                </div>
                <span class="val" :style="row.overridden ? { color: 'var(--am)' } : undefined">
                  +{{ row.value.toFixed(1) }}
                </span>
              </div>
              <div v-else class="edge-row" style="cursor:default">
                <span class="ix">{{ row.ix }}</span>
                <div>
                  <div class="nm">{{ row.name }}</div>
                  <div class="ns">{{ row.sub }}</div>
                </div>
                <div class="num-step" style="margin-left:auto">
                  <button @click="bumpEdge(row.ix, row.value, -STEP)">−</button>
                  <input
                    type="text"
                    :value="row.value.toFixed(1)"
                    @change="onEdgeInput(row.ix, $event)"
                    @blur="stopEdgeEdit"
                  />
                  <span class="unit">mm</span>
                  <button @click="bumpEdge(row.ix, row.value, STEP)">+</button>
                </div>
              </div>
            </template>
          </div>
          <div v-if="Object.keys(edgeOverrides).length > 0" class="action-row" style="margin-top:8px">
            <button
              class="action-btn"
              @click="() => { Object.keys(edgeOverrides).forEach((id) => clearEdgeOverride(id)); }"
            >個別設定をリセット</button>
          </div>
        </div>

        <div class="summary">
          <h6>加工代適用後</h6>
          <div class="summary-row">
            <span class="k">外周長</span>
            <span class="v big">{{ summaryPerimeter }}<span class="u">mm</span></span>
          </div>
          <div class="summary-row">
            <span class="k">板取り寸法</span>
            <span class="v">{{ summaryPlate }}</span>
          </div>
          <div class="summary-row">
            <span class="k">材料効率</span>
            <span class="v">{{ summaryEfficiency }}</span>
          </div>
        </div>

        <div class="action-row">
          <button
            class="action-btn"
            :disabled="!hasOuterLoop || isComputingOffset"
            @click="computeOffset"
          >
            {{ isComputingOffset ? '計算中…' : 'プレビュー再計算' }}
          </button>
          <button class="action-btn cy" :disabled="!offsetResult">
            <svg><use href="#i-arrow-right" /></svg>次へ
          </button>
        </div>
      </template>

      <!-- ========== chamfer (Phase 3 — wired to session store) ========== -->
      <template v-else-if="activeTool === 'chamfer'">
        <div class="section-block">
          <p class="lead">
            外径の <em>角</em> または <em>辺</em> をキャンバスでクリックして指定します。出力時に注記として記載されます。
          </p>

          <div class="kv">
            <div><div class="k">C面サイズ</div><div class="ksub">CHAMFER-SIZE</div></div>
            <div class="num-step">
              <button :disabled="!currentFile" @click="bumpChamferSize(-CHAMFER_SIZE_STEP)">−</button>
              <input
                type="text"
                :value="`C${chamferDefaultSize}`"
                @change="onChamferSizeInput"
              />
              <span class="unit">×45°</span>
              <button :disabled="!currentFile" @click="bumpChamferSize(CHAMFER_SIZE_STEP)">+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">開先角度</div><div class="ksub">BEVEL-ANGLE</div></div>
            <div class="num-step">
              <button :disabled="!currentFile" @click="bumpChamferAngle(-CHAMFER_ANGLE_STEP)">−</button>
              <input
                type="text"
                :value="chamferDefaultAngle"
                @change="onChamferAngleInput"
              />
              <span class="unit">°</span>
              <button :disabled="!currentFile" @click="bumpChamferAngle(CHAMFER_ANGLE_STEP)">+</button>
            </div>
          </div>
        </div>

        <div class="section-block">
          <h6 class="lbl">角 — C面 <span class="right">クリック で 解除</span></h6>
          <div v-if="corners.length === 0" class="placeholder-card">
            <div class="ic"><svg><use href="#i-chamfer" /></svg></div>
            <h5>角が未取得です</h5>
            <p>
              外径を先に <b style="color:var(--t-2)">外形</b> モードで検出するか、ファイルをアップロードしてください。
            </p>
            <span class="meta">角 0 件</span>
          </div>
          <div v-else class="corner-list">
            <div
              v-for="c in corners"
              :key="c.corner_id"
              class="corner-chip"
              :class="{ on: chamferSpecByCorner.has(c.corner_id) }"
              @click="toggleChamferTarget(c.corner_id, 'C')"
            >
              <span class="dot" :class="c.is_convex ? '' : 'concave'"></span>
              <template v-if="chamferSpecByCorner.get(c.corner_id)">
                {{ chamferCornerLabel(c.corner_id) }}
                ({{ c.is_convex ? '凸' : '凹' }}) ·
                C{{ chamferSpecByCorner.get(c.corner_id)!.size_mm }}
              </template>
              <template v-else>
                {{ chamferCornerLabel(c.corner_id) }} ({{ c.is_convex ? '凸' : '凹' }})
              </template>
            </div>
          </div>
        </div>

        <!-- H3: 辺 (開先) section — pick an edge to add a bevel note. -->
        <div v-if="edges.length > 0" class="section-block">
          <h6 class="lbl">辺 — 開先 <span class="right">クリック で 解除</span></h6>
          <div class="edge-list">
            <div
              v-for="e in edges"
              :key="e.edge_id"
              class="edge-row"
              :class="{ on: chamferSpecByCorner.has(e.edge_id) }"
              @click="toggleChamferTarget(e.edge_id, 'bevel')"
              style="cursor:pointer"
            >
              <span class="ix">{{ e.edge_id }}</span>
              <div>
                <div class="nm">辺 {{ e.edge_id }}</div>
                <div class="ns">{{ Math.round(e.length) }} mm</div>
              </div>
              <span
                class="val"
                :style="chamferSpecByCorner.has(e.edge_id) ? { color: 'var(--chamfer)' } : undefined"
              >
                <template v-if="chamferSpecByCorner.get(e.edge_id)">
                  {{ chamferSpecByCorner.get(e.edge_id)!.angle_deg }}°
                </template>
                <template v-else>—</template>
              </span>
            </div>
          </div>
        </div>

        <div
          class="summary"
          style="border-color:rgba(167,139,250,0.25);background:linear-gradient(180deg, rgba(167,139,250,0.04) 0%, rgba(167,139,250,0) 100%);"
        >
          <h6 style="color:var(--chamfer)">DXF出力時の注記</h6>
          <template v-if="chamferSpecs.length === 0">
            <div class="summary-row">
              <span class="k">C面</span>
              <span class="v">未指定</span>
            </div>
            <div class="summary-row"><span class="k">開先 (該当辺)</span><span class="v">なし</span></div>
          </template>
          <template v-else>
            <div
              v-for="s in chamferSpecs"
              :key="s.corner_id"
              class="summary-row"
            >
              <span class="k">
                {{ s.type === 'bevel' ? `${s.corner_id} 辺` : `${chamferCornerLabel(s.corner_id)} 角部` }}
              </span>
              <span class="v" style="color:var(--chamfer)">
                <template v-if="s.type === 'bevel'">開先 {{ s.angle_deg }}°</template>
                <template v-else>C{{ s.size_mm }}</template>
              </span>
            </div>
          </template>
        </div>

        <div class="action-row">
          <button
            class="action-btn"
            :disabled="chamferSpecs.length === 0 || isApplyingChamfer"
            @click="clearChamfer"
          >
            {{ isApplyingChamfer ? '適用中…' : '指定をクリア' }}
          </button>
          <button class="action-btn cy" :disabled="!currentFile"><svg><use href="#i-arrow-right" /></svg>出力へ</button>
        </div>
      </template>

      <!-- ========== dim (Phase 4) ========== -->
      <template v-else-if="activeTool === 'dim'">
        <div class="section-block">
          <p class="lead">
            出力DXFに <em>注釈寸法</em> を残したい場合に使用します。「削除」で消した寸法とは別に、加工指示として必要な寸法だけを再付加します。
          </p>

          <div class="kv">
            <div><div class="k">スタイル</div><div class="ksub">DIM-STYLE</div></div>
            <span class="v">ISO 標準</span>
          </div>
          <!-- H5: dimension type selector — backend defaults to 'linear'
               when omitted, so exposing it here lets the operator pick
               diameter / radius / aligned for circular features etc. -->
          <div class="kv">
            <div><div class="k">種別</div><div class="ksub">DIM-TYPE</div></div>
            <span class="v" style="cursor:pointer" @click="cycleDimType">
              {{ DIM_TYPE_LABEL[dimType] }}
            </span>
          </div>
          <div class="kv">
            <div><div class="k">小数桁</div><div class="ksub">PRECISION</div></div>
            <div class="num-step">
              <button :disabled="!currentFile" @click="bumpDimPrecision(-1)">−</button>
              <input
                type="text"
                :value="dimPrecision"
                @change="onDimPrecisionInput"
              />
              <span class="unit">桁</span>
              <button :disabled="!currentFile" @click="bumpDimPrecision(1)">+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">矢印サイズ</div><div class="ksub">ARROW-SIZE</div></div>
            <span class="v">{{ dimArrowSize.toFixed(1) }} mm</span>
          </div>
        </div>

        <div class="section-block">
          <h6 class="lbl">自動寸法</h6>
          <button
            class="action-btn cy"
            :disabled="!currentFile"
            @click="addAutoOuterDimensions"
          >
            <svg><use href="#i-dim" /></svg>
            外形寸法を自動付与 (横×縦)
          </button>
          <p class="lead">
            確定済みの外径から bbox を計算し、上方向に横寸法・右方向に縦寸法を
            自動配置します (外径未確定の場合はエラー)。
          </p>
        </div>

        <div v-if="dimensions.length === 0" class="placeholder-card">
          <div class="ic"><svg><use href="#i-dim" /></svg></div>
          <h5>寸法を追加するには</h5>
          <p>
            キャンバスで 2 点をクリックすると、その間の寸法線がここに表示されます。<br />
            キーボード <b style="color:var(--t-2)">D</b> で2点間モード。
          </p>
          <span class="meta">
            {{ dimTwoPointMode ? '2点間モード — 1点目を指定' : '追加済み 0 件' }}
          </span>
        </div>
        <div v-else class="section-block">
          <h6 class="lbl">追加済み <span class="right">クリック で 削除</span></h6>
          <div class="edge-list">
            <div
              v-for="(d, i) in dimensions"
              :key="d.id"
              class="edge-row"
              style="cursor:default"
            >
              <span class="ix">D{{ i + 1 }}</span>
              <div>
                <div class="nm">{{ d.text_override ?? dimLength(d.p1, d.p2).toFixed(dimPrecision) + ' mm' }}</div>
                <div class="ns">{{ fmtCoord(d.p1) }} → {{ fmtCoord(d.p2) }}</div>
              </div>
              <button
                class="row-x"
                title="削除"
                @click="removeDimension(d.id)"
              >×</button>
            </div>
          </div>
          <div
            v-if="dimTwoPointMode"
            class="warn-strip"
            style="margin-top:10px;background:var(--cy-soft);color:var(--cy);border:1px solid rgba(77,207,224,0.25)"
          >
            <svg style="fill:var(--cy)"><use href="#i-warning" /></svg>
            <div>
              <b style="color:var(--cy)">2点間モード</b><br />
              キャンバスで 1点目 → 2点目 をクリックして寸法を確定します。
            </div>
          </div>
        </div>
      </template>

      <!-- ========== edit (Phase 4) ========== -->
      <template v-else-if="activeTool === 'edit'">
        <div class="section-block">
          <p class="lead">
            外径や穴の <em>頂点・線分</em> を直接ドラッグして編集できます。スナップ・寸法表示は自動で有効になります。
          </p>

          <div class="kv">
            <div><div class="k">スナップ</div><div class="ksub">SNAP</div></div>
            <span class="v" style="cursor:pointer" @click="toggleEditSnap">
              {{ editSnapEnabled ? '端点 + 中点 + 交点' : 'OFF' }}
            </span>
          </div>
          <div class="kv">
            <div><div class="k">グリッド吸着</div><div class="ksub">GRID-SNAP</div></div>
            <span class="v">1 mm</span>
          </div>
          <div class="kv">
            <div><div class="k">直交モード</div><div class="ksub">ORTHO</div></div>
            <span class="v" style="cursor:pointer" @click="toggleEditOrtho">
              {{ editOrtho ? 'ON' : 'OFF' }} (Shift)
            </span>
          </div>
        </div>

        <div v-if="!editSelection" class="placeholder-card">
          <div class="ic"><svg><use href="#i-edit-line" /></svg></div>
          <h5>線を選択してください</h5>
          <p>
            キャンバス上の <b style="color:var(--t-2)">頂点</b> または <b style="color:var(--t-2)">線分</b>
            をクリックすると、ここに座標・長さ・角度が表示され、ドラッグで編集できます。
          </p>
          <span class="meta">
            選択 0 / {{ currentFile?.entities.length ?? 0 }} entities
            <template v-if="vertexEdits.length > 0"> · 編集済み {{ vertexEdits.length }} 件</template>
          </span>
        </div>
        <div v-else class="section-block">
          <h6 class="lbl">選択中 <span class="right">ドラッグ で 移動</span></h6>
          <div class="kv">
            <div><div class="k">エンティティ</div><div class="ksub">ENTITY-ID</div></div>
            <span class="v" style="font-size:10.5px">{{ editSelection.entity_id }}</span>
          </div>
          <div class="kv">
            <div><div class="k">頂点</div><div class="ksub">VERTEX-INDEX</div></div>
            <span class="v">#{{ editSelection.vertex_index }}</span>
          </div>
          <div
            v-if="vertexEdits.length > 0"
            class="warn-strip"
            style="margin-top:10px;background:var(--cy-soft);color:var(--cy);border:1px solid rgba(77,207,224,0.25)"
          >
            <svg style="fill:var(--cy)"><use href="#i-warning" /></svg>
            <div>
              <b style="color:var(--cy)">編集済み {{ vertexEdits.length }} 件</b><br />
              書き出し時にDXFへ反映されます。
            </div>
          </div>
        </div>
      </template>

      <!-- ========== hole (Phase 4) ========== -->
      <template v-else-if="activeTool === 'hole'">
        <div class="section-block">
          <p class="lead">
            外径の内側の任意位置に <em>穴を追加</em> します。座標指定 / クリック配置 / 整列パターン に対応。
          </p>

          <div class="kv">
            <div><div class="k">穴径</div><div class="ksub">DIAMETER</div></div>
            <div class="num-step">
              <button :disabled="!currentFile" @click="bumpHole(-HOLE_STEP)">−</button>
              <input
                type="text"
                :value="`φ${holeDiameter.toFixed(1)}`"
                @change="onHoleInput"
              />
              <span class="unit">mm</span>
              <button :disabled="!currentFile" @click="bumpHole(HOLE_STEP)">+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">配置方式</div><div class="ksub">PLACEMENT</div></div>
            <span class="v" style="cursor:pointer" @click="setHoleContinuous(!holeContinuous)">
              {{ holeContinuous ? '連続配置 (Shift)' : 'クリックで配置' }}
            </span>
          </div>
          <div class="kv">
            <div><div class="k">タップ指示</div><div class="ksub">TAP-NOTE</div></div>
            <span class="v">なし</span>
          </div>
        </div>

        <!-- Pattern modal — opened by "A" key (App.vue) or button below. -->
        <div v-if="holePatternOpen" class="section-block">
          <h6 class="lbl">整列パターン <span class="right">行 × 列 + 間隔</span></h6>
          <div class="kv">
            <div><div class="k">行 × 列</div><div class="ksub">ROWS · COLS</div></div>
            <div class="num-step">
              <button @click="setHolePatternRows(holePatternRows - 1)">−</button>
              <input type="text" :value="holePatternRows" @change="onPatternRowsInput" />
              <span class="unit">×</span>
              <input type="text" :value="holePatternCols" @change="onPatternColsInput" />
              <button @click="setHolePatternCols(holePatternCols + 1)">+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">間隔 X / Y</div><div class="ksub">PITCH-XY</div></div>
            <div class="num-step">
              <input type="text" :value="holePatternPitchX.toFixed(1)" @change="onPatternPitchXInput" />
              <span class="unit">/</span>
              <input type="text" :value="holePatternPitchY.toFixed(1)" @change="onPatternPitchYInput" />
              <span class="unit">mm</span>
            </div>
          </div>
          <div class="action-row">
            <button class="action-btn" @click="setHolePatternOpen(false)">キャンセル</button>
            <button class="action-btn cy" :disabled="!currentFile" @click="onConfirmPattern">
              <svg><use href="#i-arrow-right" /></svg>追加
            </button>
          </div>
        </div>

        <div v-if="addedHoles.length === 0 && !holePatternOpen" class="placeholder-card">
          <div class="ic"><svg><use href="#i-hole-add" /></svg></div>
          <h5>キャンバスをクリックして配置</h5>
          <p>
            カーソル位置に <b style="color:var(--t-2)">φ{{ holeDiameter.toFixed(1) }}</b> の穴が追加されます。<br />
            連続配置: Shift+クリック、整列パターン: <b style="color:var(--t-2)">A</b>
          </p>
          <span class="meta">追加済み 0 件</span>
        </div>
        <div v-else-if="addedHoles.length > 0" class="section-block">
          <h6 class="lbl">追加済み <span class="right">クリック で 削除</span></h6>
          <div class="edge-list">
            <div
              v-for="(h, i) in addedHoles"
              :key="h.id"
              class="edge-row"
              style="cursor:default"
            >
              <span class="ix">H{{ i + 1 }}</span>
              <div>
                <div class="nm">φ{{ h.diameter.toFixed(1) }}</div>
                <div class="ns">{{ fmtCoord(h.position) }}</div>
              </div>
              <button class="row-x" title="削除" @click="removeHole(h.id)">×</button>
            </div>
          </div>
        </div>
      </template>

      <!-- ========== note (Phase 4) ========== -->
      <template v-else-if="activeTool === 'note'">
        <div class="section-block">
          <p class="lead">
            部品単位の <em>加工指示</em> (溶接記号・面粗さ・熱処理 等) を文字注記として残します。
          </p>

          <div class="kv">
            <div><div class="k">プリセット</div><div class="ksub">NOTE-PRESET</div></div>
            <span class="v" style="cursor:pointer" @click="cycleNotePreset">
              {{ NOTE_PRESET_LABEL[notePreset] }}
            </span>
          </div>
          <div class="kv">
            <div><div class="k">フォント</div><div class="ksub">FONT</div></div>
            <span class="v">isocp · {{ noteHeight.toFixed(1) }} mm</span>
          </div>
        </div>

        <div v-if="notes.length === 0" class="placeholder-card">
          <div class="ic"><svg><use href="#i-note" /></svg></div>
          <h5>注記はまだありません</h5>
          <p>
            キャンバス上で右クリック → <b style="color:var(--t-2)">「注記を追加」</b> または
            <b style="color:var(--t-2)">T</b> キーで挿入。
          </p>
          <span class="meta">注記 0 件</span>
        </div>
        <div v-else class="section-block">
          <h6 class="lbl">追加済み <span class="right">クリック で 削除</span></h6>
          <div class="edge-list">
            <div
              v-for="(n, i) in notes"
              :key="n.id"
              class="edge-row"
              style="cursor:default"
            >
              <span class="ix">N{{ i + 1 }}</span>
              <div>
                <div class="nm">{{ n.text }}</div>
                <div class="ns">{{ NOTE_PRESET_LABEL[n.preset] }} · {{ fmtCoord(n.position) }}</div>
              </div>
              <button class="row-x" title="削除" @click="removeNote(n.id)">×</button>
            </div>
          </div>
        </div>
      </template>

      <!-- ========== bridge (Phase 4) ========== -->
      <template v-else-if="activeTool === 'bridge'">
        <div class="section-block">
          <p class="lead">
            レーザ・プラズマ加工で部品が脱落しないよう、外径に <em>ブリッジ(保持タブ)</em> を残します。出力時に切断パスが分断されます。
          </p>

          <div class="kv">
            <div><div class="k">ブリッジ幅</div><div class="ksub">BRIDGE-WIDTH</div></div>
            <div class="num-step">
              <button :disabled="!currentFile" @click="bumpBridge(-BRIDGE_STEP)">−</button>
              <input
                type="text"
                :value="bridgeWidth.toFixed(1)"
                @change="onBridgeInput"
              />
              <span class="unit">mm</span>
              <button :disabled="!currentFile" @click="bumpBridge(BRIDGE_STEP)">+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">推奨個数</div><div class="ksub">AUTO-COUNT</div></div>
            <div class="num-step">
              <button :disabled="!currentFile" @click="bumpBridgeRec(-1)">−</button>
              <input type="text" :value="bridgeRecommended" readonly />
              <span class="unit">個</span>
              <button :disabled="!currentFile" @click="bumpBridgeRec(1)">+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">配置方式</div><div class="ksub">PLACEMENT</div></div>
            <span class="v">外径クリック / 自動</span>
          </div>
        </div>

        <div v-if="bridges.length === 0" class="placeholder-card">
          <div class="ic"><svg><use href="#i-bridge" /></svg></div>
          <h5>外径をクリックして配置</h5>
          <p>
            キャンバス上の外径線をクリックすると、その位置に <b style="color:var(--t-2)">{{ bridgeWidth.toFixed(1) }} mm</b> のブリッジが残ります。
          </p>
          <span class="meta">配置 0 / 推奨 {{ bridgeRecommended }}</span>
        </div>
        <div v-else class="section-block">
          <h6 class="lbl">配置済み <span class="right">クリック で 削除</span></h6>
          <div class="edge-list">
            <div
              v-for="(b, i) in bridges"
              :key="b.id"
              class="edge-row"
              style="cursor:default"
            >
              <span class="ix">B{{ i + 1 }}</span>
              <div>
                <div class="nm">{{ b.edge_id }}</div>
                <div class="ns">
                  幅 {{ b.width_mm.toFixed(1) }} mm · ratio {{ b.position_ratio.toFixed(2) }}
                  <template v-if="b.position">· {{ fmtCoord(b.position) }}</template>
                </div>
              </div>
              <button class="row-x" title="削除" @click="removeBridge(b.id)">×</button>
            </div>
          </div>
        </div>

        <div class="action-row">
          <button
            class="action-btn"
            :disabled="bridges.length === 0"
            @click="bridges.forEach((b) => removeBridge(b.id))"
          >配置をクリア</button>
          <button
            class="action-btn cy"
            :disabled="!currentFile"
            @click="addBridgeAuto"
          >
            <svg><use href="#i-arrow-right" /></svg>自動配置
          </button>
        </div>
      </template>

      <!-- ========== nest (Phase 5 —板取り最適化) ========== -->
      <template v-else-if="activeTool === 'nest'">
        <div class="section-block">
          <p class="lead">
            複数部品を <em>1枚の板</em> に最適配置します。シートサイズ・加工代・回転を指定し、結果は右側キャンバスでプレビューします。
          </p>

          <h6 class="lbl">含めるファイル <span class="right">クリックで切替</span></h6>
          <div v-if="nestSessionFiles.length === 0" class="placeholder-card">
            <div class="ic"><svg><use href="#i-nest" /></svg></div>
            <h5>DXFが未読込です</h5>
            <p>ヘッダの <b style="color:var(--t-2)">ファイル</b> から部品DXFを読み込むとここに表示されます。</p>
            <span class="meta">対象 0 件</span>
          </div>
          <div v-else class="entity-list">
            <div
              v-for="f in nestSessionFiles"
              :key="f.file_id"
              class="ent-row"
              :class="{ on: isNestFileOn(f.file_id) }"
              @click="toggleNestFile(f.file_id)"
            >
              <span class="cb"></span>
              <div>
                <div class="nm">{{ f.name }}</div>
                <div class="ns">{{ (f.size / 1024).toFixed(1) }} KB</div>
              </div>
              <span class="cnt">{{ isNestFileOn(f.file_id) ? '✓' : '—' }}</span>
            </div>
          </div>
        </div>

        <div class="section-block">
          <h6 class="lbl">シートサイズ <span class="right">mm</span></h6>
          <div class="kv">
            <div><div class="k">幅 W</div><div class="ksub">SHEET-WIDTH</div></div>
            <div class="num-step">
              <button @click="bumpNestWidth(-SHEET_STEP)">−</button>
              <input
                type="text"
                :value="nestSheetWidth"
                @change="onNestWidthInput"
              />
              <span class="unit">mm</span>
              <button @click="bumpNestWidth(SHEET_STEP)">+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">高さ H</div><div class="ksub">SHEET-HEIGHT</div></div>
            <div class="num-step">
              <button @click="bumpNestHeight(-SHEET_STEP)">−</button>
              <input
                type="text"
                :value="nestSheetHeight"
                @change="onNestHeightInput"
              />
              <span class="unit">mm</span>
              <button @click="bumpNestHeight(SHEET_STEP)">+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">加工代</div><div class="ksub">SPACING</div></div>
            <div class="num-step">
              <button @click="bumpNestSpacing(-NEST_SPACING_STEP)">−</button>
              <input
                type="text"
                :value="nestSpacingMm.toFixed(1)"
                @change="onNestSpacingInput"
              />
              <span class="unit">mm</span>
              <button @click="bumpNestSpacing(NEST_SPACING_STEP)">+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">アルゴリズム</div><div class="ksub">ALGORITHM</div></div>
            <span class="v" style="cursor:pointer" @click="cycleNestAlgorithm">
              {{ NEST_ALGORITHM_LABEL[nestAlgorithm] }}
            </span>
          </div>
          <div class="kv">
            <div><div class="k">90°回転を許可</div><div class="ksub">ROTATE</div></div>
            <span class="v" style="cursor:pointer" @click="setNestAllowRotate(!nestAllowRotate)">
              {{ nestAllowRotate ? 'ON' : 'OFF' }}
            </span>
          </div>
        </div>

        <!-- Template chips — apply preset spacing/sheet/material in one click. -->
        <div class="section-block">
          <h6 class="lbl">テンプレート <span class="right">クリックで適用</span></h6>
          <div v-if="templates.length === 0 && !isLoadingTemplates" class="placeholder-card">
            <div class="ic"><svg><use href="#i-output" /></svg></div>
            <h5>テンプレート未取得</h5>
            <p>取得には一度ネスティングモードに入る必要があります。</p>
            <span class="meta">テンプレ 0 件</span>
          </div>
          <div v-else-if="isLoadingTemplates" class="placeholder-card">
            <span class="meta">読み込み中…</span>
          </div>
          <div v-else class="corner-list">
            <div
              v-for="t in templates"
              :key="t.template_id"
              class="corner-chip"
              :title="t.description ?? t.name"
              @click="applyTemplate(t.template_id)"
            >
              <span class="dot"></span>
              {{ t.name }}
            </div>
          </div>
        </div>

        <!-- Job progress strip — shown only while a job is in-flight. -->
        <div
          v-if="nestingJob && (nestingJob.status === 'pending' || nestingJob.status === 'running')"
          class="warn-strip"
          style="background:var(--cy-soft);color:var(--cy);border:1px solid rgba(77,207,224,0.25)"
        >
          <svg style="fill:var(--cy)"><use href="#i-warning" /></svg>
          <div>
            <b style="color:var(--cy)">
              {{ nestingJob.status === 'pending' ? 'キューに登録中' : '計算中…' }}
              ({{ Math.round((nestingJob.progress ?? 0) * 100) }}%)
            </b><br />
            {{ nestingJob.message ?? '部品をシートに配置しています。' }}
          </div>
        </div>

        <!-- Result summary — only after a successful run. -->
        <div v-if="nestingResult" class="summary">
          <h6>ネスティング結果</h6>
          <div class="summary-row">
            <span class="k">シート数</span>
            <span class="v big">{{ nestingResult.sheets.length }}<span class="u">枚</span></span>
          </div>
          <div class="summary-row">
            <span class="k">歩留まり</span>
            <span class="v">{{ utilPct(nestingResult.utilization) }}</span>
          </div>
          <div class="summary-row">
            <span class="k">未配置</span>
            <span class="v">{{ nestingResult.unplaced }} 件</span>
          </div>
        </div>

        <div class="action-row">
          <button
            class="action-btn"
            :disabled="!nestingResult || nestingResult.sheets.length === 0"
            @click="nestingResult && nestingResult.sheets.forEach((s) => exportNestSheet(s.sheet_index))"
          >
            DXFとして書き出し
          </button>
          <button
            class="action-btn cy"
            :disabled="!currentSession || isRunningNest || (nestingJob?.status === 'running')"
            @click="runNesting"
          >
            <svg><use href="#i-arrow-right" /></svg>
            {{ isRunningNest || nestingJob?.status === 'running' ? '実行中…' : 'ネスティング実行' }}
          </button>
        </div>
      </template>
    </div>
  </aside>
</template>

<style scoped>
/* 矩形範囲削除 — v3 トークン (--cy / --am / --line-2 / --r-md / --t-2 / --t-3)
   準拠で、既存の .section-block / .lead と違和感なく並ぶように。
   .toggle-btn は色別チェックの .ent-row と同じ高さ感 (28px) で揃え、ON で
   cyan 縁取り + 軽いハイライト。ラジオは v3 既定のフォーム要素のため
   accent-color のみ統一する。 */
.rect-block { margin-top: 10px; }
.rect-tools {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.toggle-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 28px;
  padding: 0 12px;
  border: 1px solid var(--line-2);
  border-radius: var(--r-md);
  background: transparent;
  color: var(--t-3);
  font-family: var(--f-mono);
  font-size: 10.5px;
  letter-spacing: 0.04em;
  cursor: pointer;
  transition: background .15s, color .15s, border-color .15s;
}
.toggle-btn:hover {
  color: var(--t-2);
  border-color: var(--line-3);
}
.toggle-btn.on {
  color: var(--cy);
  border-color: var(--cy);
  background: rgba(77, 207, 224, 0.08);
}
.rect-mode-switch {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 8px;
  border: 1px solid var(--line-2);
  border-radius: var(--r-md);
  background: var(--bg-2);
}
.rect-mode-switch label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--t-2);
  font-size: 11.5px;
  cursor: pointer;
}
.rect-mode-switch input[type="radio"],
.rect-mode-switch input[type="checkbox"] {
  accent-color: var(--cy);
  margin: 0;
}
.rect-mode-switch label.rect-protect {
  margin-top: 2px;
  padding-top: 6px;
  border-top: 1px dashed var(--line-2);
  color: var(--t-3);
  font-family: var(--f-mono);
  font-size: 10.5px;
  letter-spacing: 0.03em;
}

/* Phase 4 — Per-row delete affordance (× button on dim/hole/note/bridge
   added-item rows). Sized to slot into the .edge-row's ``auto`` trailing
   column without disturbing the v3 row height. */
.row-x {
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 1px solid var(--line-2);
  border-radius: var(--r-sm);
  color: var(--t-3);
  font-family: var(--f-mono);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  padding: 0;
  transition: background .15s, color .15s, border-color .15s;
}
.row-x:hover {
  color: var(--am);
  border-color: rgba(245, 166, 35, 0.4);
  background: var(--am-soft);
}
</style>
