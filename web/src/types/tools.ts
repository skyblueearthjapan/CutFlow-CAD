/**
 * Tool mode identifiers shared between the rail, the inspector, and the
 * body[data-mode] styling. Order matches v3 design (1..9 keys + 0 for nest).
 *
 * - outer / delete / offset / chamfer ... 編集グループ (Phase 0-2 で実装)
 * - dim / edit / hole / note ............. 作図グループ (Phase 4 以降)
 * - bridge ............................... 出力グループ
 * - nest ................................. ネスティング (Phase 5)
 */
export type ToolMode =
  | 'outer'
  | 'delete'
  | 'offset'
  | 'chamfer'
  | 'dim'
  | 'edit'
  | 'hole'
  | 'note'
  | 'bridge'
  | 'nest';

/** Pill colour variants used in the tool head badge. */
export type PillVariant = 'cy' | 'am' | 'ok' | 'ch' | 'gh';

/** Per-tool identity displayed in the inspector's tool-head. */
export interface ToolMeta {
  /** SVG symbol id (without leading '#'), e.g. 'i-shape' */
  icon: string;
  /** Japanese display name */
  name: string;
  /** Mono caption below the name */
  sub: string;
  pill: { cls: PillVariant; text: string };
}
