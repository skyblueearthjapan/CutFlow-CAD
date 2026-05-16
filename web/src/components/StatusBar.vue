<script setup lang="ts">
// 最下段ステータスバー (22px)。検出率・削除候補数・autosave 等。
// Phase 0 はモック値ハードコード。
</script>

<template>
  <div class="status">
    <div class="status-left">
      <span class="status-item"><span class="dot"></span>外径検出 <b>92%</b></span>
      <span class="status-item"><span class="dot am"></span>削除候補 <b>12</b></span>
      <span class="status-item">エンティティ <b>162 → 150</b></span>
    </div>
    <div class="status-right">
      <span class="status-item">autosave <b>2m ago</b></span>
      <span class="status-item">session <b>23:47</b></span>
    </div>
  </div>
</template>
