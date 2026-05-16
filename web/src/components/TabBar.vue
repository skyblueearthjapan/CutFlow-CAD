<script setup lang="ts">
// 下部DXFタブバー (30px)。Phase 0 では mockup と同じハードコードデータで描画。
// Phase 1 で実 DXF セッションのファイル一覧と差し替える。
interface DxfTab {
  no: string;
  name: string;
  state: 'clean' | 'dirty' | 'todo';
  active?: boolean;
}

const tabs: DxfTab[] = [
  { no: '01', name: 'ベースフレーム',     state: 'clean' },
  { no: '02', name: '本体フレーム',       state: 'clean' },
  { no: '03', name: 'センタープレート',   state: 'dirty', active: true },
  { no: '04', name: '減速機サポート',     state: 'todo' },
  { no: '05', name: 'ピン受け 上',        state: 'clean' },
  { no: '06', name: 'ピン受け 下',        state: 'clean' },
];

function tsClass(state: DxfTab['state']) {
  if (state === 'dirty') return 'ts dirty';
  if (state === 'todo') return 'ts todo';
  return 'ts';
}
</script>

<template>
  <div class="tabs">
    <div
      v-for="t in tabs"
      :key="t.no"
      class="tab"
      :class="{ active: t.active }"
    >
      <span :class="tsClass(t.state)"></span>
      <span class="tn">{{ t.no }}</span>
      {{ t.name }}
    </div>
    <div class="tab-add"><svg><use href="#i-plus" /></svg>追加</div>
  </div>
</template>
