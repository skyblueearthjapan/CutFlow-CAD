/**
 * Thin fetch wrapper around the CutFlow•CAD FastAPI backend.
 *
 * Endpoints are documented in docs/ARCHITECTURE.md §3 and the Phase 1 task
 * brief. Base URL defaults to http://localhost:8080 and can be overridden
 * via VITE_API_BASE.
 *
 * Backend availability: when the API does not respond to /api/health (Phase 1
 * is being built concurrently), the service auto-falls back to the in-browser
 * mock implementation in mockSession.ts. This lets the UI be developed end-to-
 * end without blocking on the backend. Once the API is live the real path is
 * used automatically — no flag flip required.
 */

import type {
  ChamferGeometry,
  ChamferSpec,
  CleanupFrameResult,
  CornerInfo,
  DeleteResult,
  EdgeInfo,
  FileData,
  OffsetRequest,
  OffsetResult,
  OuterDetectionResult,
  PdfExportOptions,
  Session,
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
} from './mockSession';

const API_BASE = (import.meta.env.VITE_API_BASE ?? 'http://localhost:8080').replace(/\/$/, '');
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
      const res = await fetch(`${API_BASE}/api/upload`, { method: 'OPTIONS' });
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
  const res = await fetch(`${API_BASE}/api/upload`, { method: 'POST', body: fd });
  return jsonOrThrow<Session>(res);
}

/** GET /api/session/{sid}/file/{fid} — full parsed entity payload. */
export async function getFile(sid: string, fid: string): Promise<FileData> {
  if (!(await probeBackend())) return mockGetFile(sid, fid);
  const res = await fetch(`${API_BASE}/api/session/${sid}/file/${fid}`);
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
    `${API_BASE}/api/session/${sid}/file/${fid}/delete`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_ids: ids }),
    },
  );
  return jsonOrThrow<DeleteResult>(res);
}

/** GET .../export?format=dxf — cleaned DXF download (binary).
 *  When `withOffset` is true the backend embeds the computed offset loop
 *  alongside the cleaned geometry (Phase 2). */
export async function exportDxf(
  sid: string,
  fid: string,
  withOffset = false,
): Promise<Blob> {
  if (!(await probeBackend())) return mockExportDxf(sid, fid, withOffset);
  const params = new URLSearchParams({ format: 'dxf' });
  if (withOffset) params.set('with_offset', 'true');
  const res = await fetch(
    `${API_BASE}/api/session/${sid}/file/${fid}/export?${params.toString()}`,
  );
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
    `${API_BASE}/api/session/${sid}/file/${fid}/detect-outer`,
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
    `${API_BASE}/api/session/${sid}/file/${fid}/outer-manual`,
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
    `${API_BASE}/api/session/${sid}/file/${fid}/offset`,
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
  const res = await fetch(`${API_BASE}/api/session/${sid}/file/${fid}/outer`);
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
  const res = await fetch(`${API_BASE}/api/session/${sid}/file/${fid}/offset`);
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
  const res = await fetch(`${API_BASE}/api/session/${sid}/file/${fid}/corners`);
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
    `${API_BASE}/api/session/${sid}/file/${fid}/chamfer`,
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
  const res = await fetch(`${API_BASE}/api/session/${sid}/file/${fid}/chamfer`);
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
    `${API_BASE}/api/session/${sid}/file/${fid}/cleanup-frame`,
    { method: 'POST' },
  );
  return jsonOrThrow<CleanupFrameResult>(res);
}

/** GET .../export?format=pdf — material-take PDF download (binary).
 *  ``opts.material`` (H4) is forwarded as a query param so the backend can
 *  render it in the PDF header band. */
export async function exportPdf(
  sid: string,
  fid: string,
  opts: PdfExportOptions,
): Promise<Blob> {
  if (!(await probeBackend())) return mockExportPdf(sid, fid, opts);
  const params = new URLSearchParams({ format: 'pdf' });
  params.set('with_offset', opts.with_offset ? 'true' : 'false');
  params.set('with_chamfer', opts.with_chamfer ? 'true' : 'false');
  params.set('with_frame', opts.frame);
  if (opts.material) params.set('material', opts.material);
  const res = await fetch(
    `${API_BASE}/api/session/${sid}/file/${fid}/export?${params.toString()}`,
  );
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(res.status, body, buildErrorMessage(res.status, body, res.statusText));
  }
  return res.blob();
}

export { ApiError };
