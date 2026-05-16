/**
 * Session store (Composition API, no Pinia needed).
 *
 * Owns the upload → parse → render → delete → export flow:
 *  - `currentSession` ........ the response from POST /api/upload
 *  - `currentFileId` ......... the file_id of the active tab
 *  - `files` ................. parsed entity caches keyed by file_id
 *  - `selectedForDelete` ..... entity ids currently flagged for deletion
 *  - `uploadFiles()` ......... ingest user-selected DXFs and auto-select #0
 *  - `selectFile()` .......... switch active tab (loads on demand)
 *  - `loadFile()` ............ force a re-fetch (used after delete)
 *  - `selectEntity()` ........ toggle a single entity in/out of the delete set
 *  - `toggleCategory()` ...... bulk add/remove a delete-candidate category
 *  - `executeDelete()` ....... POST the delete set, refresh the file cache
 *  - `exportDxf()` ........... fetch the cleaned DXF and trigger download
 *
 * Errors surface via `lastError` so the canvas warning banner can show them.
 */

import { computed, ref, shallowRef } from 'vue';
import * as api from '../services/api';
import type {
  AddedHole,
  BoundingBox,
  Bridge,
  ChamferGeometry,
  ChamferSpec,
  CornerInfo,
  CornerJoin,
  DeleteCategoryKey,
  DeleteCategoryRow,
  Dimension,
  DimensionType,
  DxfExportOptions,
  EdgeInfo,
  EditedVertex,
  Entity,
  FileData,
  HolePatternRequest,
  Job,
  NestAlgorithm,
  NestRequest,
  NestResult,
  Note,
  NotePreset,
  OffsetRequest,
  OffsetResult,
  OuterDetectionResult,
  PdfExportOptions,
  PdfFrameOption,
  RenderedSvg,
  SavedSession,
  Session,
  SnapResult,
  Template,
} from '../types/dxf';
import { bboxIntersects, entityBbox } from '../utils/entityBbox';
import { useActiveTool } from './activeTool';

/** Tiny id helper — keeps the client-generated UUID-ish strings consistent
 *  with what mockSession.ts emits (so a switch live↔mock doesn't churn ids). */
function makeId(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 10)}`;
}

const _currentSession = ref<Session | null>(null);
const _currentFileId = ref<string | null>(null);
/** Use shallowRef so swapping a whole Map doesn't deep-track every entity. */
const _files = shallowRef<Map<string, FileData>>(new Map());
const _selectedForDelete = ref<Set<string>>(new Set());
/** Rectangle-select sub-mode for the delete tool. When ``true``, mouse drags
 *  on the canvas draw a selection rectangle and add the enclosed entities to
 *  ``selectedForDelete`` (instead of treating the drag as a click). */
const _rectSelectMode = ref<boolean>(false);
/** Invert flag for rect-select: ``false`` selects entities inside the rect;
 *  ``true`` selects everything OUTSIDE (part-stays, annotation-purges). */
const _rectSelectInvert = ref<boolean>(false);
/** Safety net for invert-mode: when ``true`` (default), entities classified
 *  as ``outer`` AND the operator's confirmed manual chain are preserved even
 *  if they lie outside the rect — so a "select everything outside" sweep
 *  cannot accidentally nuke the part's outline. */
const _protectOuterFromRect = ref<boolean>(true);
const _isUploading = ref(false);
const _isLoadingFile = ref(false);
const _isDeleting = ref(false);
const _lastError = ref<string | null>(null);
const _isLiveBackend = ref<boolean | null>(null);

/* ------------------- Phase 2 — outer/offset shared state ------------------ */
/** Per-file outer-detection results, keyed by file_id. */
const _outerByFile = shallowRef<Map<string, OuterDetectionResult>>(new Map());
/** Per-file offset results, keyed by file_id. */
const _offsetByFile = shallowRef<Map<string, OffsetResult>>(new Map());
/** Per-file manual-selection state (entity ids in click order). */
const _manualByFile = shallowRef<Map<string, string[]>>(new Map());
const _isDetectingOuter = ref(false);
const _isComputingOffset = ref(false);
const _manualMode = ref(false);
const _defaultOffsetMm = ref<number>(3.0);
const _edgeOverrides = ref<Record<string, number>>({});
const _cornerJoin = ref<CornerJoin>('arc');

/* ------------------- Phase 3 — chamfer / PDF / cleanup-frame --------------- */
/** Per-file corner list (from /corners). */
const _cornersByFile = shallowRef<Map<string, CornerInfo[]>>(new Map());
/** Per-file edge list (from /corners — used by the 開先/bevel UI). */
const _edgesByFile = shallowRef<Map<string, EdgeInfo[]>>(new Map());
/** Per-file chamfer specs keyed by file_id; UI-source of truth (post-server
 *  mutations apply server-returned specs back to this map). */
const _chamferByFile = shallowRef<Map<string, ChamferSpec[]>>(new Map());
/** Per-file chamfer geometry (annotations for canvas markers). */
const _chamferGeometryByFile = shallowRef<Map<string, ChamferGeometry>>(new Map());
/** Last cleanup-frame result for the active file (for the Inspector summary). */
const _lastFrameCleanup = ref<{ removed_count: number; ids: string[] } | null>(null);
/** PDF export options (single config shared across files — matches how the
 *  header dropdown is presented to the user). Defaults are OFF so a
 *  user-clicked "ダウンロード" never trips the backend's 422 guards (which
 *  require pre-computed offset / confirmed outer before they can be true).
 *  The user opts in explicitly by ticking the boxes. (C2) */
const _pdfExportOptions = ref<PdfExportOptions>({
  frame: 'none',
  with_offset: false,
  with_chamfer: false,
  // C3: Phase 4 overlay defaults are ON so a user that just placed dims/
  // holes/notes/bridges sees them in the exported file without remembering
  // to re-tick checkboxes. The Header dropdown auto-disables the checkbox
  // when the corresponding list is empty so this is harmless when there
  // is nothing to bake in.
  with_dimensions: true,
  with_added_holes: true,
  with_notes: true,
  with_bridges: true,
  with_edits: true,
});
/** Same shape for DXF (no frame / material — those are PDF-only). */
const _dxfExportOptions = ref<DxfExportOptions>({
  with_offset: false,
  with_chamfer: false,
  with_dimensions: true,
  with_added_holes: true,
  with_notes: true,
  with_bridges: true,
  with_edits: true,
});
/** Default chamfer values surfaced in the inspector num-step. C面 size is in
 *  mm (Cn); the bevel angle is in degrees and only applies to ``type='bevel'``
 *  specs (H6 — C面 is fixed at 45° by convention so we never plumb angle into
 *  the C-面 spec from the UI). */
const _chamferDefaultSize = ref<number>(2);    // C2
const _chamferDefaultAngle = ref<number>(30);  // 30° (bevel only)
/** Optional material text shown on the PDF header (H4). */
const _pdfMaterial = ref<string>('');
const _isLoadingCorners = ref(false);
const _isApplyingChamfer = ref(false);
const _isExportingPdf = ref(false);
const _isCleaningFrame = ref(false);
/**
 * File-picker openers registered by Header.vue on mount (M4). TabBar and any
 * other component that needs to trigger the upload UI calls these instead of
 * reaching into the DOM with `document.querySelector`.
 */
const _filePickerOpener = ref<(() => void) | null>(null);
const _folderPickerOpener = ref<(() => void) | null>(null);

/* ------------------- Phase 4 — dim / edit / hole / note / bridge ----------- */
/** Per-file Phase 4 annotation lists, keyed by file_id. */
const _dimensionsByFile = shallowRef<Map<string, Dimension[]>>(new Map());
const _vertexEditsByFile = shallowRef<Map<string, EditedVertex[]>>(new Map());
const _addedHolesByFile = shallowRef<Map<string, AddedHole[]>>(new Map());
const _notesByFile = shallowRef<Map<string, Note[]>>(new Map());
const _bridgesByFile = shallowRef<Map<string, Bridge[]>>(new Map());

/** Dimension tool — UI settings persisted across mode switches.
 *  ``dimType`` selects which ezdxf renderer the backend invokes when the
 *  dim is baked into the export — H5 (frontend was previously sending no
 *  type at all so backend always defaulted to 'linear'). */
const _dimType = ref<DimensionType>('linear');
const _dimPrecision = ref<number>(1);
const _dimArrowSize = ref<number>(3.5);
/** In-progress dimension placement: first click stored until 2nd lands. */
const _pendingDimStart = ref<[number, number] | null>(null);
const _dimTwoPointMode = ref<boolean>(false);

/** Edit tool — UI toggles. */
const _editSnapEnabled = ref<boolean>(true);
const _editGridSnap = ref<number>(1);    // mm
const _editOrtho = ref<boolean>(false);  // toggled by Shift
const _editSelection = ref<{ entity_id: string; vertex_index: number } | null>(null);

/** Hole tool — diameter + placement / pattern config. */
const _holeDiameter = ref<number>(9);
const _holeContinuous = ref<boolean>(false);   // Shift-click sticks the mode
const _holePatternOpen = ref<boolean>(false);  // "A" key opens the modal
const _holePatternRows = ref<number>(2);
const _holePatternCols = ref<number>(3);
const _holePatternPitchX = ref<number>(40);
const _holePatternPitchY = ref<number>(40);

/** Note tool — preset + font + pending text modal anchor.
 *  Backend enum: 'roughness' | 'welding' | 'general' (was 'weld' in the
 *  legacy mock — C1 renames the wire value). */
const _notePreset = ref<NotePreset>('general');
const _noteHeight = ref<number>(2.5);
const _notePendingAnchor = ref<[number, number] | null>(null);

/** Bridge tool — width + computed recommended count. */
const _bridgeWidth = ref<number>(2.0);
const _bridgeRecommended = ref<number>(4);

/** Last snap result (for canvas overlay rendering). */
const _lastSnap = ref<SnapResult | null>(null);
const _isAddingDimension = ref(false);
const _isAddingHole = ref(false);
const _isAddingNote = ref(false);
const _isAddingBridge = ref(false);

/* ------------------- Phase 5 — nesting / history / templates --------------- */
/** Active nesting job (null when no job has been kicked off yet). */
const _nestingJob = ref<Job | null>(null);
/** Nesting result of the most recently completed job. */
const _nestingResult = ref<NestResult | null>(null);
/** Saved-session catalogue (lazily loaded on the first /history open). */
const _savedSessions = ref<SavedSession[]>([]);
/** Available templates (lazily loaded on the first templates panel open). */
const _templates = ref<Template[]>([]);
/** Inspector form state for the nest panel. */
const _nestSheetWidth = ref<number>(1500);
const _nestSheetHeight = ref<number>(3000);
const _nestSheetQuantity = ref<number>(1);
const _nestSpacingMm = ref<number>(3);
// C1: BE 既定アルゴリズム名 (Phase 5 は ``bottom_left`` のみ)
const _nestAlgorithm = ref<NestAlgorithm>('bottom_left');
const _nestAllowRotate = ref<boolean>(true);
/** File ids the operator has ticked for the next run. Defaults to "all" via
 *  the derived ``nestSelectedFileIds`` getter (empty selection = include
 *  every loaded part). */
const _nestSelectedFileIds = ref<Set<string>>(new Set());
const _isRunningNest = ref<boolean>(false);
const _isSavingSession = ref<boolean>(false);
const _isLoadingTemplates = ref<boolean>(false);

/* ------------------- Phase 6 — server-rendered SVG (背景レイヤー) --------- */
/** Per-file ezdxf-rendered SVG cache, keyed by file_id. The background
 *  canvas layer reads from this map; entries are populated lazily by
 *  ``loadRenderedSvg`` and cleared after delete/edit so the next paint
 *  re-fetches against the updated geometry. */
const _renderedSvgByFile = shallowRef<Map<string, RenderedSvg>>(new Map());
const _isLoadingRenderedSvg = ref<boolean>(false);
/** UI render mode: 'real' overlays the ezdxf SVG behind the operation layer;
 *  'simple' shows only the Phase 1-5 primitives (legacy / fallback view).
 *  Persisted to localStorage so a reload preserves the operator's choice. */
type RenderMode = 'real' | 'simple';
const _RENDER_MODE_KEY = 'cutflow.renderMode';
function _readInitialRenderMode(): RenderMode {
  try {
    const v = typeof window !== 'undefined'
      ? window.localStorage?.getItem(_RENDER_MODE_KEY)
      : null;
    return v === 'simple' ? 'simple' : 'real';
  } catch {
    return 'real';
  }
}
const _renderMode = ref<RenderMode>(_readInitialRenderMode());

/** Assembly-drawing filename pattern. Mirror of the server-side regex in
 *  api/routers/session.py — kept in sync manually since duplicating across
 *  the boundary is cheaper than introducing a build step for one pattern. */
const ASSEMBLY_RE = /(組立図|assembly|-0T_)/i;

/** Display labels for the 4 delete categories surfaced in the inspector. */
const CATEGORY_META: Record<DeleteCategoryKey, { label: string; sub: string }> = {
  dim:     { label: '寸法線',         sub: 'DIMENSION' },
  balloon: { label: 'バルーン',       sub: 'BALLOON' },
  tap:     { label: 'タップ穴マーク', sub: 'TAP-MARK' },
  frame:   { label: '図枠 / 表題欄',  sub: 'PRODUCTION-FRAME' },
};

/** Map a DeleteCategoryKey to the bucket keys that may carry its ids. */
const CATEGORY_BUCKETS: Record<DeleteCategoryKey, string[]> = {
  dim:     ['DIMENSION'],
  balloon: ['BALLOON'],
  tap:     ['TAP'],
  frame:   ['FRAME'],
};

/* ----------------------------- Derived state ----------------------------- */

const currentFile = computed<FileData | null>(() => {
  const fid = _currentFileId.value;
  if (!fid) return null;
  return _files.value.get(fid) ?? null;
});

/**
 * Entities visible in the canvas — same as `currentFile.entities` but with
 * server-side `deleted_ids` filtered out. The backend keeps the raw entity
 * in the payload (so Phase 2 undo can resurrect them); the renderer is the
 * one responsible for not drawing them.
 */
const visibleEntities = computed<Entity[]>(() => {
  const file = currentFile.value;
  if (!file) return [];
  const deleted = file.deleted_ids;
  if (!deleted || deleted.length === 0) return file.entities;
  const skip = new Set(deleted);
  return file.entities.filter((e) => !skip.has(e.id));
});

const deleteRows = computed<DeleteCategoryRow[]>(() => {
  const file = currentFile.value;
  if (!file) return [];
  return (Object.keys(CATEGORY_META) as DeleteCategoryKey[]).map((key) => {
    const buckets = CATEGORY_BUCKETS[key];
    const idSet = new Set<string>();
    // Prefer pre-classified candidates from the backend.
    for (const b of buckets) {
      const ids = file.delete_candidates[b];
      if (ids) ids.forEach((id) => idSet.add(id));
    }
    // Fallback: scan entity categories (covers tap/frame even when the
    // backend hasn't populated delete_candidates buckets for them).
    for (const e of file.entities) {
      if (e.category === key) idSet.add(e.id);
    }
    return {
      key,
      label: CATEGORY_META[key].label,
      sub: CATEGORY_META[key].sub,
      ids: [...idSet],
    };
  });
});

const totalDeleteCandidates = computed(() =>
  deleteRows.value.reduce((n, r) => n + r.ids.length, 0),
);

const remainingAfterDelete = computed(() => {
  const file = currentFile.value;
  if (!file) return 0;
  // Visible (= not server-deleted) entities minus the current pending
  // selection — this matches what the user actually sees in the canvas.
  return visibleEntities.value.length - _selectedForDelete.value.size;
});

/** Outer-detection result for the active file (null until detected). */
const outerDetection = computed<OuterDetectionResult | null>(() => {
  const fid = _currentFileId.value;
  if (!fid) return null;
  return _outerByFile.value.get(fid) ?? null;
});

/** Offset-preview result for the active file (null until computed). */
const offsetResult = computed<OffsetResult | null>(() => {
  const fid = _currentFileId.value;
  if (!fid) return null;
  return _offsetByFile.value.get(fid) ?? null;
});

/** Ordered list of entity ids the user has chained for manual selection. */
const manualSelection = computed<string[]>(() => {
  const fid = _currentFileId.value;
  if (!fid) return [];
  return _manualByFile.value.get(fid) ?? [];
});

/** Set of outer entity ids (manual chain takes precedence over detection). */
const outerEntityIdSet = computed<Set<string>>(() => {
  if (_manualMode.value && manualSelection.value.length > 0) {
    return new Set(manualSelection.value);
  }
  return new Set(outerDetection.value?.outer_loop ?? []);
});

/** Corners for the active file (empty until /corners is loaded). */
const corners = computed<CornerInfo[]>(() => {
  const fid = _currentFileId.value;
  if (!fid) return [];
  return _cornersByFile.value.get(fid) ?? [];
});

/** Edges for the active file (empty until /corners is loaded). Used by the
 *  開先 (bevel) UI — operator picks an edge to mark a chamfer note for. */
const edges = computed<EdgeInfo[]>(() => {
  const fid = _currentFileId.value;
  if (!fid) return [];
  return _edgesByFile.value.get(fid) ?? [];
});

/** Chamfer specs for the active file. */
const chamferSpecs = computed<ChamferSpec[]>(() => {
  const fid = _currentFileId.value;
  if (!fid) return [];
  return _chamferByFile.value.get(fid) ?? [];
});

/** Chamfer geometry annotations for the active file. */
const chamferGeometry = computed<ChamferGeometry | null>(() => {
  const fid = _currentFileId.value;
  if (!fid) return null;
  return _chamferGeometryByFile.value.get(fid) ?? null;
});

/** Map corner_id → spec for O(1) lookup in UI/canvas. */
const chamferSpecByCorner = computed<Map<string, ChamferSpec>>(() => {
  const map = new Map<string, ChamferSpec>();
  for (const s of chamferSpecs.value) map.set(s.corner_id, s);
  return map;
});

/* --------------------- Phase 4 derived (per-file lists) ------------------- */

const dimensions = computed<Dimension[]>(() => {
  const fid = _currentFileId.value;
  if (!fid) return [];
  return _dimensionsByFile.value.get(fid) ?? [];
});

const vertexEdits = computed<EditedVertex[]>(() => {
  const fid = _currentFileId.value;
  if (!fid) return [];
  return _vertexEditsByFile.value.get(fid) ?? [];
});

const addedHoles = computed<AddedHole[]>(() => {
  const fid = _currentFileId.value;
  if (!fid) return [];
  return _addedHolesByFile.value.get(fid) ?? [];
});

const notes = computed<Note[]>(() => {
  const fid = _currentFileId.value;
  if (!fid) return [];
  return _notesByFile.value.get(fid) ?? [];
});

const bridges = computed<Bridge[]>(() => {
  const fid = _currentFileId.value;
  if (!fid) return [];
  return _bridgesByFile.value.get(fid) ?? [];
});

/** Phase 6 — server-rendered SVG for the active file (null until fetched). */
const renderedSvg = computed<RenderedSvg | null>(() => {
  const fid = _currentFileId.value;
  if (!fid) return null;
  return _renderedSvgByFile.value.get(fid) ?? null;
});

/* ----------------------------- Helpers ---------------------------------- */

function setError(msg: string | null) {
  _lastError.value = msg;
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke after the click is dispatched.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ----------------------------- Public API ------------------------------- */

export function useSession() {
  const { setTool } = useActiveTool();

  /** Ingest user-selected DXF files and auto-select the first one.
   *  Client-side filters mirror the server (DXF extension + assembly drop).
   *  We warn the user if assembly drawings were skipped so the count
   *  mismatch isn't a surprise. */
  async function uploadFiles(files: File[]): Promise<void> {
    if (files.length === 0) return;
    setError(null);

    const dxfs = files.filter((f) => /\.dxf$/i.test(f.name));
    const assemblies = dxfs.filter((f) => ASSEMBLY_RE.test(f.name));
    const parts = dxfs.filter((f) => !ASSEMBLY_RE.test(f.name));

    if (parts.length === 0) {
      const msg = assemblies.length > 0
        ? `組立図のみが選択されました (${assemblies.length}件) — 部品図DXFを選択してください`
        : 'DXFファイルが選択されていません';
      setError(msg);
      return;
    }

    _isUploading.value = true;
    try {
      _isLiveBackend.value = await api.isLiveBackend();
      const session = await api.uploadFiles(parts);
      _currentSession.value = session;
      _files.value = new Map();
      _selectedForDelete.value = new Set();
      if (assemblies.length > 0) {
        setError(`組立図は除外しました: ${assemblies.length}件`);
      }
      if (session.files.length > 0) {
        await selectFile(session.files[0].file_id);
        // Per Phase 1 task brief: jump straight to delete mode on first load.
        setTool('delete');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'アップロードに失敗しました');
      throw err;
    } finally {
      _isUploading.value = false;
    }
  }

  /** Header.vue registers the two hidden inputs at mount; other components
   *  (TabBar) call `openFilePicker()` / `openFolderPicker()` instead of
   *  poking the DOM directly. */
  function registerFilePicker(fn: () => void): void {
    _filePickerOpener.value = fn;
  }
  function registerFolderPicker(fn: () => void): void {
    _folderPickerOpener.value = fn;
  }
  function openFilePicker(): void {
    _filePickerOpener.value?.();
  }
  function openFolderPicker(): void {
    _folderPickerOpener.value?.();
  }

  /** Switch the active tab — fetches the file on first visit.
   *  Phase 2: manual-selection mode is per-session UX (not per-file), so the
   *  toggle is reset on every tab switch to avoid surprising clicks on a
   *  fresh file. Per-file outer/offset/manual maps survive the switch. */
  async function selectFile(fid: string): Promise<void> {
    _currentFileId.value = fid;
    _selectedForDelete.value = new Set();
    _rectSelectMode.value = false;
    _rectSelectInvert.value = false;
    _manualMode.value = false;
    // Phase 3: the cleanup-frame banner is per-action, not per-file —
    // clear it so the green strip from file A doesn't leak into file B.
    _lastFrameCleanup.value = null;
    if (!_files.value.has(fid)) await loadFile(fid);
  }

  /** Force-fetch (or re-fetch) the parsed entity payload for a file.
   *
   *  Also rehydrates the persisted outer / offset payloads in parallel so
   *  a tab visited after a refresh shows the previously confirmed state
   *  without forcing the user to re-detect (M3). */
  async function loadFile(fid: string): Promise<void> {
    const sid = _currentSession.value?.session_id;
    if (!sid) return;
    setError(null);
    _isLoadingFile.value = true;
    try {
      const [data, outer, offset, chamfer, dims, holes, notes_, bridges_] = await Promise.all([
        api.getFile(sid, fid),
        api.getOuter(sid, fid).catch(() => null),
        api.getOffset(sid, fid).catch(() => null),
        api.getChamfer(sid, fid).catch(() => null),
        // Phase 4 — hydrate annotation lists so a tab switch shows existing
        // dim/hole/note/bridge work without a separate /annotations roundtrip.
        api.listDimensions(sid, fid).catch(() => [] as Dimension[]),
        api.listHoles(sid, fid).catch(() => [] as AddedHole[]),
        api.listNotes(sid, fid).catch(() => [] as Note[]),
        api.listBridges(sid, fid).catch(() => [] as Bridge[]),
      ]);
      // H3 defensive: if the freshly-fetched payload reports a different
      // deleted_ids set than the one we had cached, the background SVG
      // is stale (it was rendered against the old deletion set). Drop the
      // cached entry so the next ``loadRenderedSvg`` re-fetches.
      const prev = _files.value.get(fid);
      const prevDeleted = (prev?.deleted_ids ?? []).slice().sort().join(',');
      const nextDeleted = (data.deleted_ids ?? []).slice().sort().join(',');
      if (prev && prevDeleted !== nextDeleted) {
        setMapEntry(_renderedSvgByFile, fid, undefined);
      }
      // shallowRef requires assigning a new Map ref to trigger updates.
      const next = new Map(_files.value);
      next.set(fid, data);
      _files.value = next;
      if (outer) setMapEntry(_outerByFile, fid, outer);
      if (offset) setMapEntry(_offsetByFile, fid, offset);
      if (chamfer) {
        setMapEntry(_chamferByFile, fid, chamfer.specs);
        setMapEntry(_chamferGeometryByFile, fid, chamfer.geometry);
      }
      setMapEntry(_dimensionsByFile, fid, dims);
      setMapEntry(_addedHolesByFile, fid, holes);
      setMapEntry(_notesByFile, fid, notes_);
      setMapEntry(_bridgesByFile, fid, bridges_);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'ファイルの読み込みに失敗しました');
    } finally {
      _isLoadingFile.value = false;
    }
  }

  /** Toggle a single entity id in/out of the delete selection. */
  function selectEntity(eid: string): void {
    const next = new Set(_selectedForDelete.value);
    if (next.has(eid)) next.delete(eid);
    else next.add(eid);
    _selectedForDelete.value = next;
  }

  /** Bulk add/remove every id in a delete category. */
  function toggleCategory(key: DeleteCategoryKey): void {
    const row = deleteRows.value.find((r) => r.key === key);
    if (!row || row.ids.length === 0) return;
    const next = new Set(_selectedForDelete.value);
    const allOn = row.ids.every((id) => next.has(id));
    if (allOn) row.ids.forEach((id) => next.delete(id));
    else row.ids.forEach((id) => next.add(id));
    _selectedForDelete.value = next;
  }

  /** Returns true when every id in the category is currently selected. */
  function isCategoryOn(key: DeleteCategoryKey): boolean {
    const row = deleteRows.value.find((r) => r.key === key);
    if (!row || row.ids.length === 0) return false;
    return row.ids.every((id) => _selectedForDelete.value.has(id));
  }

  /** Clear every entity in the current delete selection. */
  function clearSelection(): void {
    if (_selectedForDelete.value.size === 0) return;
    _selectedForDelete.value = new Set();
  }

  /** Toggle the rectangle-select sub-mode (delete tool only). Turning it off
   *  also clears the invert flag — operator expectation is "off = fresh
   *  default" so the next time they flip it on they always start in the
   *  safer inside-mode. */
  function setRectSelectMode(on: boolean): void {
    _rectSelectMode.value = on;
    if (!on) _rectSelectInvert.value = false;
  }
  function setRectInvert(on: boolean): void {
    _rectSelectInvert.value = on;
  }
  function setProtectOuterFromRect(on: boolean): void {
    _protectOuterFromRect.value = on;
  }

  /** Add every entity whose bbox satisfies the rect predicate to
   *  ``selectedForDelete``. Inside-mode requires the bbox to intersect (a
   *  generous rule so brushing the rect over a TEXT anchor at the title block
   *  catches it); outside-mode requires the bbox to be fully outside the rect
   *  (a stricter rule so an entity straddling the boundary is preserved by
   *  default — the operator can always extend the rect).
   *
   *  The outer-protection guard (default ON) keeps the confirmed outer loop
   *  AND any manual chain alive in invert-mode so a "select everything
   *  outside the part" sweep cannot wipe the outline. */
  function selectByRect(
    fid: string,
    rect: BoundingBox,
    invert: boolean,
  ): void {
    const file = _files.value.get(fid);
    if (!file) return;
    // Normalize the rect — operators drag in either direction.
    const r: BoundingBox = {
      min_x: Math.min(rect.min_x, rect.max_x),
      max_x: Math.max(rect.min_x, rect.max_x),
      min_y: Math.min(rect.min_y, rect.max_y),
      max_y: Math.max(rect.min_y, rect.max_y),
    };
    // Skip degenerate rects (single-pixel drag = click) so an inside-sweep
    // with zero area can't accidentally select a stack of overlapping
    // entities that all happen to touch the same point.
    if (r.max_x - r.min_x <= 0 || r.max_y - r.min_y <= 0) return;

    // Outer-protection set: confirmed outer loop ids + any manual chain.
    const protectedIds = new Set<string>();
    if (invert && _protectOuterFromRect.value) {
      const outer = _outerByFile.value.get(fid)?.outer_loop ?? [];
      outer.forEach((id) => protectedIds.add(id));
      const manual = _manualByFile.value.get(fid) ?? [];
      manual.forEach((id) => protectedIds.add(id));
      // Also keep entities the backend classifies as ``outer`` (covers the
      // case where /detect-outer hasn't been run yet).
      for (const e of file.entities) {
        if (e.category === 'outer') protectedIds.add(e.id);
      }
    }

    const deleted = new Set(file.deleted_ids ?? []);
    const next = new Set(_selectedForDelete.value);
    for (const e of file.entities) {
      if (deleted.has(e.id)) continue;            // already server-deleted
      if (protectedIds.has(e.id)) continue;       // outer guard (invert only)
      const bb = entityBbox(e);
      if (!bb) {
        // Unknown geometry: in invert-mode we conservatively skip (we can't
        // prove the entity is outside); in inside-mode we also skip (can't
        // prove it's inside either).
        continue;
      }
      if (invert) {
        // Select when the bbox does NOT touch the rect at all — fully outside.
        if (!bboxIntersects(bb, r)) next.add(e.id);
      } else {
        // Select when the bbox intersects the rect (touching = inside).
        // bboxInside is the strict alternative; intersects keeps the brush
        // forgiving for tiny TEXT anchors hugging the rect edge.
        if (bboxIntersects(bb, r)) next.add(e.id);
      }
    }
    _selectedForDelete.value = next;
  }

  /** POST the current selection, refresh the file cache, drop the selection. */
  async function executeDelete(): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    const ids = [..._selectedForDelete.value];
    if (ids.length === 0) return;
    setError(null);
    _isDeleting.value = true;
    try {
      await api.deleteEntities(sid, fid, ids);
      // H11: any cached offset is stale once the geometry changes.
      setMapEntry(_offsetByFile, fid, undefined);
      // Phase 6: background SVG is rendered with apply_deletions=true so it
      // is stale once the server removes more ids — drop the cache so the
      // canvas refetch picks up the new render.
      setMapEntry(_renderedSvgByFile, fid, undefined);
      await loadFile(fid);
      _selectedForDelete.value = new Set();
    } catch (err) {
      setError(err instanceof Error ? err.message : '削除に失敗しました');
    } finally {
      _isDeleting.value = false;
    }
  }

  /** Download the cleaned DXF for the active file (no offset).
   *  C3: Phase 4 overlay flags ride on the request when the user has
   *  ticked the corresponding boxes in the dropdown. */
  async function exportDxf(): Promise<void> {
    await exportDxfInternal({ ..._dxfExportOptions.value, with_offset: false });
  }

  /** Download the cleaned DXF with the computed offset embedded. */
  async function exportDxfWithOffset(): Promise<void> {
    await exportDxfInternal({ ..._dxfExportOptions.value, with_offset: true });
  }

  async function exportDxfInternal(opts: DxfExportOptions): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    const file = currentFile.value;
    if (!sid || !fid || !file) {
      setError('開いているDXFがありません');
      return;
    }
    setError(null);
    try {
      const blob = await api.exportDxf(sid, fid, opts);
      const base = file.name.replace(/\.[Dd][Xx][Ff]$/, '');
      const suffix = opts.with_offset ? '_offset' : '_clean';
      downloadBlob(blob, `${base}${suffix}.dxf`);
    } catch (err) {
      setError(err instanceof Error ? err.message : '書き出しに失敗しました');
    }
  }
  function setDxfWithOffset(on: boolean): void {
    _dxfExportOptions.value = { ..._dxfExportOptions.value, with_offset: on };
  }
  function setDxfWithDimensions(on: boolean): void {
    _dxfExportOptions.value = { ..._dxfExportOptions.value, with_dimensions: on };
  }
  function setDxfWithAddedHoles(on: boolean): void {
    _dxfExportOptions.value = { ..._dxfExportOptions.value, with_added_holes: on };
  }
  function setDxfWithNotes(on: boolean): void {
    _dxfExportOptions.value = { ..._dxfExportOptions.value, with_notes: on };
  }
  function setDxfWithBridges(on: boolean): void {
    _dxfExportOptions.value = { ..._dxfExportOptions.value, with_bridges: on };
  }
  function setDxfWithEdits(on: boolean): void {
    _dxfExportOptions.value = { ..._dxfExportOptions.value, with_edits: on };
  }

  /* -------------------- Phase 2 — outer / offset actions ----------------- */

  /** Mutate a shallowRef Map by cloning so subscribers re-render. */
  function setMapEntry<V>(
    map: { value: Map<string, V> },
    key: string,
    value: V | undefined,
  ): void {
    const next = new Map(map.value);
    if (value === undefined) next.delete(key);
    else next.set(key, value);
    map.value = next;
  }

  /** Run the automatic outer-detection for the active file. */
  async function detectOuter(): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) {
      setError('開いているDXFがありません');
      return;
    }
    setError(null);
    _isDetectingOuter.value = true;
    try {
      const res = await api.detectOuter(sid, fid);
      setMapEntry(_outerByFile, fid, res);
      // H11: outer changed → offset cache is stale.
      setMapEntry(_offsetByFile, fid, undefined);
      // Successful auto detection drops any in-progress manual chain.
      if (res.status === 'success') {
        setMapEntry(_manualByFile, fid, []);
        _manualMode.value = false;
      }
      if (res.status === 'failed' || res.status === 'low_confidence') {
        setError(
          res.warnings[0] ??
            '外径の自動検出に失敗しました。線を手動で指定してください。',
        );
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '外径検出に失敗しました');
    } finally {
      _isDetectingOuter.value = false;
    }
  }

  /** Toggle manual-selection mode (entity clicks chain ids while ON). */
  function setManualMode(on: boolean): void {
    _manualMode.value = on;
  }

  /** Append (or remove, when already last) an entity id to the manual chain. */
  function addToManual(entityId: string): void {
    const fid = _currentFileId.value;
    if (!fid) return;
    const cur = _manualByFile.value.get(fid) ?? [];
    // Click on the last-added id: pop it (undo). Click on any other already-
    // selected id: ignore (no duplicates in a chain).
    if (cur.length > 0 && cur[cur.length - 1] === entityId) {
      setMapEntry(_manualByFile, fid, cur.slice(0, -1));
      return;
    }
    if (cur.includes(entityId)) return;
    setMapEntry(_manualByFile, fid, [...cur, entityId]);
  }

  /** Drop the entire manual chain for the active file. */
  function clearManual(): void {
    const fid = _currentFileId.value;
    if (!fid) return;
    setMapEntry(_manualByFile, fid, []);
  }

  /** Send the manual chain to the backend for closure + summary. */
  async function confirmManual(): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    const ids = _manualByFile.value.get(fid) ?? [];
    if (ids.length === 0) {
      setError('手動選択された線がありません');
      return;
    }
    setError(null);
    _isDetectingOuter.value = true;
    try {
      const res = await api.confirmOuterManual(sid, fid, ids);
      setMapEntry(_outerByFile, fid, res);
      _manualMode.value = false;
    } catch (err) {
      setError(err instanceof Error ? err.message : '閉ループになっていません');
    } finally {
      _isDetectingOuter.value = false;
    }
  }

  /** Recompute the offset preview using the current default + overrides.
   *
   *  Uses an AbortController per request (M5): when a new computation is
   *  scheduled while an old one is still in flight (e.g. user spams the
   *  ± buttons), the previous request is cancelled so its (now stale)
   *  result cannot overwrite a fresher one. */
  let _offsetAbort: AbortController | null = null;
  async function computeOffset(): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) {
      setError('開いているDXFがありません');
      return;
    }
    setError(null);
    _isComputingOffset.value = true;
    if (_offsetAbort) _offsetAbort.abort();
    const ctrl = new AbortController();
    _offsetAbort = ctrl;
    try {
      const req: OffsetRequest = {
        default_mm: _defaultOffsetMm.value,
        edge_overrides: { ..._edgeOverrides.value },
        corner_join: _cornerJoin.value,
      };
      const res = await api.computeOffset(sid, fid, req, ctrl.signal);
      // Bail out if a newer request superseded us mid-flight.
      if (ctrl.signal.aborted) return;
      setMapEntry(_offsetByFile, fid, res);
    } catch (err) {
      // AbortError is expected when we cancel — don't surface it.
      if ((err as { name?: string } | null)?.name === 'AbortError') return;
      setError(err instanceof Error ? err.message : '加工代の計算に失敗しました');
    } finally {
      if (_offsetAbort === ctrl) _offsetAbort = null;
      _isComputingOffset.value = false;
    }
  }

  function setDefaultOffset(mm: number): void {
    if (!Number.isFinite(mm)) return;
    // Clamp to a sensible range — backend will validate too but a UI guard
    // prevents accidental negative/giant values during num-step interaction.
    const clamped = Math.max(0, Math.min(50, Number(mm.toFixed(2))));
    _defaultOffsetMm.value = clamped;
  }

  /** Apply a per-edge offset override.
   *
   *  ``edgeLabel`` is the 1-based loop traversal label (``E1``..``En``,
   *  or the composite ``E1#k`` form for closed-polyline segments) — this
   *  matches the backend's ``edge_overrides`` dict shape exactly (C1). */
  function setEdgeOverride(edgeLabel: string, mm: number): void {
    if (!Number.isFinite(mm)) return;
    const clamped = Math.max(0, Math.min(50, Number(mm.toFixed(2))));
    _edgeOverrides.value = { ..._edgeOverrides.value, [edgeLabel]: clamped };
  }

  function clearEdgeOverride(edgeLabel: string): void {
    if (!(edgeLabel in _edgeOverrides.value)) return;
    const next = { ..._edgeOverrides.value };
    delete next[edgeLabel];
    _edgeOverrides.value = next;
  }

  function setCornerJoin(join: CornerJoin): void {
    _cornerJoin.value = join;
  }

  /* -------------------- Phase 3 — chamfer / pdf / frame ------------------- */

  /** Fetch the outer-loop corners + edges for the active file (idempotent).
   *  H1: edges power the 開先/bevel UI section in the Inspector. */
  async function loadCorners(): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    // Skip if already loaded (corners are stable per file).
    if (_cornersByFile.value.has(fid)) return;
    _isLoadingCorners.value = true;
    try {
      const res = await api.getCorners(sid, fid);
      setMapEntry(_cornersByFile, fid, res.corners);
      setMapEntry(_edgesByFile, fid, res.edges);
    } catch (err) {
      setError(err instanceof Error ? err.message : '角の取得に失敗しました');
    } finally {
      _isLoadingCorners.value = false;
    }
  }

  /** Force re-fetch of the persisted chamfer + geometry. */
  async function loadChamfer(): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    try {
      const ch = await api.getChamfer(sid, fid);
      if (ch) {
        setMapEntry(_chamferByFile, fid, ch.specs);
        setMapEntry(_chamferGeometryByFile, fid, ch.geometry);
      } else {
        setMapEntry(_chamferByFile, fid, []);
        setMapEntry(_chamferGeometryByFile, fid, { items: [] });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'C面情報の取得に失敗しました');
    }
  }

  /** Internal: POST the current spec list for the active file. */
  async function syncChamfer(specs: ChamferSpec[]): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    _isApplyingChamfer.value = true;
    try {
      const res = await api.setChamfer(sid, fid, specs);
      setMapEntry(_chamferByFile, fid, res.specs);
      setMapEntry(_chamferGeometryByFile, fid, res.geometry);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'C面の適用に失敗しました');
    } finally {
      _isApplyingChamfer.value = false;
    }
  }

  /** Add or update a single corner's chamfer spec. */
  async function setChamferSpec(spec: ChamferSpec): Promise<void> {
    const fid = _currentFileId.value;
    if (!fid) return;
    const current = _chamferByFile.value.get(fid) ?? [];
    const next = current.filter((s) => s.corner_id !== spec.corner_id);
    next.push(spec);
    await syncChamfer(next);
  }

  /** Remove the chamfer spec for a single corner. */
  async function removeChamferSpec(corner_id: string): Promise<void> {
    const fid = _currentFileId.value;
    if (!fid) return;
    const current = _chamferByFile.value.get(fid) ?? [];
    const next = current.filter((s) => s.corner_id !== corner_id);
    if (next.length === current.length) return;
    await syncChamfer(next);
  }

  /** Clear every chamfer spec for the active file. */
  async function clearChamfer(): Promise<void> {
    const fid = _currentFileId.value;
    if (!fid) return;
    const current = _chamferByFile.value.get(fid) ?? [];
    if (current.length === 0) return;
    await syncChamfer([]);
  }

  function setChamferDefaultSize(mm: number): void {
    if (!Number.isFinite(mm)) return;
    _chamferDefaultSize.value = Math.max(0.5, Math.min(50, Number(mm.toFixed(1))));
  }
  function setChamferDefaultAngle(deg: number): void {
    if (!Number.isFinite(deg)) return;
    _chamferDefaultAngle.value = Math.max(5, Math.min(90, Math.round(deg)));
  }

  /** POST /cleanup-frame — reserve the frame entities for deletion and
   *  refresh the file so they disappear from the canvas. */
  async function cleanupFrame(): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) {
      setError('開いているDXFがありません');
      return;
    }
    setError(null);
    _isCleaningFrame.value = true;
    try {
      const res = await api.cleanupFrame(sid, fid);
      _lastFrameCleanup.value = {
        removed_count: res.removed_count,
        ids: res.frame_entity_ids,
      };
      // Phase 6: invalidate cached server render — geometry just changed.
      setMapEntry(_renderedSvgByFile, fid, undefined);
      // Refresh the file so deleted_ids includes the new ids.
      await loadFile(fid);
    } catch (err) {
      setError(err instanceof Error ? err.message : '製作図枠の削除に失敗しました');
    } finally {
      _isCleaningFrame.value = false;
    }
  }

  function setPdfFrameOption(frame: PdfFrameOption): void {
    _pdfExportOptions.value = { ..._pdfExportOptions.value, frame };
  }
  function setPdfWithOffset(on: boolean): void {
    _pdfExportOptions.value = { ..._pdfExportOptions.value, with_offset: on };
  }
  function setPdfWithChamfer(on: boolean): void {
    _pdfExportOptions.value = { ..._pdfExportOptions.value, with_chamfer: on };
  }
  function setPdfWithDimensions(on: boolean): void {
    _pdfExportOptions.value = { ..._pdfExportOptions.value, with_dimensions: on };
  }
  function setPdfWithAddedHoles(on: boolean): void {
    _pdfExportOptions.value = { ..._pdfExportOptions.value, with_added_holes: on };
  }
  function setPdfWithNotes(on: boolean): void {
    _pdfExportOptions.value = { ..._pdfExportOptions.value, with_notes: on };
  }
  function setPdfWithBridges(on: boolean): void {
    _pdfExportOptions.value = { ..._pdfExportOptions.value, with_bridges: on };
  }
  function setPdfWithEdits(on: boolean): void {
    _pdfExportOptions.value = { ..._pdfExportOptions.value, with_edits: on };
  }
  /** Update the optional material string that lands on the PDF header (H4). */
  function setPdfMaterial(text: string): void {
    _pdfMaterial.value = text;
    _pdfExportOptions.value = {
      ..._pdfExportOptions.value,
      material: text.trim() ? text : undefined,
    };
  }

  /** Download the PDF using the supplied (or stored) options. */
  async function exportPdf(opts?: Partial<PdfExportOptions>): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    const file = currentFile.value;
    if (!sid || !fid || !file) {
      setError('開いているDXFがありません');
      return;
    }
    setError(null);
    _isExportingPdf.value = true;
    try {
      const effective: PdfExportOptions = { ..._pdfExportOptions.value, ...(opts ?? {}) };
      const blob = await api.exportPdf(sid, fid, effective);
      const base = file.name.replace(/\.[Dd][Xx][Ff]$/, '');
      downloadBlob(blob, `${base}.pdf`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'PDF出力に失敗しました');
    } finally {
      _isExportingPdf.value = false;
    }
  }

  /* -------------------- Phase 4 — annotation actions --------------------- */

  /** Refresh all five Phase 4 lists for the active file. */
  async function loadAnnotations(): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    try {
      const [dims, holes, notes_, bridges_, edits_] = await Promise.all([
        api.listDimensions(sid, fid).catch(() => [] as Dimension[]),
        api.listHoles(sid, fid).catch(() => [] as AddedHole[]),
        api.listNotes(sid, fid).catch(() => [] as Note[]),
        api.listBridges(sid, fid).catch(() => [] as Bridge[]),
        api.listEdits(sid, fid).catch(() => [] as EditedVertex[]),
      ]);
      setMapEntry(_dimensionsByFile, fid, dims);
      setMapEntry(_addedHolesByFile, fid, holes);
      setMapEntry(_notesByFile, fid, notes_);
      setMapEntry(_bridgesByFile, fid, bridges_);
      setMapEntry(_vertexEditsByFile, fid, edits_);
    } catch (err) {
      setError(err instanceof Error ? err.message : '注釈情報の取得に失敗しました');
    }
  }

  /** Dimension: append after a 2-point click. */
  async function addDimension(p1: [number, number], p2: [number, number]): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    _isAddingDimension.value = true;
    try {
      const dim: Dimension = {
        id: makeId('dim'),
        type: _dimType.value,
        p1,
        p2,
        style: 'iso',
      };
      // Backend is last-write-wins for the list; just send current + new.
      const next = await api.addDimension(sid, fid, dim);
      setMapEntry(_dimensionsByFile, fid, next);
    } catch (err) {
      setError(err instanceof Error ? err.message : '寸法の追加に失敗しました');
    } finally {
      _isAddingDimension.value = false;
    }
  }

  async function removeDimension(id: string): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    try {
      await api.removeDimension(sid, fid, id);
      const list = _dimensionsByFile.value.get(fid) ?? [];
      setMapEntry(_dimensionsByFile, fid, list.filter((d) => d.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : '寸法の削除に失敗しました');
    }
  }

  function setDimPrecision(n: number): void {
    if (!Number.isFinite(n)) return;
    _dimPrecision.value = Math.max(0, Math.min(4, Math.round(n)));
  }
  function setDimType(t: DimensionType): void { _dimType.value = t; }
  function setDimTwoPointMode(on: boolean): void { _dimTwoPointMode.value = on; }
  function setPendingDimStart(p: [number, number] | null): void { _pendingDimStart.value = p; }

  /** Edit: snap a cursor coord then commit the vertex translation. */
  async function snapPoint(cursor: [number, number]): Promise<SnapResult> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) {
      const fallback: SnapResult = { snapped: cursor, type: null };
      _lastSnap.value = fallback;
      return fallback;
    }
    try {
      const r = await api.getSnapPoint(sid, fid, cursor);
      _lastSnap.value = r;
      return r;
    } catch {
      const fallback: SnapResult = { snapped: cursor, type: null };
      _lastSnap.value = fallback;
      return fallback;
    }
  }

  async function applyVertexEdit(
    entity_id: string,
    vertex_index: number,
    _original: [number, number],
    position: [number, number],
  ): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    try {
      const merged = await api.editVertex(sid, fid, {
        entity_id,
        vertex_index,
        new_position: position,
      });
      setMapEntry(_vertexEditsByFile, fid, merged);
      // Phase 6 / HIGH-2: geometry mutated — drop the stale background
      // SVG. The next ``loadRenderedSvg`` will re-fetch with
      // ``apply_edits=true`` so the new render reflects the vertex
      // translations rather than the pre-edit DXF.
      setMapEntry(_renderedSvgByFile, fid, undefined);
    } catch (err) {
      setError(err instanceof Error ? err.message : '頂点編集に失敗しました');
    }
  }

  function selectEditTarget(entity_id: string | null, vertex_index = 0): void {
    _editSelection.value = entity_id ? { entity_id, vertex_index } : null;
  }
  function setEditSnap(on: boolean): void { _editSnapEnabled.value = on; }
  function setEditOrtho(on: boolean): void { _editOrtho.value = on; }

  /** Hole: append at the given centre using the current diameter. */
  async function addHole(position: [number, number]): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    _isAddingHole.value = true;
    try {
      const hole: AddedHole = {
        id: makeId('h'),
        position,
        diameter: _holeDiameter.value,
      };
      const next = await api.addHole(sid, fid, hole);
      setMapEntry(_addedHolesByFile, fid, next);
    } catch (err) {
      setError(err instanceof Error ? err.message : '穴の追加に失敗しました');
    } finally {
      _isAddingHole.value = false;
    }
  }

  async function addHolePattern(anchor: [number, number]): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    const req: HolePatternRequest = {
      anchor,
      rows: _holePatternRows.value,
      cols: _holePatternCols.value,
      spacing: [_holePatternPitchX.value, _holePatternPitchY.value],
      diameter: _holeDiameter.value,
    };
    _isAddingHole.value = true;
    try {
      const merged = await api.addHolePattern(sid, fid, req);
      setMapEntry(_addedHolesByFile, fid, merged);
      _holePatternOpen.value = false;
    } catch (err) {
      setError(err instanceof Error ? err.message : '整列穴の追加に失敗しました');
    } finally {
      _isAddingHole.value = false;
    }
  }

  async function removeHole(id: string): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    try {
      await api.removeHole(sid, fid, id);
      const list = _addedHolesByFile.value.get(fid) ?? [];
      setMapEntry(_addedHolesByFile, fid, list.filter((h) => h.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : '穴の削除に失敗しました');
    }
  }

  function setHoleDiameter(d: number): void {
    if (!Number.isFinite(d)) return;
    _holeDiameter.value = Math.max(0.5, Math.min(200, Number(d.toFixed(2))));
  }
  function setHoleContinuous(on: boolean): void { _holeContinuous.value = on; }
  function setHolePatternOpen(on: boolean): void { _holePatternOpen.value = on; }

  /** Note: append at the given anchor with text + current preset/height.
   *  Backend uses ``font_size_mm`` (was ``height`` in the legacy client). */
  async function addNote(
    position: [number, number],
    text: string,
    preset?: NotePreset,
  ): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid || !text.trim()) return;
    _isAddingNote.value = true;
    try {
      const note: Note = {
        id: makeId('n'),
        position,
        text: text.trim(),
        preset: preset ?? _notePreset.value,
        font_size_mm: _noteHeight.value,
      };
      const merged = await api.addNote(sid, fid, note);
      setMapEntry(_notesByFile, fid, merged);
      _notePendingAnchor.value = null;
    } catch (err) {
      setError(err instanceof Error ? err.message : '注記の追加に失敗しました');
    } finally {
      _isAddingNote.value = false;
    }
  }

  async function removeNote(id: string): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    try {
      await api.removeNote(sid, fid, id);
      const list = _notesByFile.value.get(fid) ?? [];
      setMapEntry(_notesByFile, fid, list.filter((n) => n.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : '注記の削除に失敗しました');
    }
  }

  function setNotePreset(p: NotePreset): void { _notePreset.value = p; }
  function setNotePendingAnchor(p: [number, number] | null): void { _notePendingAnchor.value = p; }

  /** Bridge: append at the given position_ratio on the given outer edge.
   *  C1: backend identifies a bridge by ``(edge_id, position_ratio)`` —
   *  callers that have an XY anchor must convert it to ratio first
   *  (CanvasArea.vue does this once it picked which edge was clicked). */
  async function addBridge(
    edge_id: string,
    position_ratio: number,
  ): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    _isAddingBridge.value = true;
    try {
      const bridge: Bridge = {
        id: makeId('b'),
        edge_id,
        position_ratio: Math.max(0, Math.min(1, position_ratio)),
        width_mm: _bridgeWidth.value,
      };
      const merged = await api.addBridge(sid, fid, bridge);
      setMapEntry(_bridgesByFile, fid, merged);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'ブリッジの追加に失敗しました');
    } finally {
      _isAddingBridge.value = false;
    }
  }

  async function addBridgeAuto(): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    _isAddingBridge.value = true;
    try {
      // H7: backend ``/bridges/auto`` REPLACES the full list — we set
      // the entire returned array so manual entries placed before auto
      // are also dropped (matches what the server did silently before).
      const next = await api.addBridgeAuto(sid, fid, _bridgeRecommended.value, _bridgeWidth.value);
      setMapEntry(_bridgesByFile, fid, next);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'ブリッジ自動配置に失敗しました');
    } finally {
      _isAddingBridge.value = false;
    }
  }

  async function removeBridge(id: string): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    if (!sid || !fid) return;
    try {
      await api.removeBridge(sid, fid, id);
      const list = _bridgesByFile.value.get(fid) ?? [];
      setMapEntry(_bridgesByFile, fid, list.filter((b) => b.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'ブリッジの削除に失敗しました');
    }
  }

  function setBridgeWidth(w: number): void {
    if (!Number.isFinite(w)) return;
    _bridgeWidth.value = Math.max(0.5, Math.min(20, Number(w.toFixed(2))));
  }
  function setBridgeRecommended(n: number): void {
    if (!Number.isFinite(n)) return;
    _bridgeRecommended.value = Math.max(1, Math.min(20, Math.round(n)));
  }

  /* -------------------- Phase 5 — nesting / history / templates ---------- */

  /** Toggle a file in/out of the nesting selection. Empty selection = "all". */
  function toggleNestFile(fid: string): void {
    const next = new Set(_nestSelectedFileIds.value);
    if (next.has(fid)) next.delete(fid);
    else next.add(fid);
    _nestSelectedFileIds.value = next;
  }
  function setNestFiles(ids: string[]): void {
    _nestSelectedFileIds.value = new Set(ids);
  }
  function clearNestFiles(): void {
    _nestSelectedFileIds.value = new Set();
  }
  function setNestSheetWidth(mm: number): void {
    if (!Number.isFinite(mm)) return;
    _nestSheetWidth.value = Math.max(50, Math.min(10000, Math.round(mm)));
  }
  function setNestSheetHeight(mm: number): void {
    if (!Number.isFinite(mm)) return;
    _nestSheetHeight.value = Math.max(50, Math.min(20000, Math.round(mm)));
  }
  function setNestSpacing(mm: number): void {
    if (!Number.isFinite(mm)) return;
    _nestSpacingMm.value = Math.max(0, Math.min(50, Number(mm.toFixed(1))));
  }
  function setNestSheetQuantity(n: number): void {
    if (!Number.isFinite(n)) return;
    // H9: BE side caps at 20 — clamp on the UI too to avoid 422s.
    _nestSheetQuantity.value = Math.max(1, Math.min(20, Math.round(n)));
  }
  function setNestAlgorithm(a: NestAlgorithm): void { _nestAlgorithm.value = a; }
  function setNestAllowRotate(on: boolean): void { _nestAllowRotate.value = on; }

  /** Poll handle so a follow-up run cancels the previous timer. */
  let _nestPollHandle: number | null = null;
  /** Track the currently-polled job_id so out-of-order responses don't trip us. */
  let _nestActiveJobId: string | null = null;
  function stopNestPolling(): void {
    if (_nestPollHandle !== null) {
      window.clearTimeout(_nestPollHandle);
      _nestPollHandle = null;
    }
    _nestActiveJobId = null;
  }

  /** Issue one /api/jobs/{id} poll, recurse at 1 s while still running.
   *
   *  C2/C6: status enum is the backend wire form
   *  (``pending`` | ``running`` | ``completed`` | ``failed``). We use an
   *  **explicit allow-list** for the re-schedule path — anything outside
   *  that list (unknown status, malformed payload) stops polling so we
   *  never spin forever. (M7) `_nestActiveJobId` guards against a stale
   *  poll from a previous job firing after a fresh ``runNesting``. */
  async function pollJob(job_id: string): Promise<void> {
    if (_nestActiveJobId !== null && _nestActiveJobId !== job_id) {
      // A newer job has taken over — let the new poll loop run.
      return;
    }
    try {
      const j = await api.getJobStatus(job_id);
      // The result must still be for the active job. If runNesting kicked
      // a new job mid-flight we discard this response.
      if (_nestActiveJobId !== null && _nestActiveJobId !== job_id) return;
      _nestingJob.value = j;
      if (j.status === 'completed') {
        await loadNestingResult(job_id);
        stopNestPolling();
        return;
      }
      if (j.status === 'failed') {
        setError(j.error ?? 'ネスティングジョブが失敗しました');
        stopNestPolling();
        return;
      }
      if (j.status === 'pending' || j.status === 'running') {
        // Re-arm 1 s timer (closure pins job_id explicitly).
        _nestPollHandle = window.setTimeout(() => pollJob(job_id), 1000);
        return;
      }
      // Unknown / unsupported status — bail out instead of looping forever.
      // eslint-disable-next-line no-console
      console.warn(`pollJob: unexpected job status ${String(j.status)} — stopping`);
      stopNestPolling();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'ジョブ状態の取得に失敗しました');
      stopNestPolling();
    }
  }

  /** Kick off a nesting job and start polling immediately. */
  async function runNesting(): Promise<void> {
    const sid = _currentSession.value?.session_id;
    if (!sid) {
      setError('セッションがアクティブではありません');
      return;
    }
    setError(null);
    // M7: stop any previous polling so a stale timer can't write into the
    // newly-running job (and so the active-job-id guard above resets).
    stopNestPolling();
    _nestingResult.value = null;
    _isRunningNest.value = true;
    try {
      // C1: BE-shaped NestRequest (sheet wrapper + ``rotation`` flag + algo enum).
      // If the operator left ``_nestSelectedFileIds`` empty, default to every
      // file in the session so the backend doesn't 422 on an empty list.
      const explicitIds = [..._nestSelectedFileIds.value];
      const fileIds = explicitIds.length > 0
        ? explicitIds
        : (_currentSession.value?.files ?? []).map((f) => f.file_id);
      const req: NestRequest = {
        file_ids: fileIds,
        sheet: {
          width_mm: _nestSheetWidth.value,
          height_mm: _nestSheetHeight.value,
          quantity: _nestSheetQuantity.value,
        },
        spacing_mm: _nestSpacingMm.value,
        algorithm: _nestAlgorithm.value,
        rotation: _nestAllowRotate.value,
      };
      const { job_id } = await api.nest(sid, req);
      _nestActiveJobId = job_id;
      _nestingJob.value = {
        job_id,
        status: 'pending',
        progress: 0,
        message: 'キューに登録しました',
      };
      // Kick off polling without awaiting so the UI returns immediately.
      pollJob(job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'ネスティング実行に失敗しました');
    } finally {
      _isRunningNest.value = false;
    }
  }

  /** Fetch the full sheets payload for a completed job. */
  async function loadNestingResult(job_id: string): Promise<void> {
    try {
      const r = await api.getNestResult(job_id);
      _nestingResult.value = r;
      if (r.warnings.length > 0) {
        // Surface the first warning as a non-blocking error banner; the
        // result panel still renders so the operator can see partial output.
        setError(r.warnings[0]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'ネスティング結果の取得に失敗しました');
    }
  }

  /** Download a single sheet as DXF. */
  async function exportNestSheet(sheet_index: number): Promise<void> {
    const job = _nestingJob.value;
    if (!job) return;
    try {
      const blob = await api.exportNestSheet(job.job_id, sheet_index);
      downloadBlob(blob, `nest_${job.job_id}_sheet${sheet_index}.dxf`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'シートDXFの書き出しに失敗しました');
    }
  }

  /** Persist the current session under the given name. Refreshes the
   *  cached list so the History dropdown shows it immediately. */
  async function saveCurrentSession(name: string): Promise<void> {
    const sid = _currentSession.value?.session_id;
    if (!sid) {
      setError('保存対象のセッションがありません');
      return;
    }
    const trimmed = name.trim();
    if (!trimmed) {
      setError('名前を入力してください');
      return;
    }
    setError(null);
    _isSavingSession.value = true;
    try {
      await api.saveSession(trimmed, sid);
      _savedSessions.value = await api.listSavedSessions();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'セッション保存に失敗しました');
    } finally {
      _isSavingSession.value = false;
    }
  }

  /** Refresh the saved-session list (used by the Header history dropdown). */
  async function listSavedSessions(): Promise<void> {
    try {
      _savedSessions.value = await api.listSavedSessions();
    } catch (err) {
      setError(err instanceof Error ? err.message : '履歴の取得に失敗しました');
    }
  }

  /** Load a saved session — replaces the current session and refreshes the
   *  active file's payload so the canvas snaps to the new geometry. */
  async function loadSavedSession(name: string): Promise<void> {
    setError(null);
    try {
      const next = await api.loadSession(name);
      _currentSession.value = next;
      _files.value = new Map();
      _selectedForDelete.value = new Set();
      _nestingResult.value = null;
      _nestingJob.value = null;
      if (next.files.length > 0) await selectFile(next.files[0].file_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'セッションの読み込みに失敗しました');
    }
  }

  /** Lazy-load templates the first time the panel opens. */
  async function loadTemplates(): Promise<void> {
    _isLoadingTemplates.value = true;
    try {
      _templates.value = await api.getTemplates();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'テンプレート取得に失敗しました');
    } finally {
      _isLoadingTemplates.value = false;
    }
  }

  /* -------------------- Phase 6 — server-rendered SVG ------------------- */

  /** Fetch (and cache) the ezdxf-rendered SVG for the active file. Safe to
   *  call repeatedly — already-cached entries short-circuit unless ``force``
   *  is true (used by delete/edit after they mutate the underlying geometry).
   *  Errors are swallowed into ``lastError`` so a backend that hasn't yet
   *  exposed ``/render-svg`` (or one returning 500 on an exotic DXF) cannot
   *  break the canvas — the foreground layer keeps working in either case. */
  async function loadRenderedSvg(fid?: string, force = false): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const file_id = fid ?? _currentFileId.value;
    if (!sid || !file_id) return;
    if (!force && _renderedSvgByFile.value.has(file_id)) return;
    _isLoadingRenderedSvg.value = true;
    try {
      // HIGH-2: the backend honours ``apply_edits=true`` by reading the
      // persisted Phase 4 vertex edits and baking them into the live
      // ezdxf document before render, so the background SVG reflects the
      // operator's line-edit translations rather than a frozen pre-edit
      // snapshot.
      const res = await api.renderSvg(sid, file_id, {
        apply_deletions: true,
        apply_edits: true,
        dark_theme: true,
      });
      setMapEntry(_renderedSvgByFile, file_id, res);
    } catch (err) {
      // Non-fatal: foreground layer remains usable.
      setError(err instanceof Error ? err.message : '背景レンダリングに失敗しました');
    } finally {
      _isLoadingRenderedSvg.value = false;
    }
  }

  /** Drop the cached SVG for a file (or the active file if omitted) so the
   *  next paint re-fetches it. Called after delete / edit / cleanup-frame so
   *  the background catches up with the geometry change. */
  function clearRenderedSvg(fid?: string): void {
    const file_id = fid ?? _currentFileId.value;
    if (!file_id) return;
    if (!_renderedSvgByFile.value.has(file_id)) return;
    setMapEntry(_renderedSvgByFile, file_id, undefined);
  }

  /** Toggle the canvas render mode. Persisted to localStorage so a reload
   *  keeps the operator's preference. The CanvasArea component watches this
   *  and triggers a lazy ``loadRenderedSvg`` when flipping into 'real'. */
  function setRenderMode(mode: RenderMode): void {
    if (_renderMode.value === mode) return;
    _renderMode.value = mode;
    try {
      window.localStorage?.setItem(_RENDER_MODE_KEY, mode);
    } catch {
      // localStorage may be unavailable (private mode / SSR) — non-fatal.
    }
  }
  function toggleRenderMode(): void {
    setRenderMode(_renderMode.value === 'real' ? 'simple' : 'real');
  }

  /** Apply a template — server-side write + local Inspector defaults so the
   *  operator sees the change without a tab switch.
   *
   *  C5: Backend response is ``ApplyTemplateResponse`` which embeds a full
   *  ``Template`` payload (under ``template``) plus the canonical
   *  ``default_offset_mm`` it just wrote. We sync the inspector defaults
   *  from either source so a backend that hasn't shipped the embedded
   *  template yet still leaves the offset right. */
  async function applyTemplate(template_id: string): Promise<void> {
    const sid = _currentSession.value?.session_id;
    if (!sid) {
      setError('適用先のセッションがありません');
      return;
    }
    setError(null);
    try {
      const resp = await api.applyTemplate(sid, template_id);
      // H7: 全件 skipped でも 207 で body は返ってくる — UI 既定値は更新する
      // が、ユーザーには警告を出す。
      if (resp.applied_to.length === 0 && resp.skipped.length > 0) {
        setError(
          `テンプレートが ${resp.skipped.length} ファイルに適用できませんでした (template_id=${resp.template_id})`,
        );
      }
      const tpl = resp.template;
      const offset = typeof resp.default_offset_mm === 'number'
        ? resp.default_offset_mm
        : tpl?.spacing_mm;
      if (typeof offset === 'number') {
        _defaultOffsetMm.value = offset;
        _nestSpacingMm.value = offset;
      }
      if (tpl?.sheet_width && typeof tpl.sheet_width === 'number') {
        _nestSheetWidth.value = tpl.sheet_width;
      }
      if (tpl?.sheet_height && typeof tpl.sheet_height === 'number') {
        _nestSheetHeight.value = tpl.sheet_height;
      }
      if (tpl?.material) setPdfMaterial(tpl.material);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'テンプレート適用に失敗しました');
    }
  }

  return {
    // state
    currentSession: _currentSession,
    currentFileId: _currentFileId,
    currentFile,
    visibleEntities,
    files: _files,
    selectedForDelete: _selectedForDelete,
    rectSelectMode: _rectSelectMode,
    rectSelectInvert: _rectSelectInvert,
    protectOuterFromRect: _protectOuterFromRect,
    isUploading: _isUploading,
    isLoadingFile: _isLoadingFile,
    isDeleting: _isDeleting,
    lastError: _lastError,
    isLiveBackend: _isLiveBackend,
    // Phase 2 state
    isDetectingOuter: _isDetectingOuter,
    isComputingOffset: _isComputingOffset,
    manualMode: _manualMode,
    defaultOffsetMm: _defaultOffsetMm,
    edgeOverrides: _edgeOverrides,
    cornerJoin: _cornerJoin,
    // Phase 3 state
    pdfExportOptions: _pdfExportOptions,
    dxfExportOptions: _dxfExportOptions,
    pdfMaterial: _pdfMaterial,
    chamferDefaultSize: _chamferDefaultSize,
    chamferDefaultAngle: _chamferDefaultAngle,
    lastFrameCleanup: _lastFrameCleanup,
    isLoadingCorners: _isLoadingCorners,
    isApplyingChamfer: _isApplyingChamfer,
    isExportingPdf: _isExportingPdf,
    isCleaningFrame: _isCleaningFrame,
    // derived
    deleteRows,
    totalDeleteCandidates,
    remainingAfterDelete,
    outerDetection,
    offsetResult,
    manualSelection,
    outerEntityIdSet,
    // Phase 3 derived
    corners,
    edges,
    chamferSpecs,
    chamferGeometry,
    chamferSpecByCorner,
    // actions
    uploadFiles,
    selectFile,
    loadFile,
    selectEntity,
    toggleCategory,
    isCategoryOn,
    clearSelection,
    setRectSelectMode,
    setRectInvert,
    setProtectOuterFromRect,
    selectByRect,
    executeDelete,
    exportDxf,
    exportDxfWithOffset,
    // Phase 2 actions
    detectOuter,
    setManualMode,
    addToManual,
    clearManual,
    confirmManual,
    computeOffset,
    setDefaultOffset,
    setEdgeOverride,
    clearEdgeOverride,
    setCornerJoin,
    // Phase 3 actions
    loadCorners,
    loadChamfer,
    setChamferSpec,
    removeChamferSpec,
    clearChamfer,
    setChamferDefaultSize,
    setChamferDefaultAngle,
    cleanupFrame,
    setPdfFrameOption,
    setPdfWithOffset,
    setPdfWithChamfer,
    setPdfWithDimensions,
    setPdfWithAddedHoles,
    setPdfWithNotes,
    setPdfWithBridges,
    setPdfWithEdits,
    setDxfWithOffset,
    setDxfWithDimensions,
    setDxfWithAddedHoles,
    setDxfWithNotes,
    setDxfWithBridges,
    setDxfWithEdits,
    setPdfMaterial,
    exportPdf,
    // Phase 4 state
    dimType: _dimType,
    dimPrecision: _dimPrecision,
    dimArrowSize: _dimArrowSize,
    pendingDimStart: _pendingDimStart,
    dimTwoPointMode: _dimTwoPointMode,
    editSnapEnabled: _editSnapEnabled,
    editGridSnap: _editGridSnap,
    editOrtho: _editOrtho,
    editSelection: _editSelection,
    holeDiameter: _holeDiameter,
    holeContinuous: _holeContinuous,
    holePatternOpen: _holePatternOpen,
    holePatternRows: _holePatternRows,
    holePatternCols: _holePatternCols,
    holePatternPitchX: _holePatternPitchX,
    holePatternPitchY: _holePatternPitchY,
    notePreset: _notePreset,
    noteHeight: _noteHeight,
    notePendingAnchor: _notePendingAnchor,
    bridgeWidth: _bridgeWidth,
    bridgeRecommended: _bridgeRecommended,
    lastSnap: _lastSnap,
    isAddingDimension: _isAddingDimension,
    isAddingHole: _isAddingHole,
    isAddingNote: _isAddingNote,
    isAddingBridge: _isAddingBridge,
    // Phase 4 derived
    dimensions,
    vertexEdits,
    addedHoles,
    notes,
    bridges,
    // Phase 4 actions
    loadAnnotations,
    addDimension,
    removeDimension,
    setDimType,
    setDimPrecision,
    setDimTwoPointMode,
    setPendingDimStart,
    snapPoint,
    applyVertexEdit,
    selectEditTarget,
    setEditSnap,
    setEditOrtho,
    addHole,
    addHolePattern,
    removeHole,
    setHoleDiameter,
    setHoleContinuous,
    setHolePatternOpen,
    addNote,
    removeNote,
    setNotePreset,
    setNotePendingAnchor,
    addBridge,
    addBridgeAuto,
    removeBridge,
    setBridgeWidth,
    setBridgeRecommended,
    clearError: () => setError(null),
    // picker plumbing (M4)
    registerFilePicker,
    registerFolderPicker,
    openFilePicker,
    openFolderPicker,
    // Phase 5 state
    nestingJob: _nestingJob,
    nestingResult: _nestingResult,
    savedSessions: _savedSessions,
    templates: _templates,
    nestSheetWidth: _nestSheetWidth,
    nestSheetHeight: _nestSheetHeight,
    nestSheetQuantity: _nestSheetQuantity,
    nestSpacingMm: _nestSpacingMm,
    nestAlgorithm: _nestAlgorithm,
    nestAllowRotate: _nestAllowRotate,
    nestSelectedFileIds: _nestSelectedFileIds,
    isRunningNest: _isRunningNest,
    isSavingSession: _isSavingSession,
    isLoadingTemplates: _isLoadingTemplates,
    // Phase 5 actions
    toggleNestFile,
    setNestFiles,
    clearNestFiles,
    setNestSheetWidth,
    setNestSheetHeight,
    setNestSheetQuantity,
    setNestSpacing,
    setNestAlgorithm,
    setNestAllowRotate,
    runNesting,
    pollJob,
    loadNestingResult,
    exportNestSheet,
    saveCurrentSession,
    listSavedSessions,
    loadSavedSession,
    loadTemplates,
    applyTemplate,
    // Phase 6 — server-rendered SVG (背景レイヤー)
    renderedSvg,
    renderMode: _renderMode,
    isLoadingRenderedSvg: _isLoadingRenderedSvg,
    loadRenderedSvg,
    clearRenderedSvg,
    setRenderMode,
    toggleRenderMode,
  };
}

/** Map an EntityCategory to the CSS class hook used by components.css. */
export function categoryClass(e: Entity): string {
  switch (e.category) {
    case 'outer':   return 'ent outer';
    case 'hole':    return 'ent hole';
    case 'dim':     return 'ent dim';
    case 'balloon': return 'ent balloon';
    case 'tap':     return 'ent tap';
    case 'frame':   return 'ent frame';
    default:        return 'ent';
  }
}
