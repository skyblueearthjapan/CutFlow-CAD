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
  ChamferGeometry,
  ChamferSpec,
  CornerInfo,
  CornerJoin,
  DeleteCategoryKey,
  DeleteCategoryRow,
  EdgeInfo,
  Entity,
  FileData,
  OffsetRequest,
  OffsetResult,
  OuterDetectionResult,
  PdfExportOptions,
  PdfFrameOption,
  Session,
} from '../types/dxf';
import { useActiveTool } from './activeTool';

const _currentSession = ref<Session | null>(null);
const _currentFileId = ref<string | null>(null);
/** Use shallowRef so swapping a whole Map doesn't deep-track every entity. */
const _files = shallowRef<Map<string, FileData>>(new Map());
const _selectedForDelete = ref<Set<string>>(new Set());
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
      const [data, outer, offset, chamfer] = await Promise.all([
        api.getFile(sid, fid),
        api.getOuter(sid, fid).catch(() => null),
        api.getOffset(sid, fid).catch(() => null),
        api.getChamfer(sid, fid).catch(() => null),
      ]);
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
      await loadFile(fid);
      _selectedForDelete.value = new Set();
    } catch (err) {
      setError(err instanceof Error ? err.message : '削除に失敗しました');
    } finally {
      _isDeleting.value = false;
    }
  }

  /** Download the cleaned DXF for the active file (no offset). */
  async function exportDxf(): Promise<void> {
    await exportDxfInternal(false);
  }

  /** Download the cleaned DXF with the computed offset embedded. */
  async function exportDxfWithOffset(): Promise<void> {
    await exportDxfInternal(true);
  }

  async function exportDxfInternal(withOffset: boolean): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    const file = currentFile.value;
    if (!sid || !fid || !file) {
      setError('開いているDXFがありません');
      return;
    }
    setError(null);
    try {
      const blob = await api.exportDxf(sid, fid, withOffset);
      const base = file.name.replace(/\.[Dd][Xx][Ff]$/, '');
      const suffix = withOffset ? '_offset' : '_clean';
      downloadBlob(blob, `${base}${suffix}.dxf`);
    } catch (err) {
      setError(err instanceof Error ? err.message : '書き出しに失敗しました');
    }
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

  return {
    // state
    currentSession: _currentSession,
    currentFileId: _currentFileId,
    currentFile,
    visibleEntities,
    files: _files,
    selectedForDelete: _selectedForDelete,
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
    setPdfMaterial,
    exportPdf,
    clearError: () => setError(null),
    // picker plumbing (M4)
    registerFilePicker,
    registerFolderPicker,
    openFilePicker,
    openFolderPicker,
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
