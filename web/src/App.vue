<script setup lang="ts">
// ルートレイアウト。mockup の .app グリッド (44 / 1fr+(64/1fr/340) / 30 / 22) を再現。
// アイコンスプライトは ?raw import で文字列取得し、v-html で DOM に注入する。
import { onMounted, onUnmounted } from 'vue';
import Header from './components/Header.vue';
import ToolRail from './components/ToolRail.vue';
import CanvasArea from './components/CanvasArea.vue';
import Inspector from './components/Inspector.vue';
import TabBar from './components/TabBar.vue';
import StatusBar from './components/StatusBar.vue';
import { useActiveTool, keyToMode } from './stores/activeTool';
// raw 文字列として SVG スプライトを取り込む (xmlns 付きの完全な <svg>)
import iconSprite from './assets/icons.svg?raw';

const { setTool } = useActiveTool();

function onKey(e: KeyboardEvent) {
  // INPUT/TEXTAREA フォーカス中は無視 (数値入力を妨げない)
  const tag = (e.target as HTMLElement | null)?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;
  const mode = keyToMode[e.key];
  if (mode) setTool(mode);
}

onMounted(() => {
  document.addEventListener('keydown', onKey);
});

onUnmounted(() => {
  document.removeEventListener('keydown', onKey);
});
</script>

<template>
  <!-- icon sprite (defs/symbol) を一度だけ DOM に注入 -->
  <div v-html="iconSprite" aria-hidden="true"></div>

  <div class="app">
    <Header />
    <div class="main">
      <ToolRail />
      <CanvasArea />
      <Inspector />
    </div>
    <TabBar />
    <StatusBar />
  </div>
</template>
