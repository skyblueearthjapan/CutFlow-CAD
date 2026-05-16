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
  DeleteResult,
  FileData,
  Session,
} from '../types/dxf';
import {
  mockUploadFiles,
  mockGetFile,
  mockDeleteEntities,
  mockExportDxf,
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

/** FastAPI returns JSON `{ "detail": "..." }` for HTTPExceptions. Pull that
 *  out when present so the UI banner shows the meaningful Japanese error
 *  instead of the generic status text (M6). */
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

/** GET .../export?format=dxf — cleaned DXF download (binary). */
export async function exportDxf(sid: string, fid: string): Promise<Blob> {
  if (!(await probeBackend())) return mockExportDxf(sid, fid);
  const res = await fetch(
    `${API_BASE}/api/session/${sid}/file/${fid}/export?format=dxf`,
  );
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(res.status, body, buildErrorMessage(res.status, body, res.statusText));
  }
  return res.blob();
}

export { ApiError };
