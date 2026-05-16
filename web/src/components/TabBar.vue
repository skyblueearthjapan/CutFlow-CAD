<script setup lang="ts">
/**
 * Bottom DXF tab bar (30px).
 *
 * Driven by the session store. When no session is loaded the v3 demo tabs
 * are shown so the empty-state matches the mockup exactly. The "追加" tab
 * delegates to the session store's `openFilePicker()` (M4) — Header.vue
 * registers the actual `<input>` ref on mount, so this component no longer
 * needs to query the DOM.
 *
 * The numeric prefix (`tn`) is derived from the file index; the file name
 * is shown verbatim (extension stripped for display).
 */
import { computed } from 'vue';
import { useSession } from '../stores/session';

const { currentSession, currentFileId, selectFile, openFilePicker } = useSession();

interface UiTab {
  fid: string;
  no: string;
  name: string;
  state: 'clean' | 'dirty' | 'todo';
  active: boolean;
}

const demoTabs: UiTab[] = [
  { fid: 'demo-1', no: '01', name: 'ベースフレーム',   state: 'clean', active: false },
  { fid: 'demo-2', no: '02', name: '本体フレーム',     state: 'clean', active: false },
  { fid: 'demo-3', no: '03', name: 'センタープレート', state: 'dirty', active: true  },
  { fid: 'demo-4', no: '04', name: '減速機サポート',   state: 'todo',  active: false },
  { fid: 'demo-5', no: '05', name: 'ピン受け 上',      state: 'clean', active: false },
  { fid: 'demo-6', no: '06', name: 'ピン受け 下',      state: 'clean', active: false },
];

const tabs = computed<UiTab[]>(() => {
  const session = currentSession.value;
  if (!session) return demoTabs;
  return session.files.map((f, i) => ({
    fid: f.file_id,
    no: String(i + 1).padStart(2, '0'),
    name: f.name.replace(/\.[Dd][Xx][Ff]$/, ''),
    state: f.status === 'error' ? 'todo' : 'clean',
    active: f.file_id === currentFileId.value,
  }));
});

function tsClass(state: UiTab['state']) {
  if (state === 'dirty') return 'ts dirty';
  if (state === 'todo') return 'ts todo';
  return 'ts';
}

function onTabClick(t: UiTab) {
  // Demo tabs (no session) are inert.
  if (!currentSession.value) return;
  if (t.fid === currentFileId.value) return;
  void selectFile(t.fid);
}

function onAdd() {
  // Re-use the header's file picker via the store-shared opener (registered
  // by Header.vue on mount). No DOM querying needed (M4).
  openFilePicker();
}
</script>

<template>
  <div class="tabs">
    <div
      v-for="t in tabs"
      :key="t.fid"
      class="tab"
      :class="{ active: t.active }"
      @click="onTabClick(t)"
    >
      <span :class="tsClass(t.state)"></span>
      <span class="tn">{{ t.no }}</span>
      {{ t.name }}
    </div>
    <div class="tab-add" @click="onAdd"><svg><use href="#i-plus" /></svg>追加</div>
  </div>
</template>
