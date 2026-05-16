<script setup lang="ts">
// ヘッダ (44px)。ブランド・現図面名・⌘K コマンド・DXF出力・ユーザー chip。
// mockup の HTML 構造をそのまま Vue template に移植。
// データはハードコード (Phase 0 は静的見た目のみ)。
</script>

<template>
  <div class="header">
    <div class="brand">
      <div class="brand-mark"></div>
      <div class="brand-name">CutFlow<em>•</em>CAD</div>
    </div>

    <div class="title-block">
      <span class="pn">25057 / P1 / 03</span>
      <b>センタープレート</b>
      <span class="mat">SS400 t9</span>
    </div>

    <div class="actions">
      <button class="kbd-hint">
        <svg><use href="#i-search" /></svg>
        コマンド
        <span class="kbd">⌘ K</span>
      </button>
      <button class="btn primary">
        <svg><use href="#i-output" /></svg>
        DXF を書き出す
      </button>
      <div class="user-chip">YT</div>
    </div>
  </div>
</template>
