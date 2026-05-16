<script setup lang="ts">
/**
 * Header (44px).
 *
 * - Two file pickers:
 *    * ファイル: multi-select individual DXFs
 *    * フォルダ: pick a whole folder (webkitdirectory) — DXFs anywhere in
 *      the tree are accepted; assembly drawings are dropped client-side
 *      to mirror the server filter (H3).
 * - Both pickers are registered with the session store on mount so other
 *   components (TabBar) can open them without DOM queries (M4).
 * - The "DXF を書き出す" button calls the session export action.
 * - The title-block becomes dynamic once a file is open (file name + count).
 *
 * The HTML structure / classes are preserved 1:1 with the v3 mockup so the
 * design stays pixel-perfect.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useSession } from '../stores/session';

const {
  currentSession,
  currentFile,
  isUploading,
  uploadFiles,
  exportDxf,
  registerFilePicker,
  registerFolderPicker,
} = useSession();

const fileInput = ref<HTMLInputElement | null>(null);
const folderInput = ref<HTMLInputElement | null>(null);

function openFile() {
  fileInput.value?.click();
}
function openFolder() {
  folderInput.value?.click();
}

onMounted(() => {
  registerFilePicker(openFile);
  registerFolderPicker(openFolder);
});
onUnmounted(() => {
  registerFilePicker(() => undefined);
  registerFolderPicker(() => undefined);
});

async function onFilesChosen(e: Event) {
  const input = e.target as HTMLInputElement;
  if (!input.files || input.files.length === 0) return;
  // Hand the raw list to the store; it does the .dxf / assembly filtering
  // so the same rules apply whether files came from file or folder picker.
  const files = Array.from(input.files);
  // Reset so the same file can be re-selected later.
  input.value = '';
  await uploadFiles(files);
}

/** When a session is active, show file name + total count; otherwise the v3 demo line. */
const titleParts = computed(() => {
  const session = currentSession.value;
  const file = currentFile.value;
  if (!session) {
    return {
      pn: '25057 / P1 / 03',
      name: 'センタープレート',
      meta: 'SS400 t9',
    };
  }
  if (file) {
    // strip extension for display
    const base = file.name.replace(/\.[Dd][Xx][Ff]$/, '');
    return {
      pn: `${session.files.length} files`,
      name: base,
      meta: `${file.stats.total} entities`,
    };
  }
  return {
    pn: `${session.files.length} files`,
    name: 'ファイルを選択',
    meta: '—',
  };
});
</script>

<template>
  <div class="header">
    <div class="brand">
      <div class="brand-mark"></div>
      <div class="brand-name">CutFlow<em>•</em>CAD</div>
    </div>

    <div class="title-block">
      <span class="pn">{{ titleParts.pn }}</span>
      <b>{{ titleParts.name }}</b>
      <span class="mat">{{ titleParts.meta }}</span>
    </div>

    <div class="actions">
      <!-- hidden file picker (multi-file) -->
      <input
        ref="fileInput"
        type="file"
        accept=".dxf,.DXF"
        multiple
        style="display:none"
        @change="onFilesChosen"
      />
      <!-- hidden folder picker. `webkitdirectory` is non-standard but widely
           supported (Chromium/Safari/Firefox); on browsers that ignore it
           the picker just falls back to file-mode without crashing. -->
      <input
        ref="folderInput"
        type="file"
        accept=".dxf,.DXF"
        multiple
        webkitdirectory
        directory
        style="display:none"
        @change="onFilesChosen"
      />

      <button class="kbd-hint" @click="openFile" :disabled="isUploading">
        <svg><use href="#i-search" /></svg>
        {{ isUploading ? 'アップロード中…' : 'ファイル' }}
        <span class="kbd">📂</span>
      </button>
      <button class="kbd-hint" @click="openFolder" :disabled="isUploading" title="フォルダ単位で取り込み">
        <svg><use href="#i-search" /></svg>
        フォルダ
        <span class="kbd">📁</span>
      </button>
      <button class="btn primary" @click="exportDxf" :disabled="!currentFile">
        <svg><use href="#i-output" /></svg>
        DXF を書き出す
      </button>
      <div class="user-chip">YT</div>
    </div>
  </div>
</template>
