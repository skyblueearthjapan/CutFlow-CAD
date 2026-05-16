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
import type { Entity } from '../types/dxf';

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
} = useSession();

const meta = computed(() => toolMeta[activeTool.value]);

// chamfer tool: corner chip ON state (mockup initial: top-right only).
// Kept as local state because chamfer is Phase 3 — UI-only for now.
const selectedCorners = ref<Set<number>>(new Set([0]));
function toggleCorner(i: number) {
  const next = new Set(selectedCorners.value);
  if (next.has(i)) next.delete(i);
  else next.add(i);
  selectedCorners.value = next;
}

/** Total number of entities that would be deleted on confirm. */
const deleteCount = computed(() => selectedForDelete.value.size);

/** Pill (top-right of tool head) for delete mode: live candidate count. */
const deletePillText = computed(() => {
  if (!currentFile.value) return meta.value.pill.text;
  return `${totalDeleteCandidates.value} 件`;
});

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
        :class="activeTool === 'outer' ? outerPillCls : meta.pill.cls"
      >{{
        activeTool === 'delete'
          ? deletePillText
          : activeTool === 'outer'
          ? outerPillText
          : activeTool === 'offset'
          ? offsetPillText
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

      <!-- ========== chamfer ========== -->
      <template v-else-if="activeTool === 'chamfer'">
        <div class="section-block">
          <p class="lead">
            外径の <em>角</em> または <em>辺</em> をキャンバスでクリックして指定します。出力時に注記として記載されます。
          </p>

          <div class="kv">
            <div><div class="k">C面サイズ</div><div class="ksub">CHAMFER-SIZE</div></div>
            <div class="num-step">
              <button>−</button>
              <input type="text" value="C2" />
              <span class="unit">×45°</span>
              <button>+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">開先角度</div><div class="ksub">BEVEL-ANGLE</div></div>
            <div class="num-step">
              <button>−</button>
              <input type="text" value="30" />
              <span class="unit">°</span>
              <button>+</button>
            </div>
          </div>
        </div>

        <div class="section-block">
          <h6 class="lbl">指定済みの角 <span class="right">クリック で 解除</span></h6>
          <div class="corner-list">
            <div class="corner-chip" :class="{ on: selectedCorners.has(0) }" @click="toggleCorner(0)"><span class="dot"></span>右上 · C2</div>
            <div class="corner-chip" :class="{ on: selectedCorners.has(1) }" @click="toggleCorner(1)"><span class="dot"></span>左上</div>
            <div class="corner-chip" :class="{ on: selectedCorners.has(2) }" @click="toggleCorner(2)"><span class="dot"></span>右下</div>
            <div class="corner-chip" :class="{ on: selectedCorners.has(3) }" @click="toggleCorner(3)"><span class="dot"></span>左下</div>
          </div>
        </div>

        <div
          class="summary"
          style="border-color:rgba(167,139,250,0.25);background:linear-gradient(180deg, rgba(167,139,250,0.04) 0%, rgba(167,139,250,0) 100%);"
        >
          <h6 style="color:var(--chamfer)">DXF出力時の注記</h6>
          <div class="summary-row"><span class="k">右上 角部</span><span class="v" style="color:var(--chamfer)">C2 × 45°</span></div>
          <div class="summary-row"><span class="k">開先 (該当辺)</span><span class="v">なし</span></div>
        </div>

        <div class="action-row">
          <button class="action-btn">指定をクリア</button>
          <button class="action-btn cy"><svg><use href="#i-arrow-right" /></svg>出力へ</button>
        </div>
      </template>

      <!-- ========== dim ========== -->
      <template v-else-if="activeTool === 'dim'">
        <div class="section-block">
          <p class="lead">
            出力DXFに <em>注釈寸法</em> を残したい場合に使用します。「削除」で消した寸法とは別に、加工指示として必要な寸法だけを再付加します。
          </p>

          <div class="kv">
            <div><div class="k">スタイル</div><div class="ksub">DIM-STYLE</div></div>
            <span class="v">ISO 標準</span>
          </div>
          <div class="kv">
            <div><div class="k">小数桁</div><div class="ksub">PRECISION</div></div>
            <div class="num-step">
              <button>−</button>
              <input type="text" value="1" />
              <span class="unit">桁</span>
              <button>+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">矢印サイズ</div><div class="ksub">ARROW-SIZE</div></div>
            <span class="v">3.5 mm</span>
          </div>
        </div>

        <div class="placeholder-card">
          <div class="ic"><svg><use href="#i-dim" /></svg></div>
          <h5>寸法を追加するには</h5>
          <p>
            キャンバスで 2 点をクリックすると、その間の寸法線がここに表示されます。<br />
            キーボード <b style="color:var(--t-2)">D</b> で2点間モード。
          </p>
          <span class="meta">追加済み 0 件</span>
        </div>
      </template>

      <!-- ========== edit ========== -->
      <template v-else-if="activeTool === 'edit'">
        <div class="section-block">
          <p class="lead">
            外径や穴の <em>頂点・線分</em> を直接ドラッグして編集できます。スナップ・寸法表示は自動で有効になります。
          </p>

          <div class="kv">
            <div><div class="k">スナップ</div><div class="ksub">SNAP</div></div>
            <span class="v">端点 + 中点 + 交点</span>
          </div>
          <div class="kv">
            <div><div class="k">グリッド吸着</div><div class="ksub">GRID-SNAP</div></div>
            <span class="v">1 mm</span>
          </div>
          <div class="kv">
            <div><div class="k">直交モード</div><div class="ksub">ORTHO</div></div>
            <span class="v">OFF (Shift)</span>
          </div>
        </div>

        <div class="placeholder-card">
          <div class="ic"><svg><use href="#i-edit-line" /></svg></div>
          <h5>線を選択してください</h5>
          <p>
            キャンバス上の <b style="color:var(--t-2)">頂点</b> または <b style="color:var(--t-2)">線分</b>
            をクリックすると、ここに座標・長さ・角度が表示され、ドラッグで編集できます。
          </p>
          <span class="meta">選択 0 / 162 entities</span>
        </div>
      </template>

      <!-- ========== hole ========== -->
      <template v-else-if="activeTool === 'hole'">
        <div class="section-block">
          <p class="lead">
            外径の内側の任意位置に <em>穴を追加</em> します。座標指定 / クリック配置 / 整列パターン に対応。
          </p>

          <div class="kv">
            <div><div class="k">穴径</div><div class="ksub">DIAMETER</div></div>
            <div class="num-step">
              <button>−</button>
              <input type="text" value="φ9.0" />
              <span class="unit">mm</span>
              <button>+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">配置方式</div><div class="ksub">PLACEMENT</div></div>
            <span class="v">クリックで配置</span>
          </div>
          <div class="kv">
            <div><div class="k">タップ指示</div><div class="ksub">TAP-NOTE</div></div>
            <span class="v">なし</span>
          </div>
        </div>

        <div class="placeholder-card">
          <div class="ic"><svg><use href="#i-hole-add" /></svg></div>
          <h5>キャンバスをクリックして配置</h5>
          <p>
            カーソル位置に <b style="color:var(--t-2)">φ9.0</b> の穴が追加されます。<br />
            連続配置: Shift+クリック、整列パターン: <b style="color:var(--t-2)">A</b>
          </p>
          <span class="meta">追加済み 0 件</span>
        </div>
      </template>

      <!-- ========== note ========== -->
      <template v-else-if="activeTool === 'note'">
        <div class="section-block">
          <p class="lead">
            部品単位の <em>加工指示</em> (溶接記号・面粗さ・熱処理 等) を文字注記として残します。
          </p>

          <div class="kv">
            <div><div class="k">プリセット</div><div class="ksub">NOTE-PRESET</div></div>
            <span class="v">面粗さ / 溶接 / 一般</span>
          </div>
          <div class="kv">
            <div><div class="k">フォント</div><div class="ksub">FONT</div></div>
            <span class="v">isocp · 2.5 mm</span>
          </div>
        </div>

        <div class="placeholder-card">
          <div class="ic"><svg><use href="#i-note" /></svg></div>
          <h5>注記はまだありません</h5>
          <p>
            キャンバス上で右クリック → <b style="color:var(--t-2)">「注記を追加」</b> または
            <b style="color:var(--t-2)">T</b> キーで挿入。
          </p>
          <span class="meta">注記 0 件</span>
        </div>
      </template>

      <!-- ========== bridge ========== -->
      <template v-else-if="activeTool === 'bridge'">
        <div class="section-block">
          <p class="lead">
            レーザ・プラズマ加工で部品が脱落しないよう、外径に <em>ブリッジ(保持タブ)</em> を残します。出力時に切断パスが分断されます。
          </p>

          <div class="kv">
            <div><div class="k">ブリッジ幅</div><div class="ksub">BRIDGE-WIDTH</div></div>
            <div class="num-step">
              <button>−</button>
              <input type="text" value="2.0" />
              <span class="unit">mm</span>
              <button>+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">推奨個数</div><div class="ksub">AUTO-COUNT</div></div>
            <span class="v">4 (重量より算出)</span>
          </div>
          <div class="kv">
            <div><div class="k">配置方式</div><div class="ksub">PLACEMENT</div></div>
            <span class="v">等間隔 (自動)</span>
          </div>
        </div>

        <div class="placeholder-card">
          <div class="ic"><svg><use href="#i-bridge" /></svg></div>
          <h5>外径をクリックして配置</h5>
          <p>
            キャンバス上の外径線をクリックすると、その位置に <b style="color:var(--t-2)">2.0 mm</b> のブリッジが残ります。
          </p>
          <span class="meta">配置 0 / 推奨 4</span>
        </div>
      </template>
    </div>
  </aside>
</template>
