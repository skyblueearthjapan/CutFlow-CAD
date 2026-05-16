<script setup lang="ts">
// キャンバス領域 (flex)。
// Phase 0 では mockup の SVG コンテンツをそのまま静的に再現する
// (本物の DXF 描画は Phase 1)。これにより `body[data-mode]` の
// CSS セレクタで色味が切り替わる挙動も同時に確認できる。
import { onMounted, onUnmounted, ref } from 'vue';
import { useActiveTool } from '../stores/activeTool';

const { showBanner } = useActiveTool();

const cursorX = ref('412.0');
const cursorY = ref('218.5');

let timer: number | undefined;
let t = 0;

onMounted(() => {
  // mockup と同じカーソル wobble (live feel 用)。
  timer = window.setInterval(() => {
    t += 0.05;
    cursorX.value = (412 + Math.sin(t) * 0.4).toFixed(1);
    cursorY.value = (218.5 + Math.cos(t * 1.3) * 0.3).toFixed(1);
  }, 80);
});

onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer);
});
</script>

<template>
  <section class="canvas-area">
    <!-- floating toolbar -->
    <div class="c-tools">
      <button title="パン (Space)"><svg><use href="#i-pan" /></svg></button>
      <button class="active" title="選択 (V)"><svg><use href="#i-select" /></svg></button>
      <div class="sep"></div>
      <button title="ズームアウト">−</button>
      <span class="zv">82%</span>
      <button title="ズームイン">+</button>
      <button title="全体表示 (F)"><svg><use href="#i-fit" /></svg></button>
      <div class="sep"></div>
      <button title="元に戻す (⌘Z)"><svg><use href="#i-undo" /></svg></button>
      <button title="やり直し (⌘⇧Z)"><svg><use href="#i-redo" /></svg></button>
    </div>

    <div class="c-banner" :class="{ show: showBanner }">
      <svg><use href="#i-warning" /></svg>
      <span><b>注意:</b> 外径に閉ループ未確認の箇所が1ヶ所あります。手動で線を選択してください。</span>
    </div>

    <!-- CANVAS SVG (Phase 0: 静的プレビュー。Phase 1 で実 DXF 描画に置換) -->
    <svg class="canvas-svg" viewBox="0 0 1200 800" preserveAspectRatio="xMidYMid meet">
      <defs>
        <marker id="arr-am" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M0,0 L10,5 L0,10 z" fill="var(--am)" />
        </marker>
      </defs>

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
    </svg>

    <!-- bottom-left: meta -->
    <div class="c-meta">
      <span>x <b>{{ cursorX }}</b></span>
      <span>y <b>{{ cursorY }}</b></span>
      <span class="sep">·</span>
      <span class="cy">1 : 1</span>
      <span>mm</span>
    </div>

    <!-- bottom-right: live summary -->
    <div class="c-summary">
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
    </div>
  </section>
</template>
