/**
 * In-browser mock for the Phase 1 backend.
 *
 * Used by services/api.ts when the FastAPI backend's Phase 1 endpoints have
 * not yet shipped (the Phase 0 backend only exposes /api/health). Produces a
 * synthetic but plausible entity set per uploaded DXF so the full UI flow
 * (upload → render → delete → re-render → export) can be exercised end-to-
 * end without the backend.
 *
 * Mock storage lives in module scope (cleared on page reload).
 */

import type {
  DeleteCandidates,
  DeleteResult,
  Entity,
  FileData,
  Session,
  SessionFile,
} from '../types/dxf';

interface MockBundle {
  session: Session;
  files: Map<string, FileData>;
}

const _store = new Map<string, MockBundle>();

function uid(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 9)}`;
}

/** Geometry approximating the v3 mockup canvas, in DXF (Y-up) coords. */
function buildSampleEntities(seed: number): {
  entities: Entity[];
  bbox: { min_x: number; min_y: number; max_x: number; max_y: number };
  candidates: DeleteCandidates;
} {
  // Slightly perturb each file so multiple tabs look different
  const wobble = (seed % 5) * 8;
  const W = 660 + wobble;
  const H = 400 + wobble;
  const X0 = 0;
  const Y0 = 0;

  const entities: Entity[] = [];
  const dimIds: string[] = [];
  const balloonIds: string[] = [];
  const tapIds: string[] = [];
  const frameIds: string[] = [];

  // Outer rectangle with rounded corners → four LINEs + four ARCs.
  const r = 20;
  const outerLines: Array<[number, number, number, number]> = [
    [X0 + r, Y0, X0 + W - r, Y0],                 // bottom
    [X0 + W, Y0 + r, X0 + W, Y0 + H - r],         // right
    [X0 + W - r, Y0 + H, X0 + r, Y0 + H],         // top
    [X0, Y0 + H - r, X0, Y0 + r],                 // left
  ];
  outerLines.forEach((coords, i) => {
    entities.push({
      id: `outer_l${i}`,
      type: 'LINE',
      category: 'outer',
      color: 256,
      layer: '0',
      geom: { x1: coords[0], y1: coords[1], x2: coords[2], y2: coords[3] },
    });
  });

  const outerArcs: Array<[number, number, number, number]> = [
    [X0 + W - r, Y0 + r, 270, 360],   // BR
    [X0 + W - r, Y0 + H - r, 0, 90],  // TR
    [X0 + r, Y0 + H - r, 90, 180],    // TL
    [X0 + r, Y0 + r, 180, 270],       // BL
  ];
  outerArcs.forEach((a, i) => {
    entities.push({
      id: `outer_a${i}`,
      type: 'ARC',
      category: 'outer',
      color: 256,
      layer: '0',
      geom: { cx: a[0], cy: a[1], r, start_angle: a[2], end_angle: a[3] },
    });
  });

  // Holes
  const holes: Array<[number, number, number]> = [
    [X0 + 90, Y0 + 90, 9],
    [X0 + W - 90, Y0 + 90, 9],
    [X0 + 90, Y0 + H - 90, 9],
    [X0 + W - 90, Y0 + H - 90, 9],
    [X0 + W / 2, Y0 + H / 2, 40],
  ];
  holes.forEach((h, i) => {
    entities.push({
      id: `hole_${i}`,
      type: 'CIRCLE',
      category: 'hole',
      color: 256,
      layer: '0',
      geom: { cx: h[0], cy: h[1], r: h[2] },
    });
  });

  // Tap marks (small circles, amber)
  const taps: Array<[number, number]> = [
    [X0 + 220, Y0 + 50],
    [X0 + W - 220, Y0 + 50],
    [X0 + 220, Y0 + H - 50],
    [X0 + W - 220, Y0 + H - 50],
  ];
  taps.forEach((t, i) => {
    const id = `tap_${i}`;
    entities.push({
      id,
      type: 'CIRCLE',
      category: 'tap',
      color: 2,
      layer: '0',
      geom: { cx: t[0], cy: t[1], r: 4 },
    });
    tapIds.push(id);
  });

  // Dimension lines (simplified as LINE entities classified as 'dim')
  const dims: Array<{ a: [number, number]; b: [number, number]; text: string }> = [
    { a: [X0, Y0 - 30], b: [X0 + W, Y0 - 30], text: String(W) },
    { a: [X0 - 30, Y0], b: [X0 - 30, Y0 + H], text: String(H) },
    { a: [X0 + 90, Y0 + H + 30], b: [X0 + W - 90, Y0 + H + 30], text: String(W - 180) },
  ];
  dims.forEach((d, i) => {
    const id = `dim_${i}`;
    entities.push({
      id,
      type: 'DIMENSION',
      category: 'dim',
      color: 2,
      layer: '0',
      geom: { anchors: [d.a, d.b], text: d.text },
    });
    dimIds.push(id);
  });

  // Balloons (INSERT-style: small circle + numeric TEXT)
  const balloons: Array<{ cx: number; cy: number; n: number }> = [
    { cx: X0 - 40, cy: Y0 + H + 50, n: 1 },
    { cx: X0 + W + 40, cy: Y0 + H - 40, n: 2 },
  ];
  balloons.forEach((b, i) => {
    const id = `balloon_${i}`;
    entities.push({
      id,
      type: 'INSERT',
      category: 'balloon',
      color: 2,
      layer: '0',
      geom: { x: b.cx, y: b.cy, name: 'BALLOON', rotation: 0, text: String(b.n), radius: 10 },
    });
    balloonIds.push(id);
  });

  // Frame: title block rectangle
  const fId = 'frame_0';
  entities.push({
    id: fId,
    type: 'LWPOLYLINE',
    category: 'frame',
    color: 2,
    layer: '0',
    geom: {
      vertices: [
        [X0 - 80, Y0 - 70],
        [X0 + W + 80, Y0 - 70],
        [X0 + W + 80, Y0 + H + 70],
        [X0 - 80, Y0 + H + 70],
      ],
      closed: true,
    },
  });
  frameIds.push(fId);

  const bbox = {
    min_x: X0 - 90,
    min_y: Y0 - 90,
    max_x: X0 + W + 90,
    max_y: Y0 + H + 90,
  };

  return {
    entities,
    bbox,
    candidates: {
      DIMENSION: dimIds,
      BALLOON: balloonIds,
      TAP: tapIds,
      FRAME: frameIds,
    },
  };
}

function buildFileData(name: string, seed: number): FileData {
  const { entities, bbox, candidates } = buildSampleEntities(seed);
  const byCat: Record<string, number> = {};
  for (const e of entities) byCat[e.category] = (byCat[e.category] ?? 0) + 1;
  return {
    file_id: uid('f'),
    name,
    bounding_box: bbox,
    entities,
    delete_candidates: candidates,
    stats: { total: entities.length, by_category: byCat },
    // Mirror the live backend shape (C2): server returns the per-file
    // delete reservation here, which the canvas uses to hide entities.
    deleted_ids: [],
  };
}

/* ------------------------------- Public API ------------------------------ */

export async function mockUploadFiles(files: File[]): Promise<Session> {
  // Tiny artificial delay so the UI exercises its loading states.
  await new Promise((r) => setTimeout(r, 250));

  const sid = uid('sid');
  const sessionFiles: SessionFile[] = [];
  const fileMap = new Map<string, FileData>();
  files.forEach((f, i) => {
    const data = buildFileData(f.name, i);
    sessionFiles.push({ file_id: data.file_id, name: f.name, size: f.size, status: 'ready' });
    fileMap.set(data.file_id, data);
  });

  const session: Session = {
    session_id: sid,
    files: sessionFiles,
    expires_at: new Date(Date.now() + 24 * 3600 * 1000).toISOString(),
  };
  _store.set(sid, { session, files: fileMap });
  return session;
}

export async function mockGetFile(sid: string, fid: string): Promise<FileData> {
  const bundle = _store.get(sid);
  if (!bundle) throw new Error(`mock: unknown session ${sid}`);
  const file = bundle.files.get(fid);
  if (!file) throw new Error(`mock: unknown file ${fid}`);
  // Return a structural clone so callers can mutate without polluting the store.
  return JSON.parse(JSON.stringify(file));
}

export async function mockDeleteEntities(
  sid: string,
  fid: string,
  ids: string[],
): Promise<DeleteResult> {
  const bundle = _store.get(sid);
  if (!bundle) throw new Error(`mock: unknown session ${sid}`);
  const file = bundle.files.get(fid);
  if (!file) throw new Error(`mock: unknown file ${fid}`);

  // Match live backend (C2): keep raw entities in the payload, but accumulate
  // the merged delete reservation in `deleted_ids`. The canvas filters by
  // `deleted_ids` so undo (Phase 2) can resurrect entries without re-fetch.
  // Also mirror M8: silently ignore unknown ids.
  const valid = new Set(file.entities.map((e) => e.id));
  const next = new Set(file.deleted_ids ?? []);
  for (const id of ids) {
    if (valid.has(id)) next.add(id);
  }
  file.deleted_ids = [...next].sort();

  const remaining = file.entities.length - file.deleted_ids.length;
  return { deleted_count: file.deleted_ids.length, remaining };
}

export async function mockExportDxf(sid: string, fid: string): Promise<Blob> {
  const bundle = _store.get(sid);
  if (!bundle) throw new Error(`mock: unknown session ${sid}`);
  const file = bundle.files.get(fid);
  if (!file) throw new Error(`mock: unknown file ${fid}`);
  // Synthesise a placeholder DXF-ish text so the download dialog is exercised.
  // (Real DXF generation lives in the backend; the mock just proves the
  // download wiring.)
  const lines: string[] = [
    '0',
    'SECTION',
    '2',
    'HEADER',
    '9',
    '$ACADVER',
    '1',
    'AC1024',
    '0',
    'ENDSEC',
    '0',
    'SECTION',
    '2',
    'ENTITIES',
    `999  mock export — ${file.entities.length - (file.deleted_ids?.length ?? 0)} entities`,
    '0',
    'ENDSEC',
    '0',
    'EOF',
  ];
  return new Blob([lines.join('\n')], { type: 'application/dxf' });
}
