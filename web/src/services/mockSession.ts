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
  BoundingBox,
  ChamferAnnotation,
  ChamferGeometry,
  ChamferSpec,
  CleanupFrameResult,
  CornerInfo,
  DeleteCandidates,
  DeleteResult,
  EdgeInfo,
  Entity,
  FileData,
  OffsetRequest,
  OffsetResult,
  OuterDetectionResult,
  PdfExportOptions,
  Session,
  SessionFile,
} from '../types/dxf';

interface MockBundle {
  session: Session;
  files: Map<string, FileData>;
  /** Per-file chamfer specs (Phase 3 mock). */
  chamfer: Map<string, ChamferSpec[]>;
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
  _store.set(sid, { session, files: fileMap, chamfer: new Map() });
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

export async function mockExportDxf(
  sid: string,
  fid: string,
  withOffset = false,
): Promise<Blob> {
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
    `999  with_offset=${withOffset ? 'true' : 'false'}`,
    '0',
    'ENDSEC',
    '0',
    'EOF',
  ];
  return new Blob([lines.join('\n')], { type: 'application/dxf' });
}

/* -------------------- Phase 2: outer detection / offset ------------------- */

/** Locate the file in the mock store or throw the same "unknown" error
 *  shape the other mock endpoints use. */
function locateFile(sid: string, fid: string): FileData {
  const bundle = _store.get(sid);
  if (!bundle) throw new Error(`mock: unknown session ${sid}`);
  const file = bundle.files.get(fid);
  if (!file) throw new Error(`mock: unknown file ${fid}`);
  return file;
}

/** Build a LoopSummary from a list of outer entity ids. */
function summariseLoop(
  file: FileData,
  ids: string[],
): OuterDetectionResult['loop_summary'] {
  const map = new Map(file.entities.map((e) => [e.id, e]));
  let lines = 0;
  let arcs = 0;
  let perimeter = 0;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  const grow = (x: number, y: number) => {
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  };
  for (const id of ids) {
    const e = map.get(id);
    if (!e) continue;
    if (e.type === 'LINE') {
      lines += 1;
      const x1 = +e.geom?.x1 || 0, y1 = +e.geom?.y1 || 0;
      const x2 = +e.geom?.x2 || 0, y2 = +e.geom?.y2 || 0;
      perimeter += Math.hypot(x2 - x1, y2 - y1);
      grow(x1, y1); grow(x2, y2);
    } else if (e.type === 'ARC') {
      arcs += 1;
      const r = +e.geom?.r || 0;
      const a1 = +e.geom?.start_angle || 0;
      const a2 = +e.geom?.end_angle || 0;
      let sweep = a2 - a1;
      while (sweep < 0) sweep += 360;
      perimeter += (Math.PI * r * sweep) / 180;
      const cx = +e.geom?.cx || 0, cy = +e.geom?.cy || 0;
      grow(cx - r, cy - r); grow(cx + r, cy + r);
    }
  }
  if (!Number.isFinite(minX)) {
    // Fall back to the file bbox if no entities matched.
    return {
      closed: ids.length > 0,
      segments: ids.length,
      lines,
      arcs,
      perimeter,
      area: 0,
      bounding_box: file.bounding_box,
    };
  }
  const w = maxX - minX;
  const h = maxY - minY;
  return {
    closed: ids.length > 0,
    segments: ids.length,
    lines,
    arcs,
    perimeter,
    // Synthetic area — for a rounded rectangle this is close enough for the
    // inspector display. The real value is computed server-side.
    area: w * h,
    bounding_box: { min_x: minX, min_y: minY, max_x: maxX, max_y: maxY },
  };
}

export async function mockDetectOuter(
  sid: string,
  fid: string,
): Promise<OuterDetectionResult> {
  await new Promise((r) => setTimeout(r, 180));
  const file = locateFile(sid, fid);
  // The sample data builds entities with `category: 'outer'` for the rounded
  // rectangle — use those ids as the detected loop. This produces a plausible
  // success response for the UI without any geometry libraries.
  const outerIds = file.entities.filter((e) => e.category === 'outer').map((e) => e.id);
  if (outerIds.length === 0) {
    return {
      status: 'failed',
      confidence: 0,
      outer_loop: [],
      loop_summary: {
        closed: false,
        segments: 0,
        lines: 0,
        arcs: 0,
        perimeter: 0,
        area: 0,
        bounding_box: file.bounding_box,
      },
      warnings: ['外径候補となる閉ループが見つかりませんでした'],
      candidates: [],
    };
  }
  const summary = summariseLoop(file, outerIds);
  return {
    status: 'success',
    confidence: 0.92,
    outer_loop: outerIds,
    loop_summary: summary,
    warnings: [],
    candidates: [
      { loop: outerIds, confidence: 0.92, area: summary.area },
    ],
  };
}

export async function mockConfirmOuterManual(
  sid: string,
  fid: string,
  entityIds: string[],
): Promise<OuterDetectionResult> {
  await new Promise((r) => setTimeout(r, 120));
  const file = locateFile(sid, fid);
  if (entityIds.length < 3) {
    // Mirror the live backend's 422 "not closed" semantics with an Error
    // (api.ts surfaces this via its ApiError flow in production).
    throw new Error('手動選択は3本以上の線で閉ループになる必要があります');
  }
  const summary = summariseLoop(file, entityIds);
  return {
    status: 'success',
    confidence: 1.0,
    outer_loop: entityIds,
    loop_summary: summary,
    warnings: [],
    candidates: [],
  };
}

export async function mockComputeOffset(
  sid: string,
  fid: string,
  req: OffsetRequest,
): Promise<OffsetResult> {
  await new Promise((r) => setTimeout(r, 200));
  const file = locateFile(sid, fid);
  const outerIds = file.entities.filter((e) => e.category === 'outer').map((e) => e.id);
  const summary = summariseLoop(file, outerIds);
  const base: BoundingBox = summary.bounding_box;
  const w = base.max_x - base.min_x;
  const h = base.max_y - base.min_y;
  const d = req.default_mm;
  // Per-edge overrides aren't truly modelled in the mock; we just expand the
  // bounding box uniformly by the default offset for a reasonable preview.
  const offsetBbox: BoundingBox = {
    min_x: base.min_x - d,
    min_y: base.min_y - d,
    max_x: base.max_x + d,
    max_y: base.max_y + d,
  };
  const ow = offsetBbox.max_x - offsetBbox.min_x;
  const oh = offsetBbox.max_y - offsetBbox.min_y;
  // Treat the offset loop as a simple rounded rectangle around the outer one.
  // The renderer only needs vertices for the dashed preview; bulge=0 keeps
  // it as straight LWPOLYLINE segments which is sufficient at this scale.
  const vertices: [number, number, number][] = [
    [offsetBbox.min_x, offsetBbox.min_y, 0],
    [offsetBbox.max_x, offsetBbox.min_y, 0],
    [offsetBbox.max_x, offsetBbox.max_y, 0],
    [offsetBbox.min_x, offsetBbox.max_y, 0],
  ];
  const perimeter = 2 * (ow + oh);
  // Material efficiency: original area / plate area (capped at 1.0).
  const plateArea = ow * oh;
  const originalArea = w * h;
  const efficiency = plateArea > 0 ? Math.min(originalArea / plateArea, 1) : 0;
  return {
    offset_loop: { type: 'LWPOLYLINE', vertices, closed: true },
    perimeter,
    bounding_box: offsetBbox,
    plate_size: `${Math.round(ow)} × ${Math.round(oh)} mm`,
    material_efficiency: efficiency,
    warnings: [],
  };
}

/* -------------------- Phase 3: chamfer / cleanup-frame / pdf -------------- */

/** Locate the bundle for the chamfer/frame/pdf mock endpoints. */
function locateBundle(sid: string): MockBundle {
  const bundle = _store.get(sid);
  if (!bundle) throw new Error(`mock: unknown session ${sid}`);
  return bundle;
}

/** Build the corner + edge records from the file's bounding box.
 *
 *  The sample geometry is a rounded rectangle, so we synthesize the four
 *  convex corners at the bbox extents (C1: top-right, C2: top-left,
 *  C3: bottom-left, C4: bottom-right) — matching the labels the v3 mockup
 *  uses on its corner-chip list. Edges (E1..E4) follow the same CCW order
 *  starting at the top side. (C1, H1) */
export async function mockGetCorners(
  sid: string,
  fid: string,
): Promise<{ corners: CornerInfo[]; edges: EdgeInfo[] }> {
  await new Promise((r) => setTimeout(r, 80));
  const file = locateFile(sid, fid);
  // The sample DXF has a rounded rectangle outer (margin r=20). We pick the
  // 4 visual corners just inside the rounding so the marker sits on the
  // outer edge. The exact value is cosmetic — backend supplies real ones.
  const bb = file.bounding_box;
  const r = 20;
  const xR = bb.max_x - 90 - r;
  const xL = bb.min_x + 90 + r;
  const yT = bb.max_y - 90 - r;
  const yB = bb.min_y + 90 + r;
  const corners: CornerInfo[] = [
    { corner_id: 'C1', position: [xR, yT], angle_deg: 90, is_acute: false, is_convex: true },  // 右上
    { corner_id: 'C2', position: [xL, yT], angle_deg: 90, is_acute: false, is_convex: true },  // 左上
    { corner_id: 'C3', position: [xL, yB], angle_deg: 90, is_acute: false, is_convex: true },  // 左下
    { corner_id: 'C4', position: [xR, yB], angle_deg: 90, is_acute: false, is_convex: true },  // 右下
  ];
  const w = xR - xL;
  const h = yT - yB;
  const edges: EdgeInfo[] = [
    { edge_id: 'E1', midpoint: [(xR + xL) / 2, yT], length: w },  // top
    { edge_id: 'E2', midpoint: [xL, (yT + yB) / 2], length: h },  // left
    { edge_id: 'E3', midpoint: [(xR + xL) / 2, yB], length: w },  // bottom
    { edge_id: 'E4', midpoint: [xR, (yT + yB) / 2], length: h },  // right
  ];
  return { corners, edges };
}

/** Build chamfer annotations from specs + corner / edge positions.
 *  Backend format: `{ corner_id, position, label, kind }` (H2). */
function buildChamferGeometry(
  specs: ChamferSpec[],
  corners: CornerInfo[],
  edges: EdgeInfo[],
): ChamferGeometry {
  const byCorner = new Map(corners.map((c) => [c.corner_id, c]));
  const byEdge = new Map(edges.map((e) => [e.edge_id, e]));
  const items: ChamferAnnotation[] = [];
  for (const s of specs) {
    const c = byCorner.get(s.corner_id);
    if (c) {
      items.push({
        corner_id: s.corner_id,
        position: [c.position[0], c.position[1]],
        label: s.type === 'bevel' ? `開先 ${s.angle_deg}°` : `C${s.size_mm}`,
        kind: s.type === 'bevel' ? 'bevel' : 'C',
      });
      continue;
    }
    const e = byEdge.get(s.corner_id);
    if (e) {
      items.push({
        corner_id: s.corner_id,
        position: [e.midpoint[0], e.midpoint[1]],
        label: s.type === 'bevel' ? `開先 ${s.angle_deg}°` : `C${s.size_mm}`,
        kind: s.type === 'bevel' ? 'bevel' : 'C',
      });
    }
  }
  return { items };
}

export async function mockSetChamfer(
  sid: string,
  fid: string,
  specs: ChamferSpec[],
): Promise<{ specs: ChamferSpec[]; geometry: ChamferGeometry }> {
  await new Promise((r) => setTimeout(r, 100));
  const bundle = locateBundle(sid);
  // Mirror the backend: full replace.
  bundle.chamfer.set(fid, [...specs]);
  const { corners, edges } = await mockGetCorners(sid, fid);
  return { specs: [...specs], geometry: buildChamferGeometry(specs, corners, edges) };
}

export async function mockGetChamfer(
  sid: string,
  fid: string,
): Promise<{ specs: ChamferSpec[]; geometry: ChamferGeometry } | null> {
  const bundle = locateBundle(sid);
  const specs = bundle.chamfer.get(fid);
  if (!specs) return null;
  const { corners, edges } = await mockGetCorners(sid, fid);
  return { specs: [...specs], geometry: buildChamferGeometry(specs, corners, edges) };
}

export async function mockCleanupFrame(
  sid: string,
  fid: string,
): Promise<CleanupFrameResult> {
  await new Promise((r) => setTimeout(r, 120));
  const file = locateFile(sid, fid);
  // The sample data tags one entity with category 'frame' — use those ids.
  const frameIds = file.entities
    .filter((e) => e.category === 'frame')
    .map((e) => e.id);
  // Mirror live backend: stage for deletion via deleted_ids so the canvas
  // hides them immediately on next file fetch.
  const next = new Set(file.deleted_ids ?? []);
  for (const id of frameIds) next.add(id);
  file.deleted_ids = [...next].sort();
  return { removed_count: frameIds.length, frame_entity_ids: frameIds };
}

export async function mockExportPdf(
  sid: string,
  fid: string,
  opts: PdfExportOptions,
): Promise<Blob> {
  await new Promise((r) => setTimeout(r, 150));
  const file = locateFile(sid, fid);
  // Synthesise a minimal-but-valid PDF so the download flow can be tested
  // without ReportLab in the loop. The contents only need to identify
  // themselves as PDF (header) and end (EOF); browsers will open / save it.
  const note =
    `% mock pdf for ${file.name}\n` +
    `% frame=${opts.frame} with_offset=${opts.with_offset} with_chamfer=${opts.with_chamfer}\n`;
  const body =
    '%PDF-1.4\n' +
    note +
    '1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n' +
    '2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n' +
    '3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >> endobj\n' +
    'xref\n0 4\n' +
    '0000000000 65535 f \n' +
    '0000000009 00000 n \n' +
    '0000000058 00000 n \n' +
    '0000000110 00000 n \n' +
    'trailer << /Size 4 /Root 1 0 R >>\nstartxref\n179\n%%EOF\n';
  return new Blob([body], { type: 'application/pdf' });
}
