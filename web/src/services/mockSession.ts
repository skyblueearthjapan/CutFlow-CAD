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
 *
 * Phase 4 contract (C1): mock honours the same field names as the real
 * backend — ``id`` / ``position`` / ``position_ratio`` / ``width_mm`` etc.
 * — so a switch between live and mock is transparent to the UI.
 */

import type {
  AddedHole,
  BoundingBox,
  Bridge,
  ChamferAnnotation,
  ChamferGeometry,
  ChamferSpec,
  CleanupFrameResult,
  CornerInfo,
  DeleteCandidates,
  DeleteResult,
  Dimension,
  EdgeInfo,
  EditedVertex,
  Entity,
  FileData,
  HolePatternRequest,
  Job,
  NestPlacement,
  NestRequest,
  NestResult,
  Note,
  OffsetRequest,
  OffsetResult,
  OuterDetectionResult,
  PdfExportOptions,
  SavedSession,
  Session,
  SessionFile,
  Sheet,
  SnapResult,
  Template,
} from '../types/dxf';

interface MockBundle {
  session: Session;
  files: Map<string, FileData>;
  /** Per-file chamfer specs (Phase 3 mock). */
  chamfer: Map<string, ChamferSpec[]>;
  /** Per-file Phase 4 annotation state. */
  dimensions: Map<string, Dimension[]>;
  vertexEdits: Map<string, EditedVertex[]>;
  addedHoles: Map<string, AddedHole[]>;
  notes: Map<string, Note[]>;
  bridges: Map<string, Bridge[]>;
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
  _store.set(sid, {
    session,
    files: fileMap,
    chamfer: new Map(),
    dimensions: new Map(),
    vertexEdits: new Map(),
    addedHoles: new Map(),
    notes: new Map(),
    bridges: new Map(),
  });
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

/** Build the corner + edge records from the file's bounding box. */
export async function mockGetCorners(
  sid: string,
  fid: string,
): Promise<{ corners: CornerInfo[]; edges: EdgeInfo[] }> {
  await new Promise((r) => setTimeout(r, 80));
  const file = locateFile(sid, fid);
  const bb = file.bounding_box;
  const r = 20;
  const xR = bb.max_x - 90 - r;
  const xL = bb.min_x + 90 + r;
  const yT = bb.max_y - 90 - r;
  const yB = bb.min_y + 90 + r;
  const corners: CornerInfo[] = [
    { corner_id: 'C1', position: [xR, yT], angle_deg: 90, is_acute: false, is_convex: true },
    { corner_id: 'C2', position: [xL, yT], angle_deg: 90, is_acute: false, is_convex: true },
    { corner_id: 'C3', position: [xL, yB], angle_deg: 90, is_acute: false, is_convex: true },
    { corner_id: 'C4', position: [xR, yB], angle_deg: 90, is_acute: false, is_convex: true },
  ];
  const w = xR - xL;
  const h = yT - yB;
  const edges: EdgeInfo[] = [
    { edge_id: 'E1', midpoint: [(xR + xL) / 2, yT], length: w },
    { edge_id: 'E2', midpoint: [xL, (yT + yB) / 2], length: h },
    { edge_id: 'E3', midpoint: [(xR + xL) / 2, yB], length: w },
    { edge_id: 'E4', midpoint: [xR, (yT + yB) / 2], length: h },
  ];
  return { corners, edges };
}

/** Build chamfer annotations from specs + corner / edge positions. */
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
  const frameIds = file.entities
    .filter((e) => e.category === 'frame')
    .map((e) => e.id);
  const next = new Set(file.deleted_ids ?? []);
  for (const id of frameIds) next.add(id);
  file.deleted_ids = [...next].sort();
  return { removed_count: frameIds.length, frame_entity_ids: frameIds };
}

/* -------------------- Phase 4: dim / edit / hole / note / bridge ---------- */

/** GET /dimensions — list (defaults to empty when nothing added yet). */
export async function mockListDimensions(sid: string, fid: string): Promise<Dimension[]> {
  const bundle = locateBundle(sid);
  return [...(bundle.dimensions.get(fid) ?? [])];
}

/** POST /dimensions — last-write-wins replace. */
export async function mockSetDimensions(
  sid: string,
  fid: string,
  dimensions: Dimension[],
): Promise<Dimension[]> {
  await new Promise((r) => setTimeout(r, 30));
  const bundle = locateBundle(sid);
  bundle.dimensions.set(fid, [...dimensions]);
  return [...dimensions];
}

/** Convenience: append + persist a single dim (mirrors api.ts addDimension). */
export async function mockAddDimension(
  sid: string,
  fid: string,
  dim: Dimension,
): Promise<Dimension[]> {
  const bundle = locateBundle(sid);
  const list = bundle.dimensions.get(fid) ?? [];
  const next = [...list, dim];
  bundle.dimensions.set(fid, next);
  return next;
}

/** DELETE /dimensions/{id}. */
export async function mockRemoveDimension(
  sid: string,
  fid: string,
  id: string,
): Promise<void> {
  const bundle = locateBundle(sid);
  const list = bundle.dimensions.get(fid) ?? [];
  bundle.dimensions.set(fid, list.filter((d) => d.id !== id));
}

/** POST /edit-vertex — record a vertex translation. */
export async function mockEditVertex(
  sid: string,
  fid: string,
  edit: EditedVertex,
): Promise<EditedVertex[]> {
  await new Promise((r) => setTimeout(r, 40));
  const bundle = locateBundle(sid);
  const list = bundle.vertexEdits.get(fid) ?? [];
  // Last-write-wins per (entity_id, vertex_index).
  const next = list.filter(
    (e) => !(e.entity_id === edit.entity_id && e.vertex_index === edit.vertex_index),
  );
  next.push(edit);
  bundle.vertexEdits.set(fid, next);
  return [...next];
}

/** POST /snap — return the snapped point (endpoint / midpoint / grid). */
export async function mockSnap(
  sid: string,
  fid: string,
  cursor: [number, number],
  tolerance = 6,
): Promise<SnapResult> {
  const file = locateFile(sid, fid);
  const cx = cursor[0];
  const cy = cursor[1];

  let best: { p: [number, number]; type: SnapResult['type']; d: number; eid: string | null } | null = null;
  const consider = (
    p: [number, number],
    type: SnapResult['type'],
    eid: string | null,
  ) => {
    const d = Math.hypot(p[0] - cx, p[1] - cy);
    if (d > tolerance) return;
    if (!best || d < best.d) best = { p, type, d, eid };
  };
  for (const e of file.entities) {
    if (e.type === 'LINE') {
      const x1 = +e.geom?.x1 || 0, y1 = +e.geom?.y1 || 0;
      const x2 = +e.geom?.x2 || 0, y2 = +e.geom?.y2 || 0;
      consider([x1, y1], 'endpoint', e.id);
      consider([x2, y2], 'endpoint', e.id);
      consider([(x1 + x2) / 2, (y1 + y2) / 2], 'midpoint', e.id);
    } else if (e.type === 'CIRCLE') {
      consider([+e.geom?.cx || 0, +e.geom?.cy || 0], 'center', e.id);
    }
  }
  if (best !== null) {
    const b = best as { p: [number, number]; type: SnapResult['type']; d: number; eid: string | null };
    return { snapped: b.p, type: b.type, entity_id: b.eid, distance: b.d };
  }
  // Grid snap (1 mm).
  const gx = Math.round(cx);
  const gy = Math.round(cy);
  if (Math.hypot(gx - cx, gy - cy) <= 0.5) {
    return { snapped: [gx, gy], type: 'grid', entity_id: null, distance: Math.hypot(gx - cx, gy - cy) };
  }
  return { snapped: null, type: null };
}

/** GET /holes (added) — list. */
export async function mockListHoles(sid: string, fid: string): Promise<AddedHole[]> {
  const bundle = locateBundle(sid);
  return [...(bundle.addedHoles.get(fid) ?? [])];
}

/** POST /holes — append a hole (dedup by id). */
export async function mockAddHole(
  sid: string,
  fid: string,
  hole: AddedHole,
): Promise<AddedHole[]> {
  await new Promise((r) => setTimeout(r, 50));
  const bundle = locateBundle(sid);
  const list = bundle.addedHoles.get(fid) ?? [];
  const next = [...list.filter((h) => h.id !== hole.id), hole];
  bundle.addedHoles.set(fid, next);
  return next;
}

/** POST /holes/pattern — expand a rows×cols grid into individual holes. */
export async function mockAddHolePattern(
  sid: string,
  fid: string,
  req: HolePatternRequest,
): Promise<AddedHole[]> {
  await new Promise((r) => setTimeout(r, 80));
  const bundle = locateBundle(sid);
  const list = bundle.addedHoles.get(fid) ?? [];
  const added: AddedHole[] = [];
  for (let r = 0; r < req.rows; r++) {
    for (let c = 0; c < req.cols; c++) {
      added.push({
        id: uid('h'),
        position: [
          req.anchor[0] + c * req.spacing[0],
          req.anchor[1] + r * req.spacing[1],
        ],
        diameter: req.diameter,
        tap_note: req.tap_note ?? null,
      });
    }
  }
  bundle.addedHoles.set(fid, [...list, ...added]);
  return [...bundle.addedHoles.get(fid)!];
}

/** DELETE /holes/{id}. */
export async function mockRemoveHole(
  sid: string,
  fid: string,
  id: string,
): Promise<void> {
  const bundle = locateBundle(sid);
  const list = bundle.addedHoles.get(fid) ?? [];
  bundle.addedHoles.set(fid, list.filter((h) => h.id !== id));
}

/** GET /notes — list. */
export async function mockListNotes(sid: string, fid: string): Promise<Note[]> {
  const bundle = locateBundle(sid);
  return [...(bundle.notes.get(fid) ?? [])];
}

/** POST /notes — last-write-wins replace. */
export async function mockSetNotes(
  sid: string,
  fid: string,
  notes: Note[],
): Promise<Note[]> {
  await new Promise((r) => setTimeout(r, 30));
  const bundle = locateBundle(sid);
  bundle.notes.set(fid, [...notes]);
  return [...notes];
}

/** Append a single note (mirrors api.ts addNote). */
export async function mockAddNote(
  sid: string,
  fid: string,
  note: Note,
): Promise<Note[]> {
  const bundle = locateBundle(sid);
  const list = bundle.notes.get(fid) ?? [];
  const next = [...list, note];
  bundle.notes.set(fid, next);
  return next;
}

/** DELETE /notes/{id}. */
export async function mockRemoveNote(
  sid: string,
  fid: string,
  id: string,
): Promise<void> {
  const bundle = locateBundle(sid);
  const list = bundle.notes.get(fid) ?? [];
  bundle.notes.set(fid, list.filter((n) => n.id !== id));
}

/** GET /bridges — list. */
export async function mockListBridges(sid: string, fid: string): Promise<Bridge[]> {
  const bundle = locateBundle(sid);
  return [...(bundle.bridges.get(fid) ?? [])];
}

/** POST /bridges — last-write-wins replace. */
export async function mockSetBridges(
  sid: string,
  fid: string,
  bridges: Bridge[],
): Promise<Bridge[]> {
  await new Promise((r) => setTimeout(r, 30));
  const bundle = locateBundle(sid);
  bundle.bridges.set(fid, [...bridges]);
  return [...bridges];
}

/** Append a single bridge (mirrors api.ts addBridge). */
export async function mockAddBridge(
  sid: string,
  fid: string,
  bridge: Bridge,
): Promise<Bridge[]> {
  await new Promise((r) => setTimeout(r, 50));
  const bundle = locateBundle(sid);
  const list = bundle.bridges.get(fid) ?? [];
  const next = [...list, bridge];
  bundle.bridges.set(fid, next);
  return next;
}

/** POST /bridges/auto — auto-distribute N bridges evenly around the outer.
 *  H7: matches the backend exactly — auto replaces the ENTIRE bridge list
 *  (no "manual + auto" mix) so the UI sees identical behaviour either way. */
export async function mockAddBridgeAuto(
  sid: string,
  fid: string,
  count: number,
  width_mm: number,
): Promise<Bridge[]> {
  await new Promise((r) => setTimeout(r, 100));
  const bundle = locateBundle(sid);
  const file = locateFile(sid, fid);
  const bb = file.bounding_box;
  const w = bb.max_x - bb.min_x;
  const h = bb.max_y - bb.min_y;
  const midX = (bb.min_x + bb.max_x) / 2;
  const midY = (bb.min_y + bb.max_y) / 2;
  // Fixed 4 edges; clamp N to that for the mock.
  const all: Array<{ edge_id: string; pos: [number, number] }> = [
    { edge_id: 'E1', pos: [midX, bb.max_y - h * 0.08] }, // top
    { edge_id: 'E4', pos: [bb.max_x - w * 0.08, midY] }, // right
    { edge_id: 'E3', pos: [midX, bb.min_y + h * 0.08] }, // bottom
    { edge_id: 'E2', pos: [bb.min_x + w * 0.08, midY] }, // left
  ];
  const positions = all.slice(0, Math.max(0, Math.min(4, count)));
  const next: Bridge[] = positions.map((p) => ({
    id: uid('b'),
    edge_id: p.edge_id,
    position_ratio: 0.5,
    width_mm,
    position: p.pos,
  }));
  bundle.bridges.set(fid, next);
  return [...next];
}

/** DELETE /bridges/{id}. */
export async function mockRemoveBridge(
  sid: string,
  fid: string,
  id: string,
): Promise<void> {
  const bundle = locateBundle(sid);
  const list = bundle.bridges.get(fid) ?? [];
  bundle.bridges.set(fid, list.filter((b) => b.id !== id));
}

export async function mockExportPdf(
  sid: string,
  fid: string,
  opts: PdfExportOptions,
): Promise<Blob> {
  await new Promise((r) => setTimeout(r, 150));
  const file = locateFile(sid, fid);
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

/* -------------------- Phase 5: nesting / history / templates -------------- */

/** In-memory job + saved-session storage for the Phase 5 mock. */
interface MockJob {
  job: Job;
  /** sid used to compute the result on demand. */
  sid: string;
  req: NestRequest;
  /** Final result cached after the job transitions to 'success'. */
  result?: NestResult;
  /** Wall-clock time the job entered 'running' so progress ramps up. */
  startedAt: number;
}

const _jobs = new Map<string, MockJob>();
const _savedSessions = new Map<string, { meta: SavedSession; session: Session; files: Map<string, FileData> }>();

/** Built-in template presets surfaced by the mock.
 *  C1/H1: Template fields are BE-aligned (``template_id`` / ``spacing_mm``
 *  alias keys, plus required ``material`` / ``thickness_mm``). */
const _templates: Template[] = [
  {
    template_id: 'ss400-t9',
    name: 'SS400 t9 標準',
    description: '一般構造用鋼 / 切板 t9 — 加工代 3.0 mm / シート 1500 × 3000',
    material: 'SS400',
    thickness_mm: 9,
    spacing_mm: 3.0,
    sheet_width: 1500,
    sheet_height: 3000,
  },
  {
    template_id: 'sphc-t1_6',
    name: 'SPHC t1.6 薄板',
    description: '熱間圧延薄板 / 加工代 1.5 mm / シート 1219 × 2438',
    material: 'SPHC',
    thickness_mm: 1.6,
    spacing_mm: 1.5,
    sheet_width: 1219,
    sheet_height: 2438,
  },
  {
    template_id: 'sus304-t3',
    name: 'SUS304 t3',
    description: 'ステンレス / 加工代 2.5 mm / シート 1219 × 2438',
    material: 'SUS304',
    thickness_mm: 3,
    spacing_mm: 2.5,
    sheet_width: 1219,
    sheet_height: 2438,
  },
];

/** Build a plausible Bottom-Left-Fill placement. Each part bbox uses the
 *  source file's bounding-box; the mock packs them row-by-row from the
 *  bottom-left corner. Yields ~65-80% utilisation for the demo geometry.
 *
 *  Phase 5 C4: 出力は BE-aligned (Sheet.sheet_index / width_mm / height_mm /
 *  efficiency, placement.x_mm / y_mm / width_mm / height_mm / rotation_deg)。 */
function packBLF(
  parts: Array<{ file_id: string; w: number; h: number }>,
  sheet_w: number,
  sheet_h: number,
  spacing: number,
  allow_rotate: boolean,
): { sheets: Sheet[]; unplaced: number } {
  const sheets: Array<Sheet & { _cursor?: { x: number; y: number; rowH: number } }> = [];
  let unplaced = 0;

  function tryPlace(
    sheet: Sheet & { _cursor: { x: number; y: number; rowH: number } },
    p: { file_id: string; w: number; h: number },
  ): boolean {
    let pw = p.w + spacing * 2;
    let ph = p.h + spacing * 2;
    let rotated = false;
    if (pw > sheet.width_mm - sheet._cursor.x && allow_rotate) {
      const r_pw = p.h + spacing * 2;
      const r_ph = p.w + spacing * 2;
      if (r_pw <= sheet.width_mm - sheet._cursor.x) {
        pw = r_pw; ph = r_ph; rotated = true;
      }
    }
    if (sheet._cursor.x + pw > sheet.width_mm) {
      sheet._cursor.x = 0;
      sheet._cursor.y += sheet._cursor.rowH;
      sheet._cursor.rowH = 0;
      pw = p.w + spacing * 2;
      ph = p.h + spacing * 2;
      rotated = false;
      if (allow_rotate && p.h < p.w && pw > sheet.width_mm) {
        pw = p.h + spacing * 2; ph = p.w + spacing * 2; rotated = true;
      }
    }
    if (pw > sheet.width_mm || sheet._cursor.y + ph > sheet.height_mm) return false;
    const dw = rotated ? p.h : p.w;
    const dh = rotated ? p.w : p.h;
    sheet.placements.push({
      file_id: p.file_id,
      sheet_index: sheet.sheet_index,
      x_mm: sheet._cursor.x + spacing,
      y_mm: sheet._cursor.y + spacing,
      width_mm: dw,
      height_mm: dh,
      rotation_deg: rotated ? 90 : 0,
    });
    sheet._cursor.x += pw;
    if (ph > sheet._cursor.rowH) sheet._cursor.rowH = ph;
    return true;
  }

  function newSheet(): Sheet & { _cursor: { x: number; y: number; rowH: number } } {
    const sh = {
      sheet_index: sheets.length,
      width_mm: sheet_w,
      height_mm: sheet_h,
      placements: [] as NestPlacement[],
      efficiency: 0,
      used_area_mm2: 0,
      sheet_area_mm2: sheet_w * sheet_h,
      _cursor: { x: 0, y: 0, rowH: 0 },
    };
    sheets.push(sh);
    return sh;
  }

  let active: (Sheet & { _cursor: { x: number; y: number; rowH: number } }) | null = null;
  for (const p of parts) {
    if (
      Math.min(p.w, p.h) + spacing * 2 > Math.min(sheet_w, sheet_h) ||
      Math.max(p.w, p.h) + spacing * 2 > Math.max(sheet_w, sheet_h)
    ) {
      unplaced += 1;
      continue;
    }
    if (!active) active = newSheet();
    if (!tryPlace(active, p)) {
      active = newSheet();
      tryPlace(active, p);
    }
  }

  for (const sh of sheets) {
    const used = sh.placements.reduce((acc, pl) => acc + pl.width_mm * pl.height_mm, 0);
    sh.used_area_mm2 = used;
    sh.efficiency = sh.sheet_area_mm2 > 0 ? used / sh.sheet_area_mm2 : 0;
    delete sh._cursor;
  }
  return { sheets, unplaced };
}

/** Build a NestResult by packing the chosen files from the session bundle. */
function buildNestResult(_job_id: string, sid: string, req: NestRequest): NestResult {
  const bundle = locateBundle(sid);
  const fileIds = req.file_ids.length > 0
    ? req.file_ids
    : [...bundle.files.keys()];

  const parts: Array<{ file_id: string; w: number; h: number }> = [];
  for (const fid of fileIds) {
    const f = bundle.files.get(fid);
    if (!f) continue;
    const bb = f.bounding_box;
    parts.push({
      file_id: fid,
      w: Math.max(1, bb.max_x - bb.min_x),
      h: Math.max(1, bb.max_y - bb.min_y),
    });
  }

  const { sheets, unplaced } = packBLF(
    parts,
    req.sheet.width_mm,
    req.sheet.height_mm,
    req.spacing_mm,
    req.rotation,
  );
  const total = sheets.length > 0
    ? sheets.reduce((acc, s) => acc + s.efficiency, 0) / sheets.length
    : 0;
  const warnings: string[] = [];
  if (unplaced > 0) warnings.push(`シートに収まらない部品が ${unplaced} 件あります`);
  return {
    sheets,
    utilization: total,
    unplaced,
    warnings,
  };
}

export async function mockNest(sid: string, req: NestRequest): Promise<{ job_id: string }> {
  // Make sure the session exists so the polling endpoints have something to
  // hand back. Throws the standard "unknown session" if not.
  locateBundle(sid);
  await new Promise((r) => setTimeout(r, 60));
  const job_id = uid('job');
  // C2: status は BE 形式 (``pending`` | ``running`` | ``completed`` | ``failed``)
  _jobs.set(job_id, {
    job: { job_id, status: 'pending', progress: 0, message: 'キューに登録しました' },
    sid,
    req,
    startedAt: performance.now(),
  });
  return { job_id };
}

export async function mockGetJobStatus(job_id: string): Promise<Job> {
  const entry = _jobs.get(job_id);
  if (!entry) {
    return { job_id, status: 'failed', progress: 0, error: `unknown job ${job_id}` };
  }
  // C2: 0 → pending (briefly) → running → completed
  const elapsed = performance.now() - entry.startedAt;
  if (entry.job.status === 'pending' && elapsed > 250) {
    entry.job = { job_id, status: 'running', progress: 0.1, message: '初期化中…' };
  }
  if (entry.job.status === 'running') {
    const p = Math.min(1, 0.1 + elapsed / 1800);
    entry.job = {
      job_id,
      status: 'running',
      progress: p,
      message: p < 0.5
        ? `部品を解析中… (${Math.round(p * 100)}%)`
        : `シートに配置中… (${Math.round(p * 100)}%)`,
    };
    if (p >= 1) {
      entry.result = buildNestResult(job_id, entry.sid, entry.req);
      entry.job = {
        job_id,
        status: 'completed',
        progress: 1,
        message: `完了 (${entry.result.sheets.length} シート)`,
      };
    }
  }
  return { ...entry.job };
}

export async function mockGetNestResult(job_id: string): Promise<NestResult> {
  const entry = _jobs.get(job_id);
  if (!entry || !entry.result) {
    throw new Error(`mock: nest result not ready for ${job_id}`);
  }
  // C4: BE-aligned Sheet shape (sheet_index / width_mm / height_mm / efficiency).
  return {
    sheets: entry.result.sheets.map((s) => ({
      sheet_index: s.sheet_index,
      width_mm: s.width_mm,
      height_mm: s.height_mm,
      placements: s.placements.map((p: NestPlacement) => ({ ...p })),
      efficiency: s.efficiency,
      used_area_mm2: s.used_area_mm2,
      sheet_area_mm2: s.sheet_area_mm2,
    })),
    utilization: entry.result.utilization,
    unplaced: entry.result.unplaced,
    warnings: [...entry.result.warnings],
  };
}

export async function mockExportNestSheet(job_id: string, sheet_index: number): Promise<Blob> {
  const entry = _jobs.get(job_id);
  if (!entry || !entry.result) throw new Error(`mock: nest result not ready for ${job_id}`);
  const sh = entry.result.sheets.find((s) => s.sheet_index === sheet_index);
  if (!sh) throw new Error(`mock: unknown sheet ${sheet_index}`);
  const head = [
    '0', 'SECTION', '2', 'ENTITIES',
    `999  mock nest export — sheet ${sh.sheet_index} / ${sh.width_mm}x${sh.height_mm} mm`,
    `999  placements=${sh.placements.length} eff=${(sh.efficiency * 100).toFixed(1)}%`,
  ];
  const placements = sh.placements.flatMap((p) => [
    `999  ${p.file_id}: ${p.x_mm.toFixed(0)},${p.y_mm.toFixed(0)}` +
      ` ${p.width_mm.toFixed(0)}x${p.height_mm.toFixed(0)}` +
      `${p.rotation_deg ? ` R${p.rotation_deg}` : ''}`,
  ]);
  const lines = [...head, ...placements, '0', 'ENDSEC', '0', 'EOF'];
  return new Blob([lines.join('\n')], { type: 'application/dxf' });
}

export async function mockSaveSession(name: string, sid: string): Promise<SavedSession> {
  await new Promise((r) => setTimeout(r, 60));
  const bundle = locateBundle(sid);
  // Structural clone so a subsequent edit in the live session doesn't leak.
  const clonedFiles = new Map<string, FileData>();
  for (const [k, v] of bundle.files) clonedFiles.set(k, JSON.parse(JSON.stringify(v)));
  const meta: SavedSession = {
    name,
    saved_at: new Date().toISOString(),
    file_count: clonedFiles.size,
    note: bundle.session.files[0]?.name,
  };
  _savedSessions.set(name, {
    meta,
    session: JSON.parse(JSON.stringify(bundle.session)) as Session,
    files: clonedFiles,
  });
  return meta;
}

export async function mockListSavedSessions(): Promise<SavedSession[]> {
  // Sort newest first to match the typical operator workflow.
  return [..._savedSessions.values()]
    .map((e) => ({ ...e.meta }))
    .sort((a, b) => (a.saved_at < b.saved_at ? 1 : -1));
}

export async function mockLoadSession(name: string): Promise<Session> {
  const entry = _savedSessions.get(name);
  if (!entry) throw new Error(`保存済みセッション「${name}」が見つかりません`);
  // Re-register the bundle under a fresh sid so the live session id matches
  // the returned payload exactly (mirrors what the real backend would do).
  const newSid = uid('sid');
  const sessionCopy: Session = {
    session_id: newSid,
    files: entry.session.files.map((f) => ({ ...f })),
    expires_at: new Date(Date.now() + 24 * 3600 * 1000).toISOString(),
  };
  const fileMap = new Map<string, FileData>();
  for (const [k, v] of entry.files) fileMap.set(k, JSON.parse(JSON.stringify(v)));
  _store.set(newSid, {
    session: sessionCopy,
    files: fileMap,
    chamfer: new Map(),
    dimensions: new Map(),
    vertexEdits: new Map(),
    addedHoles: new Map(),
    notes: new Map(),
    bridges: new Map(),
  });
  return sessionCopy;
}

export async function mockGetTemplates(): Promise<Template[]> {
  return _templates.map((t) => ({ ...t }));
}

export async function mockApplyTemplate(_sid: string, template_id: string): Promise<Template> {
  const tpl = _templates.find((t) => t.template_id === template_id);
  if (!tpl) throw new Error(`unknown template ${template_id}`);
  // The live backend updates server-side per-session defaults; the mock just
  // echoes the template back — the UI store applies the values locally.
  return { ...tpl };
}

/* -------------------- Phase 6: server-rendered SVG (mock) ---------------- */

/**
 * Phase 6 mock for GET .../render-svg. The real backend uses ezdxf to render
 * the full drawing (dimensions / hatches / blocks). The mock builds a thin
 * stand-in from the cached entity bbox so the "リアル表示" toggle has
 * something to overlay during local development without the live backend —
 * the foreground operation layer still carries the actual geometry, so the
 * background being a placeholder is acceptable while wiring the UX.
 */
export async function mockRenderSvg(
  sid: string,
  fid: string,
  _options?: { apply_deletions?: boolean; apply_edits?: boolean; dark_theme?: boolean },
): Promise<import('../types/dxf').RenderedSvg> {
  const bundle = _store.get(sid);
  const file = bundle?.files.get(fid);
  const bb = file?.bounding_box ?? { min_x: 0, min_y: 0, max_x: 1200, max_y: 800 };
  const w = Math.max(1, bb.max_x - bb.min_x);
  const h = Math.max(1, bb.max_y - bb.min_y);
  // Minimal placeholder: a faint dashed outline of the bbox so the operator
  // can confirm the background layer alignment in local dev. Y-flipped
  // wrapper so the (Y-up) DXF bbox lines up under the foreground.
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${bb.min_x} ${bb.min_y} ${w} ${h}" preserveAspectRatio="xMidYMid meet">`
    + `<g transform="translate(0 ${bb.max_y + bb.min_y}) scale(1 -1)">`
    + `<rect x="${bb.min_x}" y="${bb.min_y}" width="${w}" height="${h}" `
    + `fill="none" stroke="#4dcfe0" stroke-opacity="0.18" stroke-width="0.6" `
    + `stroke-dasharray="6 4" />`
    + `</g></svg>`;
  return { svg, bbox: { ...bb } };
}
