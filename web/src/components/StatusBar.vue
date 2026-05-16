<script setup lang="ts">
/**
 * Bottom status bar (22px).
 *
 * When a file is open: shows live entity counts + the live/mock backend flag.
 * Otherwise: keeps the v3 mockup numbers so the empty state stays faithful.
 */
import { computed } from 'vue';
import { useSession } from '../stores/session';

const {
  currentFile,
  selectedForDelete,
  totalDeleteCandidates,
  remainingAfterDelete,
  isLiveBackend,
} = useSession();

const live = computed(() => currentFile.value !== null);
const backendLabel = computed(() => {
  if (isLiveBackend.value === null) return 'idle';
  return isLiveBackend.value ? 'live' : 'mock';
});
</script>

<template>
  <div class="status">
    <div class="status-left">
      <template v-if="live">
        <span class="status-item"><span class="dot am"></span>削除候補 <b>{{ totalDeleteCandidates }}</b></span>
        <span class="status-item">選択中 <b>{{ selectedForDelete.size }}</b></span>
        <span class="status-item">エンティティ <b>{{ currentFile!.entities.length }} → {{ remainingAfterDelete }}</b></span>
      </template>
      <template v-else>
        <span class="status-item"><span class="dot"></span>外径検出 <b>92%</b></span>
        <span class="status-item"><span class="dot am"></span>削除候補 <b>12</b></span>
        <span class="status-item">エンティティ <b>162 → 150</b></span>
      </template>
    </div>
    <div class="status-right">
      <span class="status-item">backend <b>{{ backendLabel }}</b></span>
      <span class="status-item">session <b>23:47</b></span>
    </div>
  </div>
</template>
