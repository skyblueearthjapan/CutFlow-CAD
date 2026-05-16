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
 * - Phase 3: the single "DXF を書き出す" button becomes a 📤 出力 dropdown
 *   that lets the user pick DXF (with offset toggle) or PDF (枠あり/枠なし
 *   ラジオ + 加工代 / C面 込みのチェックボックス). The export action runs
 *   when the user clicks "ダウンロード" in the sub-panel.
 *
 * The HTML structure / classes are preserved 1:1 with the v3 mockup so the
 * design stays pixel-perfect. The dropdown is an extra layer that opens
 * below the primary button — no other elements move.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useSession } from '../stores/session';

const {
  currentSession,
  currentFile,
  isUploading,
  uploadFiles,
  exportDxf,
  exportDxfWithOffset,
  offsetResult,
  registerFilePicker,
  registerFolderPicker,
  // Phase 3
  pdfExportOptions,
  pdfMaterial,
  isExportingPdf,
  setPdfFrameOption,
  setPdfWithOffset,
  setPdfWithChamfer,
  setPdfMaterial,
  exportPdf,
} = useSession();

/** Format radio + checkbox state local to the dropdown UI. */
const exportFormat = ref<'dxf' | 'pdf'>('dxf');
/** Mirror of the offset-export flag for DXF (defaults to ON when an offset
 *  preview has been computed). The Phase 2 single-button behaviour is kept
 *  by binding this to the same condition. */
const dxfWithOffset = ref<boolean>(true);

/** Open / close state for the export dropdown. */
const exportOpen = ref(false);
const exportRoot = ref<HTMLElement | null>(null);
function toggleExport() {
  if (!currentFile.value) return;
  exportOpen.value = !exportOpen.value;
}
function closeExport() {
  exportOpen.value = false;
}

/** Click-outside / Escape to close the dropdown. */
function onDocClick(e: MouseEvent) {
  if (!exportOpen.value) return;
  const root = exportRoot.value;
  if (root && !root.contains(e.target as Node)) closeExport();
}
function onDocKey(e: KeyboardEvent) {
  if (e.key === 'Escape') closeExport();
}

async function onDownload() {
  if (exportFormat.value === 'dxf') {
    // Honour both the offset checkbox AND whether an offset result actually
    // exists — if the user toggled it on without computing, fall back to the
    // plain export so we never POST `with_offset=true` against an empty cache.
    if (dxfWithOffset.value && offsetResult.value) {
      await exportDxfWithOffset();
    } else {
      await exportDxf();
    }
  } else {
    await exportPdf();
  }
  closeExport();
}

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
  document.addEventListener('mousedown', onDocClick);
  document.addEventListener('keydown', onDocKey);
});
onUnmounted(() => {
  registerFilePicker(() => undefined);
  registerFolderPicker(() => undefined);
  document.removeEventListener('mousedown', onDocClick);
  document.removeEventListener('keydown', onDocKey);
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

      <!-- Phase 3: export dropdown — DXF / PDF picker -->
      <div class="export-menu" ref="exportRoot">
        <button
          class="btn primary"
          :disabled="!currentFile"
          @click="toggleExport"
          :aria-expanded="exportOpen"
        >
          <svg><use href="#i-output" /></svg>
          出力
          <span style="font-size:10px;margin-left:2px">▾</span>
        </button>
        <div v-if="exportOpen" class="export-panel" @click.stop>
          <div class="ex-row">
            <label class="ex-radio">
              <input type="radio" v-model="exportFormat" value="dxf" />
              <span>DXF</span>
            </label>
            <label class="ex-radio">
              <input type="radio" v-model="exportFormat" value="pdf" />
              <span>PDF</span>
            </label>
          </div>

          <!-- DXF options -->
          <template v-if="exportFormat === 'dxf'">
            <label class="ex-check">
              <input
                type="checkbox"
                v-model="dxfWithOffset"
                :disabled="!offsetResult"
              />
              <span>加工代を含める <small v-if="!offsetResult">(未計算)</small></span>
            </label>
          </template>

          <!-- PDF options -->
          <template v-else>
            <div class="ex-group">
              <!-- H7: 枠 labels disambiguate cutflow material-take frame
                   ('auto' falls back to cutflow on the backend so the two
                   share a "material-take frame" label). -->
              <div class="ex-glabel">枠</div>
              <label class="ex-radio">
                <input
                  type="radio"
                  :checked="pdfExportOptions.frame === 'auto'"
                  @change="setPdfFrameOption('auto')"
                />
                <span>自動 (材料取り枠)</span>
              </label>
              <label class="ex-radio">
                <input
                  type="radio"
                  :checked="pdfExportOptions.frame === 'cutflow'"
                  @change="setPdfFrameOption('cutflow')"
                />
                <span>材料取り枠</span>
              </label>
              <label class="ex-radio">
                <input
                  type="radio"
                  :checked="pdfExportOptions.frame === 'none'"
                  @change="setPdfFrameOption('none')"
                />
                <span>枠なし</span>
              </label>
            </div>
            <!-- H4: material — optional free-text rendered on the PDF header. -->
            <label class="ex-check ex-material">
              <span>材質</span>
              <input
                type="text"
                class="ex-input"
                :value="pdfMaterial"
                placeholder="例: SS400 t9"
                @input="setPdfMaterial(($event.target as HTMLInputElement).value)"
              />
            </label>
            <label class="ex-check">
              <input
                type="checkbox"
                :checked="pdfExportOptions.with_offset"
                @change="setPdfWithOffset(($event.target as HTMLInputElement).checked)"
                :disabled="!offsetResult"
              />
              <span>加工代を含める <small v-if="!offsetResult">(未計算)</small></span>
            </label>
            <label class="ex-check">
              <input
                type="checkbox"
                :checked="pdfExportOptions.with_chamfer"
                @change="setPdfWithChamfer(($event.target as HTMLInputElement).checked)"
              />
              <span>C面注記を含める</span>
            </label>
          </template>

          <button
            class="btn primary ex-download"
            :disabled="!currentFile || isExportingPdf"
            @click="onDownload"
          >
            <svg><use href="#i-output" /></svg>
            {{ isExportingPdf ? '出力中…' : 'ダウンロード' }}
          </button>
        </div>
      </div>

      <div class="user-chip">YT</div>
    </div>
  </div>
</template>

<style scoped>
.export-menu {
  position: relative;
  display: inline-flex;
}
.export-panel {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 100;
  min-width: 240px;
  padding: 12px;
  background: var(--bg-2);
  border: 1px solid var(--line-2);
  border-radius: var(--r-md);
  box-shadow: 0 6px 22px -6px rgba(0, 0, 0, 0.6);
  display: flex; flex-direction: column; gap: 10px;
}
.ex-row {
  display: flex; gap: 14px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--line-1);
}
.ex-radio, .ex-check {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px;
  color: var(--t-1);
  cursor: pointer;
}
.ex-radio input, .ex-check input {
  accent-color: var(--cy);
  cursor: pointer;
}
.ex-check small { color: var(--t-4); font-size: 10px; margin-left: 2px; }
.ex-group {
  display: flex; flex-direction: column; gap: 4px;
}
.ex-glabel {
  font-family: var(--f-mono);
  font-size: 9.5px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--t-4);
  margin-bottom: 4px;
}
.ex-download {
  margin-top: 4px;
  height: 30px;
  justify-content: center;
}
.ex-material {
  gap: 8px;
}
.ex-input {
  flex: 1;
  padding: 4px 6px;
  font-size: 12px;
  background: var(--bg-1);
  color: var(--t-1);
  border: 1px solid var(--line-1);
  border-radius: 4px;
  outline: none;
  font-family: var(--f-mono);
}
.ex-input:focus { border-color: var(--cy); }
</style>
