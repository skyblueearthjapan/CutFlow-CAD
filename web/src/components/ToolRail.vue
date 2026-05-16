<script setup lang="ts">
// 左ツールレール (64px)。9ツール3グループ(編集/作図/出力) + ネスティング(SOON)。
// クリックで activeTool を切替、`body[data-mode]` は store の watch が同期。
import { computed } from 'vue';
import { useActiveTool } from '../stores/activeTool';
import type { ToolMode } from '../types/tools';

interface RailItem {
  mode: ToolMode;
  icon: string;
  label: string;
  key: string;
  badge?: string;
}

const editTools: RailItem[] = [
  { mode: 'outer',   icon: 'i-shape',   label: '外形',   key: '1' },
  { mode: 'delete',  icon: 'i-delete',  label: '削除',   key: '2', badge: '12' },
  { mode: 'offset',  icon: 'i-offset',  label: '加工代', key: '3' },
  { mode: 'chamfer', icon: 'i-chamfer', label: 'C面',    key: '4' },
];

const drawTools: RailItem[] = [
  { mode: 'dim',  icon: 'i-dim',       label: '寸法',   key: '5' },
  { mode: 'edit', icon: 'i-edit-line', label: '線編集', key: '6' },
  { mode: 'hole', icon: 'i-hole-add',  label: '穴追加', key: '7' },
  { mode: 'note', icon: 'i-note',      label: '注記',   key: '8' },
];

const outputTools: RailItem[] = [
  { mode: 'bridge', icon: 'i-bridge', label: 'ブリッジ', key: '9' },
];

const { activeTool, setTool } = useActiveTool();
const current = computed(() => activeTool.value);
</script>

<template>
  <aside class="tool-rail" id="toolRail">
    <div class="rail-group-label">編集</div>
    <button
      v-for="t in editTools"
      :key="t.mode"
      class="rail-tool"
      :class="{ active: current === t.mode }"
      :data-mode="t.mode"
      @click="setTool(t.mode)"
    >
      <svg><use :href="`#${t.icon}`" /></svg>
      <span>{{ t.label }}</span>
      <span class="rt-key">{{ t.key }}</span>
      <span v-if="t.badge" class="rt-badge">{{ t.badge }}</span>
    </button>

    <div class="rail-sep"></div>
    <div class="rail-group-label">作図</div>
    <button
      v-for="t in drawTools"
      :key="t.mode"
      class="rail-tool"
      :class="{ active: current === t.mode }"
      :data-mode="t.mode"
      @click="setTool(t.mode)"
    >
      <svg><use :href="`#${t.icon}`" /></svg>
      <span>{{ t.label }}</span>
      <span class="rt-key">{{ t.key }}</span>
    </button>

    <div class="rail-sep"></div>
    <div class="rail-group-label">出力</div>
    <button
      v-for="t in outputTools"
      :key="t.mode"
      class="rail-tool"
      :class="{ active: current === t.mode }"
      :data-mode="t.mode"
      @click="setTool(t.mode)"
    >
      <svg><use :href="`#${t.icon}`" /></svg>
      <span>{{ t.label }}</span>
      <span class="rt-key">{{ t.key }}</span>
    </button>
    <button class="rail-tool soon" disabled>
      <svg><use href="#i-nest" /></svg>
      <span>ネスティング</span>
      <span class="rt-soon">SOON</span>
    </button>
  </aside>
</template>
