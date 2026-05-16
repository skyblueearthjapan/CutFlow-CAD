/**
 * Thin fetch wrapper around the CutFlow•CAD FastAPI backend.
 *
 * Endpoints are documented in docs/ARCHITECTURE.md §3 and the Phase 1 task
 * brief. Base URL defaults to ``/api`` (same-origin) so the same build runs
 * behind Tailscale Funnel / nginx / Caddy without rebuilding — override via
 * ``VITE_API_BASE`` only when calling a cross-origin backend.
 *
 * Backend availability: when the API does not respond to /api/health (Phase 1
 * is being built concurrently), the service auto-falls back to the in-browser
 * mock implementation in mockSession.ts. This lets the UI be developed end-to-
 * end without blocking on the backend. Once the API is live the real path is
 * used automatically — no flag flip required.
 */

import type {
  AddedHole,
  Bridge,
  ChamferGeometry,
  ChamferSpec,
  CleanupFrameResult,
  CornerInfo,
  DeleteResult,
  Dimension,
  EdgeInfo,
  EditedVertex,
  FileData,
  HolePatternRequest,
  Note,
  OffsetRequest,
  OffsetResult,
  OuterDetectionResult,
  PdfExportOptions,
  Session,
  SnapKind,
  SnapResult,
} from '../types/dxf';
import {
  mockUploadFiles,
  mockGetFile,
  mockDeleteEntities,
  mockExportDxf,
  mockDetectOuter,
  mockConfirmOuterManual,
  mockComputeOffset,
  mockGetCorners,
  mockSetChamfer,
  mockGetChamfer,
  mockCleanupFrame,
  mockExportPdf,
  mockListDimensions,
  mockRemoveDimension,
  mockEditVertex,
  mockSnap,
  mockListHoles,
  mockAddHole,
  mockAddHolePattern,
  mockRemoveHole,
  mockListNotes,
  mockRemoveNote,
  mockListBridges,
  mockAddBridge,
  mockAddBridgeAuto,
  mockRemoveBridge,
} from './mockSession';

const API_BASE = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/$/, '');
const FORCE_MOCK = import.meta.env.VITE_USE_MOCK === 'true';

/** Cached probe result — undefined = not probed yet. */
let _liveProbe: Promise<boolean> | undefined;

/** Check once per session whether the backend exposes the Phase 1 endpoints. */
function probeBackend(): Promise<boolean> {
  if (FORCE_MOCK) return Promise.resolve(false);
  if (_liveProbe) return _liveProbe;
  _liveProbe = (async () => {
    try {
      // The Phase 0 backend only exposes /api/health; we treat the Phase 1
      // endpoints as available only when /api/upload responds to OPTIONS
      // (FastAPI replies 200 to CORS preflight once the route exists).
      const url = API_BASE.endsWith('/api')
        ? `${API_BASE}/upload`
        : `${API_BASE}/api/upload`;
      const res = await fetch(url, { method: 'OPTIONS' });
      // 200/204 → route registered, 404/405 → not yet implemented.
      return res.ok || res.status === 405;
    } catch {
      return false;
    }
  })();
  return _liveProbe;
}

/** Surface for the rest of the app to know which path was taken. */
export async function isLiveBackend(): Promise<boolean> {
  return probeBackend();
}

/** Build a full URL — keeps the ``/api`` segment from being duplicated when
 *  ``API_BASE`` already terminates in ``/api`` (the default for same-origin
 *  proxy setups). */
function url(path: string): string {
  if (API_BASE.endsWith('/api')) {
    return `${API_BASE}${path.replace(/^\/api/, '')}`;
  }
  return `${API_BASE}${path}`;
}

/* ----------------------------- HTTP helpers ------------------------------ */

class ApiError extends Error {
  constructor(public status: number, public body: string, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

/** FastAPI returns JSON `{ "detail": ... }` for HTTPExceptions. Pull that
 *  out when present so the UI banner shows the meaningful Japanese error
 *  instead of the generic status text.
 *
 *  ``detail`` may be a string (most endpoints) OR a dict (outer-manual
 *  422 returns ``{message, warnings}``). Both shapes are unwrapped to
 *  something readable. (M2) */
function buildErrorMessage(status: number, body: string, statusText: string): string {
  if (body) {
    try {
      const parsed = JSON.parse(body);
      const detail = parsed?.detail;
      if (typeof detail === 'string' && detail) {
        return detail;
      }
      if (Array.isArray(detail) && detail.length > 0 && detail[0]?.msg) {
        return detail[0].msg;
      }
      if (detail && typeof detail === 'object') {
        const msg = (detail as { message?: unknown }).message;
        if (typeof msg === 'string' && msg) return msg;
        const warnings = (detail as { warnings?: unknown }).warnings;
        if (Array.isArray(warnings) && warnings.length > 0 && typeof warnings[0] === 'string') {
          return warnings[0];
        }
        try {
          return JSON.stringify(detail);
        } catch {
          // fall through
        }
      }
    } catch {
      // Not JSON; fall through to the default message.
    }
  }
  return `API ${status}: ${statusText}`;
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(res.status, body, buildErrorMessage(res.status, body, res.statusText));
  }
  return res.json() as Promise<T>;
}

/* ------------------------------ Endpoints -------------------------------- */

/** POST /api/upload — multipart upload of one or more DXF files. */
export async function uploadFiles(files: File[]): Promise<Session> {
  if (!(await probeBackend())) return mockUploadFiles(files);
  const fd = new FormData();
  for (const f of files) fd.append('files', f, f.name);
  const res = await fetch(url('/api/upload'), { method: 'POST', body: fd });
  return jsonOrThrow<Session>(res);
}

/** GET /api/session/{sid}/file/{fid} — full parsed entity payload. */
export async function getFile(sid: string, fid: string): Promise<FileData> {
  if (!(await probeBackend())) return mockGetFile(sid, fid);
  const res = await fetch(url(`/api/session/${sid}/file/${fid}`));
  return jsonOrThrow<FileData>(res);
}

/** POST /api/session/{sid}/file/{fid}/delete — remove the given entities. */
export async function deleteEntities(
  sid: string,
  fid: string,
  ids: string[],
): Promise<DeleteResult> {
  if (!(await probeBackend())) return mockDeleteEntities(sid, fid, ids);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/delete`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_ids: ids }),
    },
  );
  return jsonOrThrow<DeleteResult>(res);
}

/** Common builder for the Phase 4 export query string. */
export interface ExportOverlayFlags {
  with_offset?: boolean;
  with_chamfer?: boolean;
  with_dimensions?: boolean;
  with_added_holes?: boolean;
  with_notes?: boolean;
  with_bridges?: boolean;
  with_edits?: boolean;
  with_frame?: 'auto' | 'none' | 'cutflow';
  material?: string;
  format?: 'dxf' | 'pdf';
}

function buildExportQuery(flags: ExportOverlayFlags): string {
  const params = new URLSearchParams({ format: flags.format ?? 'dxf' });
  if (flags.with_offset) params.set('with_offset', 'true');
  if (flags.with_chamfer) params.set('with_chamfer', 'true');
  if (flags.with_dimensions) params.set('with_dimensions', 'true');
  if (flags.with_added_holes) params.set('with_added_holes', 'true');
  if (flags.with_notes) params.set('with_notes', 'true');
  if (flags.with_bridges) params.set('with_bridges', 'true');
  if (flags.with_edits) params.set('with_edits', 'true');
  if (flags.with_frame) params.set('with_frame', flags.with_frame);
  if (flags.material) params.set('material', flags.material);
  return params.toString();
}

/** GET .../export?format=dxf — cleaned DXF download (binary).
 *  C3: every Phase 4 overlay query flag is forwarded so a tab that has
 *  pending dim/hole/note/bridge work can choose what to bake into the
 *  export. */
export async function exportDxf(
  sid: string,
  fid: string,
  flags: ExportOverlayFlags = {},
): Promise<Blob> {
  if (!(await probeBackend())) return mockExportDxf(sid, fid, !!flags.with_offset);
  const qs = buildExportQuery({ ...flags, format: 'dxf' });
  const res = await fetch(url(`/api/session/${sid}/file/${fid}/export?${qs}`));
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(res.status, body, buildErrorMessage(res.status, body, res.statusText));
  }
  return res.blob();
}

/* -------------------- Phase 2: outer detection / offset ------------------- */

/** POST .../detect-outer — automatic outer-loop detection. */
export async function detectOuter(
  sid: string,
  fid: string,
): Promise<OuterDetectionResult> {
  if (!(await probeBackend())) return mockDetectOuter(sid, fid);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/detect-outer`),
    { method: 'POST' },
  );
  return jsonOrThrow<OuterDetectionResult>(res);
}

/** POST .../outer-manual — confirm a manually-chained outer loop.
 *  Backend returns 422 (with detail) when the chain does not close. */
export async function confirmOuterManual(
  sid: string,
  fid: string,
  entityIds: string[],
): Promise<OuterDetectionResult> {
  if (!(await probeBackend())) return mockConfirmOuterManual(sid, fid, entityIds);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/outer-manual`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_ids: entityIds }),
    },
  );
  return jsonOrThrow<OuterDetectionResult>(res);
}

/** POST .../offset — recompute the offset preview with the given params.
 *  Honours the optional ``signal`` so the caller can cancel an in-flight
 *  request when newer params arrive (M5). */
export async function computeOffset(
  sid: string,
  fid: string,
  req: OffsetRequest,
  signal?: AbortSignal,
): Promise<OffsetResult> {
  if (!(await probeBackend())) return mockComputeOffset(sid, fid, req);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/offset`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
      signal,
    },
  );
  return jsonOrThrow<OffsetResult>(res);
}

/** GET .../outer — rehydrate the persisted outer-detection result.
 *  Returns ``null`` for 404 (no outer detected yet) so the caller can
 *  treat absence as "needs detection" without try/catch noise (M3). */
export async function getOuter(
  sid: string,
  fid: string,
): Promise<OuterDetectionResult | null> {
  if (!(await probeBackend())) return null;
  const res = await fetch(url(`/api/session/${sid}/file/${fid}/outer`));
  if (res.status === 404) return null;
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(res.status, body, buildErrorMessage(res.status, body, res.statusText));
  }
  // The persisted shape is the minimal payload written by /detect-outer,
  // not a full OuterDetectionResult. Adapt the fields the UI needs.
  const raw = (await res.json()) as {
    loop?: string[];
    confidence?: number;
    method?: string;
    perimeter?: number;
    area?: number;
    status?: 'success' | 'low_confidence' | 'failed';
  };
  const loop = raw.loop ?? [];
  return {
    status: raw.status ?? (loop.length > 0 ? 'success' : 'failed'),
    confidence: raw.confidence ?? 0,
    outer_loop: loop,
    loop_summary: {
      closed: loop.length > 0,
      segments: loop.length,
      lines: 0,
      arcs: 0,
      perimeter: raw.perimeter ?? 0,
      area: raw.area ?? 0,
      bounding_box: { min_x: 0, min_y: 0, max_x: 0, max_y: 0 },
    },
    warnings: [],
    candidates: [],
  };
}

/** GET .../offset — rehydrate the persisted offset preview (or null). */
export async function getOffset(
  sid: string,
  fid: string,
): Promise<OffsetResult | null> {
  if (!(await probeBackend())) return null;
  const res = await fetch(url(`/api/session/${sid}/file/${fid}/offset`));
  if (res.status === 404) return null;
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(res.status, body, buildErrorMessage(res.status, body, res.statusText));
  }
  const raw = (await res.json()) as { result?: OffsetResult };
  return raw.result ?? null;
}

/* -------------------- Phase 3: chamfer / PDF / cleanup-frame -------------- */

/** GET .../corners — outer-loop corners + edges (C1..Cn / E1..En). The
 *  backend always returns both arrays so the chamfer UI can populate the
 *  「角」and「辺」sections in one fetch. */
export async function getCorners(
  sid: string,
  fid: string,
): Promise<{ corners: CornerInfo[]; edges: EdgeInfo[] }> {
  if (!(await probeBackend())) return mockGetCorners(sid, fid);
  const res = await fetch(url(`/api/session/${sid}/file/${fid}/corners`));
  const data = await jsonOrThrow<{ corners: CornerInfo[]; edges: EdgeInfo[] }>(res);
  return { corners: data.corners ?? [], edges: data.edges ?? [] };
}

/** POST .../chamfer — replace the chamfer spec list and get geometry back. */
export async function setChamfer(
  sid: string,
  fid: string,
  specs: ChamferSpec[],
): Promise<{ specs: ChamferSpec[]; geometry: ChamferGeometry }> {
  if (!(await probeBackend())) return mockSetChamfer(sid, fid, specs);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/chamfer`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ specs }),
    },
  );
  return jsonOrThrow<{ specs: ChamferSpec[]; geometry: ChamferGeometry }>(res);
}

/** GET .../chamfer — rehydrate the persisted chamfer spec + geometry. */
export async function getChamfer(
  sid: string,
  fid: string,
): Promise<{ specs: ChamferSpec[]; geometry: ChamferGeometry } | null> {
  if (!(await probeBackend())) return mockGetChamfer(sid, fid);
  const res = await fetch(url(`/api/session/${sid}/file/${fid}/chamfer`));
  if (res.status === 404) return null;
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(res.status, body, buildErrorMessage(res.status, body, res.statusText));
  }
  return res.json() as Promise<{ specs: ChamferSpec[]; geometry: ChamferGeometry }>;
}

/** POST .../cleanup-frame — reserve the production-frame entities for deletion. */
export async function cleanupFrame(
  sid: string,
  fid: string,
): Promise<CleanupFrameResult> {
  if (!(await probeBackend())) return mockCleanupFrame(sid, fid);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/cleanup-frame`),
    { method: 'POST' },
  );
  return jsonOrThrow<CleanupFrameResult>(res);
}

/** GET .../export?format=pdf — material-take PDF download (binary).
 *  ``opts.material`` (H4) is forwarded as a query param so the backend can
 *  render it in the PDF header band. C3: PDF export also forwards every
 *  Phase 4 ``with_*`` overlay flag so the operator can choose what to bake
 *  into the printable. */
export async function exportPdf(
  sid: string,
  fid: string,
  opts: PdfExportOptions,
): Promise<Blob> {
  if (!(await probeBackend())) return mockExportPdf(sid, fid, opts);
  const qs = buildExportQuery({
    format: 'pdf',
    with_offset: opts.with_offset,
    with_chamfer: opts.with_chamfer,
    with_dimensions: opts.with_dimensions,
    with_added_holes: opts.with_added_holes,
    with_notes: opts.with_notes,
    with_bridges: opts.with_bridges,
    with_edits: opts.with_edits,
    with_frame: opts.frame,
    material: opts.material,
  });
  const res = await fetch(url(`/api/session/${sid}/file/${fid}/export?${qs}`));
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(res.status, body, buildErrorMessage(res.status, body, res.statusText));
  }
  return res.blob();
}

/* -------------------- Phase 4: dim / edit / hole / note / bridge ---------- */

/** GET .../dimensions — list user-added dimensions for the file.
 *  Backend wraps the list in ``{dimensions: [...]}`` (C1). */
export async function listDimensions(sid: string, fid: string): Promise<Dimension[]> {
  if (!(await probeBackend())) return mockListDimensions(sid, fid);
  const res = await fetch(url(`/api/session/${sid}/file/${fid}/dimensions`));
  const data = await jsonOrThrow<{ dimensions: Dimension[] }>(res);
  return data.dimensions ?? [];
}

/** POST .../dimensions — last-write-wins replace of the full list. Returns
 *  the merged list back from the server. */
export async function setDimensions(
  sid: string,
  fid: string,
  dimensions: Dimension[],
): Promise<Dimension[]> {
  if (!(await probeBackend())) return mockSetDimensions(sid, fid, dimensions);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/dimensions`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dimensions }),
    },
  );
  const data = await jsonOrThrow<{ dimensions: Dimension[] }>(res);
  return data.dimensions ?? [];
}

/** Add a single dimension on top of whatever is already persisted by
 *  fetching → appending → posting. The backend is last-write-wins so we
 *  cannot send the bare new dim. */
export async function addDimension(
  sid: string,
  fid: string,
  dim: Dimension,
): Promise<Dimension[]> {
  const existing = await listDimensions(sid, fid);
  return setDimensions(sid, fid, [...existing, dim]);
}

/** DELETE .../dimensions/{id}. */
export async function removeDimension(
  sid: string,
  fid: string,
  id: string,
): Promise<void> {
  if (!(await probeBackend())) return mockRemoveDimension(sid, fid, id);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/dimensions/${id}`),
    { method: 'DELETE' },
  );
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(res.status, body, buildErrorMessage(res.status, body, res.statusText));
  }
}

/** POST .../edit-vertex — record one or more vertex translations.
 *  Backend wraps the list in ``{edits: [...]}`` (C1). */
export async function editVertex(
  sid: string,
  fid: string,
  edit: EditedVertex,
): Promise<EditedVertex[]> {
  if (!(await probeBackend())) return mockEditVertex(sid, fid, edit);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/edit-vertex`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edits: [edit] }),
    },
  );
  const data = await jsonOrThrow<{ edits: EditedVertex[] }>(res);
  return data.edits ?? [];
}

/** GET .../edits — list user vertex edits for the file. */
export async function listEdits(sid: string, fid: string): Promise<EditedVertex[]> {
  if (!(await probeBackend())) return [];
  const res = await fetch(url(`/api/session/${sid}/file/${fid}/edits`));
  if (res.status === 404) return [];
  const data = await jsonOrThrow<{ edits: EditedVertex[] }>(res);
  return data.edits ?? [];
}

/** POST .../snap — resolve a cursor position to the nearest snap target.
 *  Body contract (C1): ``{position, snap_types, tolerance}``. */
export async function getSnapPoint(
  sid: string,
  fid: string,
  position: [number, number],
  options: {
    snap_types?: SnapKind[];
    tolerance?: number;
  } = {},
): Promise<SnapResult> {
  if (!(await probeBackend())) return mockSnap(sid, fid, position, options.tolerance);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/snap`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        position,
        snap_types: options.snap_types ?? [
          'endpoint',
          'midpoint',
          'intersection',
          'center',
        ],
        tolerance: options.tolerance ?? 6,
      }),
    },
  );
  return jsonOrThrow<SnapResult>(res);
}

/** GET .../holes — list user-added holes. */
export async function listHoles(sid: string, fid: string): Promise<AddedHole[]> {
  if (!(await probeBackend())) return mockListHoles(sid, fid);
  const res = await fetch(url(`/api/session/${sid}/file/${fid}/holes`));
  const data = await jsonOrThrow<{ holes: AddedHole[] }>(res);
  return data.holes ?? [];
}

/** POST .../holes — append (and de-duplicate by id) a single hole. */
export async function addHole(
  sid: string,
  fid: string,
  hole: AddedHole,
): Promise<AddedHole[]> {
  if (!(await probeBackend())) return mockAddHole(sid, fid, hole);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/holes`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ holes: [hole] }),
    },
  );
  const data = await jsonOrThrow<{ holes: AddedHole[] }>(res);
  return data.holes ?? [];
}

/** POST .../holes/pattern — expand a rows×cols grid into holes. */
export async function addHolePattern(
  sid: string,
  fid: string,
  req: HolePatternRequest,
): Promise<AddedHole[]> {
  if (!(await probeBackend())) return mockAddHolePattern(sid, fid, req);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/holes/pattern`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    },
  );
  const data = await jsonOrThrow<{ holes: AddedHole[] }>(res);
  return data.holes ?? [];
}

/** DELETE .../holes/{id}. */
export async function removeHole(
  sid: string,
  fid: string,
  id: string,
): Promise<void> {
  if (!(await probeBackend())) return mockRemoveHole(sid, fid, id);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/holes/${id}`),
    { method: 'DELETE' },
  );
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(res.status, body, buildErrorMessage(res.status, body, res.statusText));
  }
}

/** GET .../notes — list user-added notes. */
export async function listNotes(sid: string, fid: string): Promise<Note[]> {
  if (!(await probeBackend())) return mockListNotes(sid, fid);
  const res = await fetch(url(`/api/session/${sid}/file/${fid}/notes`));
  const data = await jsonOrThrow<{ notes: Note[] }>(res);
  return data.notes ?? [];
}

/** POST .../notes — last-write-wins replace of the full list. */
export async function setNotes(
  sid: string,
  fid: string,
  notes: Note[],
): Promise<Note[]> {
  if (!(await probeBackend())) return mockSetNotes(sid, fid, notes);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/notes`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes }),
    },
  );
  const data = await jsonOrThrow<{ notes: Note[] }>(res);
  return data.notes ?? [];
}

/** Append a single note on top of the persisted list. */
export async function addNote(
  sid: string,
  fid: string,
  note: Note,
): Promise<Note[]> {
  const existing = await listNotes(sid, fid);
  return setNotes(sid, fid, [...existing, note]);
}

/** DELETE .../notes/{id}. */
export async function removeNote(
  sid: string,
  fid: string,
  id: string,
): Promise<void> {
  if (!(await probeBackend())) return mockRemoveNote(sid, fid, id);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/notes/${id}`),
    { method: 'DELETE' },
  );
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(res.status, body, buildErrorMessage(res.status, body, res.statusText));
  }
}

/** GET .../bridges — list user-added bridges. */
export async function listBridges(sid: string, fid: string): Promise<Bridge[]> {
  if (!(await probeBackend())) return mockListBridges(sid, fid);
  const res = await fetch(url(`/api/session/${sid}/file/${fid}/bridges`));
  const data = await jsonOrThrow<{ bridges: Bridge[] }>(res);
  return data.bridges ?? [];
}

/** POST .../bridges — last-write-wins replace of the bridge list. */
export async function setBridges(
  sid: string,
  fid: string,
  bridges: Bridge[],
): Promise<Bridge[]> {
  if (!(await probeBackend())) return mockSetBridges(sid, fid, bridges);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/bridges`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bridges }),
    },
  );
  const data = await jsonOrThrow<{ bridges: Bridge[] }>(res);
  return data.bridges ?? [];
}

/** Append a single bridge on top of the persisted list. */
export async function addBridge(
  sid: string,
  fid: string,
  bridge: Bridge,
): Promise<Bridge[]> {
  if (!(await probeBackend())) return mockAddBridge(sid, fid, bridge);
  const existing = await listBridges(sid, fid);
  return setBridges(sid, fid, [...existing, bridge]);
}

/** POST .../bridges/auto — auto-distribute N bridges around the outer.
 *  Returns the full bridge list (auto replaces the previous set). */
export async function addBridgeAuto(
  sid: string,
  fid: string,
  count: number,
  width_mm: number,
): Promise<Bridge[]> {
  if (!(await probeBackend())) return mockAddBridgeAuto(sid, fid, count, width_mm);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/bridges/auto`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ count, width_mm }),
    },
  );
  const data = await jsonOrThrow<{ bridges: Bridge[] }>(res);
  return data.bridges ?? [];
}

/** DELETE .../bridges/{id}. */
export async function removeBridge(
  sid: string,
  fid: string,
  id: string,
): Promise<void> {
  if (!(await probeBackend())) return mockRemoveBridge(sid, fid, id);
  const res = await fetch(
    url(`/api/session/${sid}/file/${fid}/bridges/${id}`),
    { method: 'DELETE' },
  );
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(res.status, body, buildErrorMessage(res.status, body, res.statusText));
  }
}

/* Re-exports — keep the symbol available even when the mock is missing
 * a couple of last-write-wins helpers we just routed through it. */
import { mockSetDimensions, mockSetNotes, mockSetBridges } from './mockSession';

export { ApiError };
