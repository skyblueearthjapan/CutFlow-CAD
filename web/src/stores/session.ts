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
  DeleteCategoryKey,
  DeleteCategoryRow,
  Entity,
  FileData,
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

  /** Switch the active tab — fetches the file on first visit. */
  async function selectFile(fid: string): Promise<void> {
    _currentFileId.value = fid;
    _selectedForDelete.value = new Set();
    if (!_files.value.has(fid)) await loadFile(fid);
  }

  /** Force-fetch (or re-fetch) the parsed entity payload for a file. */
  async function loadFile(fid: string): Promise<void> {
    const sid = _currentSession.value?.session_id;
    if (!sid) return;
    setError(null);
    _isLoadingFile.value = true;
    try {
      const data = await api.getFile(sid, fid);
      // shallowRef requires assigning a new Map ref to trigger updates.
      const next = new Map(_files.value);
      next.set(fid, data);
      _files.value = next;
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
      await loadFile(fid);
      _selectedForDelete.value = new Set();
    } catch (err) {
      setError(err instanceof Error ? err.message : '削除に失敗しました');
    } finally {
      _isDeleting.value = false;
    }
  }

  /** Download the cleaned DXF for the active file. */
  async function exportDxf(): Promise<void> {
    const sid = _currentSession.value?.session_id;
    const fid = _currentFileId.value;
    const file = currentFile.value;
    if (!sid || !fid || !file) {
      setError('開いているDXFがありません');
      return;
    }
    setError(null);
    try {
      const blob = await api.exportDxf(sid, fid);
      const base = file.name.replace(/\.[Dd][Xx][Ff]$/, '');
      downloadBlob(blob, `${base}_clean.dxf`);
    } catch (err) {
      setError(err instanceof Error ? err.message : '書き出しに失敗しました');
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
    // derived
    deleteRows,
    totalDeleteCandidates,
    remainingAfterDelete,
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
