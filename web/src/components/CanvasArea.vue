<script setup lang="ts">
/**
 * Canvas area.
 *
 * - When **no DXF is loaded** the v3 mockup SVG content is rendered verbatim
 *   so the design lives untouched at startup (1px-perfect with v3).
 * - When a DXF **is** loaded, the inner SVG body is replaced with real
 *   entities from the session store. The viewBox is derived from the file's
 *   bounding_box, and we wrap entities in `<g transform="...scale(1,-1)">`
 *   to flip DXF's Y-up coordinate system into SVG's Y-down.
 * - Entity clicks (in delete mode) toggle membership of `selectedForDelete`.
 *
 * The surrounding chrome — floating toolbar, banner, bottom-left meta and
 * bottom-right summary — is preserved in both states so the v3 layout is
 * never disturbed.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useActiveTool } from '../stores/activeTool';
import { useSession } from '../stores/session';
import EntityRenderer from './EntityRenderer.vue';

const { showBanner, activeTool } = useActiveTool();
const {
  currentFile,
  visibleEntities,
  selectedForDelete,
  selectEntity,
  lastError,
  remainingAfterDelete,
  isLoadingFile,
  // Phase 2
  outerEntityIdSet,
  offsetResult,
  manualMode,
  manualSelection,
  addToManual,
} = useSession();

const cursorX = ref('412.0');
const cursorY = ref('218.5');
let timer: number | undefined;
let t = 0;
onMounted(() => {
  // v3 mockup keeps a slow cursor wobble for "live feel"; preserved.
  timer = window.setInterval(() => {
    t += 0.05;
    cursorX.value = (412 + Math.sin(t) * 0.4).toFixed(1);
    cursorY.value = (218.5 + Math.cos(t * 1.3) * 0.3).toFixed(1);
  }, 80);
});
onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer);
});

/** viewBox + Y-flip transform derived from the active file's bounding box. */
const viewBox = computed(() => {
  const f = currentFile.value;
  if (!f) return '0 0 1200 800';
  const bb = f.bounding_box;
  const w = bb.max_x - bb.min_x;
  const h = bb.max_y - bb.min_y;
  // Add 8% margin around the drawing.
  const mx = w * 0.08;
  const my = h * 0.08;
  return `${bb.min_x - mx} ${bb.min_y - my} ${w + 2 * mx} ${h + 2 * my}`;
});

/** Y-flip: translate by (max_y + min_y) so the flipped result is still in
 *  the bounding box, then scale(1,-1) to make Y point down on the screen. */
const flipTransform = computed(() => {
  const f = currentFile.value;
  if (!f) return '';
  const bb = f.bounding_box;
  return `translate(0 ${bb.max_y + bb.min_y}) scale(1 -1)`;
});

/** Y offset used by text entities to counter-flip the parent scale(1,-1).
 *  Equivalent to `max_y + min_y` above; exposed so EntityRenderer can
 *  produce upright TEXT/MTEXT (otherwise the parent scale mirrors them). */
const flipYBase = computed(() => {
  const f = currentFile.value;
  if (!f) return 0;
  return f.bounding_box.max_y + f.bounding_box.min_y;
});

/** When live data is showing, summary numbers come from the file stats.
 *  Counts use `visibleEntities` so server-deleted IDs don't inflate the total
 *  the user sees in the bottom-right HUD. */
const liveSummary = computed(() => {
  const f = currentFile.value;
  if (!f) return null;
  return {
    total: visibleEntities.value.length,
    remaining: remainingAfterDelete.value,
    selected: selectedForDelete.value.size,
  };
});

/** Whether the canvas should show live entities (vs the v3 demo). */
const hasFile = computed(() => currentFile.value !== null);

/** Banner shows either the manual one (outer) OR a session error. */
const showWarningBanner = computed(() => showBanner.value || !!lastError.value);
const bannerText = computed(() =>
  lastError.value
    ? lastError.value
    : '外径に閉ループ未確認の箇所が1ヶ所あります。手動で線を選択してください。',
);
const bannerTitle = computed(() => (lastError.value ? 'エラー' : '注意'));

/** Canvas-level click handler — propagates entity hits to the store.
 *  - delete mode: toggles the entity in/out of the delete selection.
 *  - outer mode + manual chain mode: appends the entity to the manual chain
 *    (clicking the most-recent entity again pops it for one-step undo). */
function onCanvasClick(e: MouseEvent) {
  if (activeTool.value !== 'delete' && !(activeTool.value === 'outer' && manualMode.value)) {
    return;
  }
  // Walk up from the click target to find the nearest element carrying a
  // data-entity-id attribute (the EntityRenderer puts it on the SVG element).
  let el: SVGElement | null = e.target as SVGElement;
  while (el && el instanceof SVGElement) {
    const id = el.getAttribute('data-entity-id');
    if (id) {
      if (activeTool.value === 'delete') {
        selectEntity(id);
      } else {
        addToManual(id);
      }
      return;
    }
    el = el.parentNode as SVGElement | null;
  }
}

/* -------------------- Phase 2 — outer / offset overlay ------------------- */

/** Set of ids currently selected via the manual chain — used by the renderer
 *  to apply the `.is-manual` highlight class. */
const manualSelectionSet = computed<Set<string>>(
  () => new Set(manualSelection.value),
);

/** SVG path `d` for the offset preview loop. The backend returns
 *  `[x, y, bulge]` triples; for the preview we draw bulge=0 segments only
 *  (full bulge handling lives in EntityRenderer for actual entities). */
const offsetPreviewPath = computed<string>(() => {
  const r = offsetResult.value;
  if (!r) return '';
  const verts = r.offset_loop.vertices;
  if (!verts || verts.length === 0) return '';
  let d = `M ${verts[0][0]} ${verts[0][1]}`;
  for (let i = 1; i < verts.length; i++) {
    d += ` L ${verts[i][0]} ${verts[i][1]}`;
  }
  if (r.offset_loop.closed) d += ' Z';
  return d;
});

/** Show the offset preview overlay only when in offset mode and we have a
 *  computed loop (avoids a stale preview leaking into other tools). */
const showOffsetPreview = computed(
  () => activeTool.value === 'offset' && !!offsetPreviewPath.value,
);

/** "Fit" button — for now just resets to the bounding-box viewBox (no zoom
 *  state to clear yet). Kept as a stub so the button is wired and visible. */
function onFit() {
  // No-op until pan/zoom lands in Phase 2; viewBox already follows the file.
}
</script>

<template>
  <section class="canvas-area">
    <!-- floating toolbar (kept 1:1 with v3) -->
    <div class="c-tools">
      <button title="パン (Space)"><svg><use href="#i-pan" /></svg></button>
      <button class="active" title="選択 (V)"><svg><use href="#i-select" /></svg></button>
      <div class="sep"></div>
      <button title="ズームアウト">−</button>
      <span class="zv">82%</span>
      <button title="ズームイン">+</button>
      <button title="全体表示 (F)" @click="onFit"><svg><use href="#i-fit" /></svg></button>
      <div class="sep"></div>
      <button title="元に戻す (⌘Z)"><svg><use href="#i-undo" /></svg></button>
      <button title="やり直し (⌘⇧Z)"><svg><use href="#i-redo" /></svg></button>
    </div>

    <div class="c-banner" :class="{ show: showWarningBanner }">
      <svg><use href="#i-warning" /></svg>
      <span><b>{{ bannerTitle }}:</b> {{ bannerText }}</span>
    </div>

    <!-- CANVAS SVG -->
    <svg
      class="canvas-svg"
      :viewBox="viewBox"
      preserveAspectRatio="xMidYMid meet"
      @click="onCanvasClick"
    >
      <defs>
        <marker id="arr-am" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M0,0 L10,5 L0,10 z" fill="var(--am)" />
        </marker>
      </defs>

      <!-- ========== LIVE FILE PATH ========== -->
      <template v-if="hasFile">
        <!-- Y-flip wrapper: DXF is Y-up, SVG is Y-down. The list is
             `visibleEntities` so server-deleted ids never render — see
             stores/session.ts. -->
        <g :transform="flipTransform">
          <EntityRenderer
            v-for="ent in visibleEntities"
            :key="ent.id"
            :entity="ent"
            :selected="selectedForDelete.has(ent.id)"
            :is-outer="outerEntityIdSet.has(ent.id)"
            :is-manual="manualSelectionSet.has(ent.id)"
            :flip-y-base="flipYBase"
          />

          <!-- Offset preview: dashed cyan loop drawn outside the outer.
               `class="ent offset"` reuses the v3 dashed style (4dcfe0,
               stroke-dasharray 5 4); the fill rectangle uses .offset-fill
               so the body[data-mode="offset"] CSS keeps it subtle. -->
          <template v-if="showOffsetPreview">
            <path class="offset-fill" :d="offsetPreviewPath" />
            <path class="ent offset" :d="offsetPreviewPath" />
          </template>
        </g>
      </template>

      <!-- ========== EMPTY-STATE: v3 demo SVG (unchanged) ========== -->
      <template v-else>
        <!-- Origin -->
        <g transform="translate(220, 660)">
          <line x1="0" y1="0" x2="18" y2="0" stroke="var(--cy)" stroke-width="1" />
          <line x1="0" y1="0" x2="0" y2="-18" stroke="var(--cy)" stroke-width="1" />
          <circle cx="0" cy="0" r="2.5" fill="var(--cy)" />
          <text x="-6" y="14" font-family="IBM Plex Mono" font-size="9" fill="var(--cy)" text-anchor="end">0,0</text>
        </g>

        <!-- Offset preview -->
        <path
          class="offset-fill"
          d="M 196 167 L 856 167 Q 893 167 893 204 L 893 596 Q 893 633 856 633 L 196 633 Q 160 633 160 596 L 160 204 Q 160 167 196 167 Z"
          fill="rgba(77,207,224,0.06)"
        />
        <path
          class="ent offset"
          d="M 196 167 L 856 167 Q 893 167 893 204 L 893 596 Q 893 633 856 633 L 196 633 Q 160 633 160 596 L 160 204 Q 160 167 196 167 Z"
        />

        <!-- Outer -->
        <path
          class="ent outer"
          d="M 220 200 L 840 200 Q 860 200 860 220 L 860 580 Q 860 600 840 600 L 220 600 Q 200 600 200 580 L 200 220 Q 200 200 220 200 Z"
        />
        <path
          class="outer-anim"
          d="M 220 200 L 840 200 Q 860 200 860 220 L 860 580 Q 860 600 840 600 L 220 600 Q 200 600 200 580 L 200 220 Q 200 200 220 200 Z"
        />

        <!-- Chamfer -->
        <path class="ent chamfer" d="M 845 200 L 860 215" />
        <g class="ent chamfer-glyph">
          <line x1="855" y1="195" x2="865" y2="195" />
          <text x="870" y="208" font-family="IBM Plex Mono" font-size="10" fill="var(--chamfer)" stroke="none">C2</text>
        </g>

        <!-- Holes -->
        <circle class="ent hole" cx="290" cy="290" r="14" />
        <circle class="ent hole" cx="770" cy="290" r="14" />
        <circle class="ent hole" cx="290" cy="510" r="14" />
        <circle class="ent hole" cx="770" cy="510" r="14" />
        <circle class="ent hole" cx="530" cy="400" r="40" />
        <g stroke="var(--cy)" stroke-width="0.6" opacity="0.4">
          <line x1="285" y1="290" x2="295" y2="290" /><line x1="290" y1="285" x2="290" y2="295" />
          <line x1="765" y1="290" x2="775" y2="290" /><line x1="770" y1="285" x2="770" y2="295" />
          <line x1="285" y1="510" x2="295" y2="510" /><line x1="290" y1="505" x2="290" y2="515" />
          <line x1="765" y1="510" x2="775" y2="510" /><line x1="770" y1="505" x2="770" y2="515" />
          <line x1="520" y1="400" x2="540" y2="400" /><line x1="530" y1="390" x2="530" y2="410" />
        </g>
        <text class="lbl" x="304" y="280" fill="rgba(77,207,224,0.5)">φ9</text>
        <text class="lbl" x="784" y="280" fill="rgba(77,207,224,0.5)">φ9</text>
        <text class="lbl" x="304" y="500" fill="rgba(77,207,224,0.5)">φ9</text>
        <text class="lbl" x="784" y="500" fill="rgba(77,207,224,0.5)">φ9</text>
        <text class="lbl" x="558" y="378" fill="rgba(77,207,224,0.5)">φ80</text>

        <!-- Taps -->
        <g><circle class="ent tap" cx="430" cy="250" r="6" /><text class="lbl-am" x="440" y="246">M8</text></g>
        <g><circle class="ent tap" cx="630" cy="250" r="6" /><text class="lbl-am" x="640" y="246">M8</text></g>
        <g><circle class="ent tap" cx="430" cy="550" r="6" /><text class="lbl-am" x="440" y="546">M8</text></g>
        <g><circle class="ent tap" cx="630" cy="550" r="6" /><text class="lbl-am" x="640" y="546">M8</text></g>

        <!-- Dimensions -->
        <g>
          <line class="ent dim" x1="200" y1="660" x2="860" y2="660" marker-end="url(#arr-am)" marker-start="url(#arr-am)" />
          <line class="ent dim" x1="200" y1="650" x2="200" y2="670" />
          <line class="ent dim" x1="860" y1="650" x2="860" y2="670" />
          <text class="lbl-am" x="530" y="678" text-anchor="middle">440</text>
        </g>
        <g>
          <line class="ent dim" x1="130" y1="200" x2="130" y2="600" marker-end="url(#arr-am)" marker-start="url(#arr-am)" />
          <line class="ent dim" x1="120" y1="200" x2="140" y2="200" />
          <line class="ent dim" x1="120" y1="600" x2="140" y2="600" />
          <text class="lbl-am" x="118" y="404" text-anchor="end">280</text>
        </g>
        <g>
          <line class="ent dim" x1="530" y1="400" x2="690" y2="180" />
          <line class="ent dim" x1="690" y1="180" x2="730" y2="180" />
          <text class="lbl-am" x="734" y="178">φ80</text>
        </g>
        <g>
          <line class="ent dim" x1="290" y1="120" x2="770" y2="120" marker-end="url(#arr-am)" marker-start="url(#arr-am)" />
          <line class="ent dim" x1="290" y1="110" x2="290" y2="130" />
          <line class="ent dim" x1="770" y1="110" x2="770" y2="130" />
          <text class="lbl-am" x="530" y="138" text-anchor="middle">480</text>
        </g>

        <!-- Balloons -->
        <g>
          <line class="ent dim" x1="290" y1="290" x2="170" y2="100" />
          <circle class="balloon-circle" cx="160" cy="92" r="14" />
          <text class="lbl-am" x="160" y="96" text-anchor="middle" font-size="10" font-weight="600">1</text>
        </g>
        <g>
          <line class="ent dim" x1="530" y1="400" x2="950" y2="320" />
          <circle class="balloon-circle" cx="966" cy="316" r="14" />
          <text class="lbl-am" x="966" y="320" text-anchor="middle" font-size="10" font-weight="600">2</text>
        </g>

        <!-- Title frame -->
        <g>
          <rect class="ent frame" x="80" y="70" width="1040" height="660" rx="2" />
          <rect class="ent frame" x="900" y="700" width="220" height="30" />
          <line class="ent frame" x1="900" y1="715" x2="1120" y2="715" />
          <line class="ent frame" x1="1010" y1="700" x2="1010" y2="730" />
          <text class="lbl-am" x="908" y="711" font-size="9">25057-P1-03 センタープレート</text>
          <text class="lbl-am" x="908" y="726" font-size="9">SS400 t9</text>
          <text class="lbl-am" x="1018" y="711" font-size="9">SCALE 1:1</text>
          <text class="lbl-am" x="1018" y="726" font-size="9">REV. 03</text>
        </g>

        <!-- Cut sequence nodes -->
        <g class="cut-node"><circle cx="220" cy="200" r="9" /><text x="220" y="203" text-anchor="middle">1</text></g>
        <g class="cut-node"><circle cx="290" cy="290" r="8" /><text x="290" y="293" text-anchor="middle">2</text></g>
        <g class="cut-node"><circle cx="770" cy="290" r="8" /><text x="770" y="293" text-anchor="middle">3</text></g>
        <g class="cut-node"><circle cx="530" cy="400" r="8" /><text x="530" y="403" text-anchor="middle">4</text></g>
        <g class="cut-node"><circle cx="290" cy="510" r="8" /><text x="290" y="513" text-anchor="middle">5</text></g>
        <g class="cut-node"><circle cx="770" cy="510" r="8" /><text x="770" y="513" text-anchor="middle">6</text></g>
      </template>
    </svg>

    <!-- bottom-left: meta -->
    <div class="c-meta">
      <span>x <b>{{ cursorX }}</b></span>
      <span>y <b>{{ cursorY }}</b></span>
      <span class="sep">·</span>
      <span class="cy">1 : 1</span>
      <span>mm</span>
      <template v-if="isLoadingFile">
        <span class="sep">·</span>
        <span class="cy">読込中…</span>
      </template>
    </div>

    <!-- bottom-right: live summary -->
    <div class="c-summary">
      <template v-if="liveSummary">
        <div class="item">
          <span class="lbl">エンティティ</span>
          <span class="val">{{ liveSummary.total }}</span>
        </div>
        <div class="sep"></div>
        <div class="item">
          <span class="lbl">選択中</span>
          <span class="val">{{ liveSummary.selected }}</span>
        </div>
        <div class="sep"></div>
        <div class="item time">
          <span class="lbl">削除後</span>
          <span class="val">{{ liveSummary.remaining }}</span>
        </div>
      </template>
      <template v-else>
        <div class="item">
          <span class="lbl">外周</span>
          <span class="val">1,847<span class="u">mm</span></span>
        </div>
        <div class="sep"></div>
        <div class="item">
          <span class="lbl">ピアス</span>
          <span class="val">5</span>
        </div>
        <div class="sep"></div>
        <div class="item time">
          <span class="lbl">推定加工</span>
          <span class="val">02:47</span>
        </div>
      </template>
    </div>
  </section>
</template>
