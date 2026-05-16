import { ref, watch } from 'vue';
import type { ToolMeta, ToolMode } from '../types/tools';

/**
 * Single-file global tool-mode store (Pinia 不要)。
 * - `activeTool` を ref で共有
 * - `setTool()` で切替、`body[data-mode]` を同期して mockup と同じ CSS 挙動を再現
 * - mockup と同じく初期モードは `delete`
 * - `data-mode` は **このストアが唯一のソース** (index.html 側には書かない)
 */
const _activeTool = ref<ToolMode>('delete');

// バナー (canvas 上部の警告ストリップ) 表示状態 ── Inspector からトグル
const _showBanner = ref<boolean>(false);

// `body[data-mode]` を ref と同期。
// 初期マウント前に確実に属性が乗るよう、モジュール評価時に同期書き込み +
// 以降の変更は watch で追従する (immediate を使わない明示形)。
if (typeof document !== 'undefined') {
  document.body.setAttribute('data-mode', _activeTool.value);
  watch(_activeTool, (mode) => {
    document.body.setAttribute('data-mode', mode);
  });
}

export function useActiveTool() {
  return {
    activeTool: _activeTool,
    showBanner: _showBanner,
    setTool(mode: ToolMode) {
      _activeTool.value = mode;
    },
    toggleBanner() {
      _showBanner.value = !_showBanner.value;
    },
  };
}

/**
 * Tool head に表示する各ツールのメタ情報 (mockup の `toolMeta` を完全移植)。
 */
export const toolMeta: Record<ToolMode, ToolMeta> = {
  outer:   { icon: 'i-shape',     name: '外形検出',       sub: 'SHAPE · 自動検出 + 手動修正',    pill: { cls: 'ok', text: '自動 92%' } },
  delete:  { icon: 'i-delete',    name: '不要要素の削除', sub: 'DELETE · 製図情報のクリーンアップ', pill: { cls: 'am', text: '12 件' } },
  offset:  { icon: 'i-offset',    name: '加工代の付加',   sub: 'OFFSET · 外側へのオフセット',   pill: { cls: 'cy', text: '+3.0 mm' } },
  chamfer: { icon: 'i-chamfer',   name: 'C面 / 開先',     sub: 'CHAMFER · 角・辺の面取り',      pill: { cls: 'ch', text: '1 ヶ所' } },
  dim:     { icon: 'i-dim',       name: '寸法の入力',     sub: 'DIMENSION · 注釈寸法の追加',    pill: { cls: 'gh', text: '0 / 0' } },
  edit:    { icon: 'i-edit-line', name: '線の編集',       sub: 'EDIT · 頂点ドラッグ / トリム',  pill: { cls: 'gh', text: '選択 0' } },
  hole:    { icon: 'i-hole-add',  name: '穴の追加',       sub: 'HOLE · 任意位置に追加',         pill: { cls: 'gh', text: '+0' } },
  note:    { icon: 'i-note',      name: '注記',           sub: 'NOTE · 加工指示の追記',         pill: { cls: 'gh', text: '0 件' } },
  bridge:  { icon: 'i-bridge',    name: 'ブリッジ',       sub: 'BRIDGE · 切断時の保持タブ',     pill: { cls: 'gh', text: '0 / 4' } },
  nest:    { icon: 'i-nest',      name: 'ネスティング',   sub: 'NEST · 板取り最適化',           pill: { cls: 'gh', text: '0 シート' } },
};

/** 数字キー → モードのマップ。`document` の keydown ハンドラ用。
 *  Phase 5 で '0' をネスティングに割り当て (テンキー / 上段どちらも対応)。 */
export const keyToMode: Record<string, ToolMode> = {
  '1': 'outer',
  '2': 'delete',
  '3': 'offset',
  '4': 'chamfer',
  '5': 'dim',
  '6': 'edit',
  '7': 'hole',
  '8': 'note',
  '9': 'bridge',
  '0': 'nest',
};
