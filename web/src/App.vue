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
import { useSession } from './stores/session';
// raw 文字列として SVG スプライトを取り込む (xmlns 付きの完全な <svg>)
import iconSprite from './assets/icons.svg?raw';

const { activeTool, setTool } = useActiveTool();
const {
  setDimTwoPointMode,
  dimTwoPointMode,
  setHolePatternOpen,
  holePatternOpen,
  setNotePendingAnchor,
  notePendingAnchor,
  setEditOrtho,
  selectPreviousFile,
  selectNextFile,
} = useSession();

function onKey(e: KeyboardEvent) {
  // INPUT/TEXTAREA / contentEditable フォーカス中は無視 (数値入力を妨げない)
  const target = e.target as HTMLElement | null;
  const tag = target?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || target?.isContentEditable) return;
  // 矢印キーは修飾キーなしの単独押下のみ前後DXF切替に使う。
  // (Shift は edit ortho など別用途で使うため矢印では弾かない)
  if (e.key === 'ArrowLeft' && !e.ctrlKey && !e.altKey && !e.metaKey) {
    e.preventDefault();
    void selectPreviousFile();
    return;
  }
  if (e.key === 'ArrowRight' && !e.ctrlKey && !e.altKey && !e.metaKey) {
    e.preventDefault();
    void selectNextFile();
    return;
  }
  const mode = keyToMode[e.key];
  if (mode) {
    setTool(mode);
    return;
  }
  // Phase 4 — mode-specific letter shortcuts.
  if (e.key === 'D' || e.key === 'd') {
    if (activeTool.value === 'dim') setDimTwoPointMode(!dimTwoPointMode.value);
    return;
  }
  if (e.key === 'A' || e.key === 'a') {
    if (activeTool.value === 'hole') setHolePatternOpen(!holePatternOpen.value);
    return;
  }
  if (e.key === 'T' || e.key === 't') {
    if (activeTool.value === 'note' && !notePendingAnchor.value) {
      // Open the modal anchored at the origin so the user can type immediately
      // without having to click first. The canvas click handler still works
      // for spatial placement.
      setNotePendingAnchor([0, 0]);
    }
    return;
  }
  if (e.key === 'Shift') {
    if (activeTool.value === 'edit') setEditOrtho(true);
    return;
  }
  if (e.key === 'Escape') {
    if (activeTool.value === 'dim' && dimTwoPointMode.value) setDimTwoPointMode(false);
    if (activeTool.value === 'note' && notePendingAnchor.value) setNotePendingAnchor(null);
    if (activeTool.value === 'hole' && holePatternOpen.value) setHolePatternOpen(false);
  }
}

function onKeyUp(e: KeyboardEvent) {
  if (e.key === 'Shift' && activeTool.value === 'edit') setEditOrtho(false);
}

onMounted(() => {
  document.addEventListener('keydown', onKey);
  document.addEventListener('keyup', onKeyUp);
});

onUnmounted(() => {
  document.removeEventListener('keydown', onKey);
  document.removeEventListener('keyup', onKeyUp);
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
