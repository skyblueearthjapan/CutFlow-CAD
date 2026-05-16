<script setup lang="ts">
/**
 * Bottom DXF tab bar (30px).
 *
 * Driven by the session store. The "ファイル / フォルダ" buttons delegate
 * to the store-shared openFilePicker/openFolderPicker (registered by
 * Header.vue on mount), so this component never queries the DOM.
 *
 * 検索ボタン: 左端の虫眼鏡をクリックすると input が展開し、ファイル名
 * 部分一致でタブをフィルタする (ESC で閉じる)。
 */
import { computed, nextTick, ref } from 'vue';
import { useSession } from '../stores/session';

const {
  currentSession,
  currentFileId,
  selectFile,
  openFilePicker,
  openFolderPicker,
} = useSession();

interface UiTab {
  fid: string;
  no: string;
  name: string;
  state: 'clean' | 'dirty' | 'todo';
  active: boolean;
}

/* -------------------- 検索 -------------------- */
const searchOpen = ref(false);
const searchQuery = ref('');
const searchInput = ref<HTMLInputElement | null>(null);

async function toggleSearch(): Promise<void> {
  searchOpen.value = !searchOpen.value;
  if (searchOpen.value) {
    await nextTick();
    searchInput.value?.focus();
  } else {
    searchQuery.value = '';
  }
}

function onSearchKey(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    searchOpen.value = false;
    searchQuery.value = '';
    (e.target as HTMLInputElement).blur();
  }
}

/* -------------------- タブリスト -------------------- */
const tabs = computed<UiTab[]>(() => {
  const session = currentSession.value;
  if (!session) return [];
  const q = searchQuery.value.trim().toLowerCase();
  const all = session.files.map((f, i) => ({
    fid: f.file_id,
    no: String(i + 1).padStart(2, '0'),
    name: f.name.replace(/\.[Dd][Xx][Ff]$/, ''),
    state: f.status === 'error' ? 'todo' : 'clean',
    active: f.file_id === currentFileId.value,
  } as UiTab));
  if (!q) return all;
  return all.filter((t) =>
    t.name.toLowerCase().includes(q) || t.no.includes(q),
  );
});

const totalCount = computed(() => currentSession.value?.files.length ?? 0);

function tsClass(state: UiTab['state']) {
  if (state === 'dirty') return 'ts dirty';
  if (state === 'todo') return 'ts todo';
  return 'ts';
}

function onTabClick(t: UiTab) {
  if (!currentSession.value) return;
  if (t.fid === currentFileId.value) return;
  void selectFile(t.fid);
}
</script>

<template>
  <div class="tabs">
    <!-- 検索 -->
    <div class="tab-search" :class="{ open: searchOpen }">
      <button
        class="search-btn"
        :title="searchOpen ? '検索を閉じる (Esc)' : 'ファイル名で検索'"
        @click="toggleSearch"
      >
        <svg><use href="#i-search" /></svg>
      </button>
      <input
        v-if="searchOpen"
        ref="searchInput"
        v-model="searchQuery"
        type="text"
        placeholder="ファイル名で検索…"
        @keydown="onSearchKey"
      />
      <span v-if="searchOpen && searchQuery" class="search-count">
        {{ tabs.length }} / {{ totalCount }}
      </span>
    </div>

    <!-- 空状態プレースホルダ -->
    <div v-if="!currentSession" class="tab-empty">
      <svg><use href="#i-plus" /></svg>
      DXF をアップロードしてください (右の「ファイル」/「フォルダ」ボタンから)
    </div>

    <!-- 検索ヒットゼロ -->
    <div v-else-if="tabs.length === 0 && searchQuery" class="tab-empty">
      該当するファイルがありません
    </div>

    <!-- タブ群 -->
    <div
      v-for="t in tabs"
      :key="t.fid"
      class="tab"
      :class="{ active: t.active }"
      :title="t.name"
      @click="onTabClick(t)"
    >
      <span :class="tsClass(t.state)"></span>
      <span class="tn">{{ t.no }}</span>
      {{ t.name }}
    </div>

    <!-- 追加ボタン (ファイル / フォルダ) -->
    <div class="tab-add" @click="openFilePicker" title="DXFファイルを追加">
      <svg><use href="#i-plus" /></svg>ファイル
    </div>
    <div class="tab-add" @click="openFolderPicker" title="フォルダ単位で取り込み">
      <svg><use href="#i-plus" /></svg>フォルダ
    </div>
  </div>
</template>

<style scoped>
/* 検索エリア */
.tab-search {
  display: flex;
  align-items: center;
  height: 100%;
  border-right: 1px solid var(--line-2);
  padding: 0 6px;
  gap: 4px;
  flex-shrink: 0;
}
.tab-search .search-btn {
  width: 22px;
  height: 22px;
  border: none;
  background: transparent;
  cursor: pointer;
  color: var(--t-3);
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 3px;
  padding: 0;
}
.tab-search .search-btn:hover { background: var(--bg-3); color: var(--cy); }
.tab-search .search-btn svg { width: 13px; height: 13px; }
.tab-search.open .search-btn { color: var(--cy); }
.tab-search input {
  background: var(--bg-3);
  border: 1px solid var(--line-2);
  color: var(--t-1);
  padding: 2px 8px;
  height: 22px;
  width: 200px;
  border-radius: 4px;
  font-family: inherit;
  font-size: 11.5px;
  outline: none;
}
.tab-search input:focus { border-color: var(--cy); }
.tab-search .search-count {
  font-family: var(--f-mono);
  font-size: 10px;
  color: var(--t-3);
  white-space: nowrap;
  padding: 0 4px;
}

/* 空状態 */
.tab-empty {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 16px;
  color: var(--t-3);
  font-size: 11.5px;
  font-style: italic;
  white-space: nowrap;
}
.tab-empty svg { width: 12px; height: 12px; opacity: 0.6; }
</style>
