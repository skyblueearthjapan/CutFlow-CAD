<script setup lang="ts">
// 右インスペクタ (340px)。
// 現在の activeTool に応じて tool-head + mode-body を切替。
// 各ツールのボディは mockup の `panels[mode]` をそのまま Vue template に移植。
// 行/チップの ON 状態は ref(Set) で reactive 管理し、DOM 直接操作はしない。
// バナー表示は activeTool ストアに集約。
import { computed, ref } from 'vue';
import { useActiveTool, toolMeta } from '../stores/activeTool';

const { activeTool, toggleBanner } = useActiveTool();
const meta = computed(() => toolMeta[activeTool.value]);

// mockup の初期 on 状態 (寸法線/バルーン/タップ穴マーク) を踏襲
const selectedEntRows = ref<Set<number>>(new Set([0, 1, 2]));
function toggleEntRow(i: number) {
  const next = new Set(selectedEntRows.value);
  if (next.has(i)) next.delete(i); else next.add(i);
  selectedEntRows.value = next;
}

// chamfer ツール: 角チップの ON 状態 (mockup 初期: 右上のみ)
const selectedCorners = ref<Set<number>>(new Set([0]));
function toggleCorner(i: number) {
  const next = new Set(selectedCorners.value);
  if (next.has(i)) next.delete(i); else next.add(i);
  selectedCorners.value = next;
}
</script>

<template>
  <aside class="editor">
    <!-- tool-head: 現在ツールのアイデンティティ -->
    <div class="tool-head" id="toolHead">
      <div class="ti"><svg><use :href="`#${meta.icon}`" /></svg></div>
      <div>
        <div class="tname">{{ meta.name }}</div>
        <div class="tsub">{{ meta.sub }}</div>
      </div>
      <span class="pill tpill" :class="meta.pill.cls">{{ meta.pill.text }}</span>
    </div>

    <!-- mode-body: 各ツールごとの本文 -->
    <div class="mode-body" id="modeBody">
      <!-- ========== outer ========== -->
      <template v-if="activeTool === 'outer'">
        <div class="section-block">
          <p class="lead">
            トポロジ再構築で <em>外径 (LINE×8 + ARC×4)</em> を検出しました。誤検出があればキャンバスから線をクリックして修正。
          </p>

          <div class="kv">
            <div><div class="k">外径構成</div><div class="ksub">CLOSED · 12 segments</div></div>
            <span class="v">LINE 8 / ARC 4</span>
          </div>
          <div class="kv">
            <div><div class="k">外周長</div><div class="ksub">PERIMETER</div></div>
            <span class="v">1,398 mm</span>
          </div>
          <div class="kv">
            <div><div class="k">面積</div><div class="ksub">AREA</div></div>
            <span class="v">123,200 mm²</span>
          </div>

          <div class="warn-strip">
            <svg><use href="#i-warning" /></svg>
            <div><b>閉ループ未確認 (1)</b><br />右上角の C2 開先指定により、ARC を再計算してください。</div>
          </div>

          <div class="action-row">
            <button class="action-btn" @click="toggleBanner">線を手動指定</button>
            <button class="action-btn cy"><svg><use href="#i-arrow-right" /></svg>次へ</button>
          </div>
        </div>
      </template>

      <!-- ========== delete ========== -->
      <template v-else-if="activeTool === 'delete'">
        <div class="section-block">
          <p class="lead">
            DXF由来の <em>製図情報</em> を出力前に取り除きます。種類別にトグル、またはキャンバスで個別選択。
          </p>

          <div class="entity-list">
            <div class="ent-row" :class="{ on: selectedEntRows.has(0) }" @click="toggleEntRow(0)">
              <span class="cb"></span>
              <div><div class="nm">寸法線</div><div class="ns">DIMENSION</div></div>
              <span class="cnt">5</span>
            </div>
            <div class="ent-row" :class="{ on: selectedEntRows.has(1) }" @click="toggleEntRow(1)">
              <span class="cb"></span>
              <div><div class="nm">バルーン</div><div class="ns">BALLOON</div></div>
              <span class="cnt">2</span>
            </div>
            <div class="ent-row" :class="{ on: selectedEntRows.has(2) }" @click="toggleEntRow(2)">
              <span class="cb"></span>
              <div><div class="nm">タップ穴マーク</div><div class="ns">TAP-MARK · M8 × 4</div></div>
              <span class="cnt">4</span>
            </div>
            <div class="ent-row" :class="{ on: selectedEntRows.has(3) }" @click="toggleEntRow(3)">
              <span class="cb"></span>
              <div><div class="nm">図枠 / 表題欄</div><div class="ns">PRODUCTION-FRAME</div></div>
              <span class="cnt">1</span>
            </div>
          </div>
        </div>

        <div class="summary">
          <h6>削除後プレビュー</h6>
          <div class="summary-row"><span class="k">残るエンティティ</span><span class="v big">150</span></div>
          <div class="summary-row"><span class="k">外径</span><span class="v">12 / 12 OK</span></div>
          <div class="summary-row"><span class="k">推定切断長</span><span class="v">1,847.3<span class="u">mm</span></span></div>
        </div>

        <div class="action-row">
          <button class="action-btn">プレビュー</button>
          <button class="action-btn danger"><svg><use href="#i-delete" /></svg>12件を削除</button>
        </div>
      </template>

      <!-- ========== offset ========== -->
      <template v-else-if="activeTool === 'offset'">
        <div class="section-block">
          <p class="lead">
            外径から外側に <em>加工代</em> を付加します。デフォルト値の後、辺ごとに個別調整できます。
          </p>

          <div class="kv">
            <div><div class="k">デフォルト</div><div class="ksub">外周全体に適用</div></div>
            <div class="num-step">
              <button>−</button>
              <input type="text" value="3.0" />
              <span class="unit">mm</span>
              <button>+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">角の処理</div><div class="ksub">CORNER-TYPE</div></div>
            <span class="v">円弧で連結</span>
          </div>
        </div>

        <div class="section-block">
          <h6 class="lbl">辺ごとの個別設定 <span class="right">クリック で 編集</span></h6>
          <div class="edge-list">
            <div class="edge-row">
              <span class="ix">E1</span>
              <div><div class="nm">上辺</div><div class="ns">LINE · 660 mm</div></div>
              <span class="val">+3.0</span>
            </div>
            <div class="edge-row">
              <span class="ix">E2</span>
              <div><div class="nm">右辺</div><div class="ns">LINE · 360 mm</div></div>
              <span class="val">+3.0</span>
            </div>
            <div class="edge-row">
              <span class="ix">E3</span>
              <div><div class="nm">下辺</div><div class="ns">LINE · 660 mm</div></div>
              <span class="val" style="color:var(--am)">+5.0</span>
            </div>
            <div class="edge-row">
              <span class="ix">E4</span>
              <div><div class="nm">左辺</div><div class="ns">LINE · 360 mm</div></div>
              <span class="val">+3.0</span>
            </div>
          </div>
        </div>

        <div class="summary">
          <h6>加工代適用後</h6>
          <div class="summary-row"><span class="k">外周長</span><span class="v big">1,471<span class="u">mm</span></span></div>
          <div class="summary-row"><span class="k">板取り寸法</span><span class="v">446 × 286 mm</span></div>
          <div class="summary-row"><span class="k">材料効率</span><span class="v">94.2%</span></div>
        </div>

        <div class="action-row">
          <button class="action-btn">プレビュー再計算</button>
          <button class="action-btn cy"><svg><use href="#i-arrow-right" /></svg>次へ</button>
        </div>
      </template>

      <!-- ========== chamfer ========== -->
      <template v-else-if="activeTool === 'chamfer'">
        <div class="section-block">
          <p class="lead">
            外径の <em>角</em> または <em>辺</em> をキャンバスでクリックして指定します。出力時に注記として記載されます。
          </p>

          <div class="kv">
            <div><div class="k">C面サイズ</div><div class="ksub">CHAMFER-SIZE</div></div>
            <div class="num-step">
              <button>−</button>
              <input type="text" value="C2" />
              <span class="unit">×45°</span>
              <button>+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">開先角度</div><div class="ksub">BEVEL-ANGLE</div></div>
            <div class="num-step">
              <button>−</button>
              <input type="text" value="30" />
              <span class="unit">°</span>
              <button>+</button>
            </div>
          </div>
        </div>

        <div class="section-block">
          <h6 class="lbl">指定済みの角 <span class="right">クリック で 解除</span></h6>
          <div class="corner-list">
            <div class="corner-chip" :class="{ on: selectedCorners.has(0) }" @click="toggleCorner(0)"><span class="dot"></span>右上 · C2</div>
            <div class="corner-chip" :class="{ on: selectedCorners.has(1) }" @click="toggleCorner(1)"><span class="dot"></span>左上</div>
            <div class="corner-chip" :class="{ on: selectedCorners.has(2) }" @click="toggleCorner(2)"><span class="dot"></span>右下</div>
            <div class="corner-chip" :class="{ on: selectedCorners.has(3) }" @click="toggleCorner(3)"><span class="dot"></span>左下</div>
          </div>
        </div>

        <div
          class="summary"
          style="border-color:rgba(167,139,250,0.25);background:linear-gradient(180deg, rgba(167,139,250,0.04) 0%, rgba(167,139,250,0) 100%);"
        >
          <h6 style="color:var(--chamfer)">DXF出力時の注記</h6>
          <div class="summary-row"><span class="k">右上 角部</span><span class="v" style="color:var(--chamfer)">C2 × 45°</span></div>
          <div class="summary-row"><span class="k">開先 (該当辺)</span><span class="v">なし</span></div>
        </div>

        <div class="action-row">
          <button class="action-btn">指定をクリア</button>
          <button class="action-btn cy"><svg><use href="#i-arrow-right" /></svg>出力へ</button>
        </div>
      </template>

      <!-- ========== dim ========== -->
      <template v-else-if="activeTool === 'dim'">
        <div class="section-block">
          <p class="lead">
            出力DXFに <em>注釈寸法</em> を残したい場合に使用します。「削除」で消した寸法とは別に、加工指示として必要な寸法だけを再付加します。
          </p>

          <div class="kv">
            <div><div class="k">スタイル</div><div class="ksub">DIM-STYLE</div></div>
            <span class="v">ISO 標準</span>
          </div>
          <div class="kv">
            <div><div class="k">小数桁</div><div class="ksub">PRECISION</div></div>
            <div class="num-step">
              <button>−</button>
              <input type="text" value="1" />
              <span class="unit">桁</span>
              <button>+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">矢印サイズ</div><div class="ksub">ARROW-SIZE</div></div>
            <span class="v">3.5 mm</span>
          </div>
        </div>

        <div class="placeholder-card">
          <div class="ic"><svg><use href="#i-dim" /></svg></div>
          <h5>寸法を追加するには</h5>
          <p>
            キャンバスで 2 点をクリックすると、その間の寸法線がここに表示されます。<br />
            キーボード <b style="color:var(--t-2)">D</b> で2点間モード。
          </p>
          <span class="meta">追加済み 0 件</span>
        </div>
      </template>

      <!-- ========== edit ========== -->
      <template v-else-if="activeTool === 'edit'">
        <div class="section-block">
          <p class="lead">
            外径や穴の <em>頂点・線分</em> を直接ドラッグして編集できます。スナップ・寸法表示は自動で有効になります。
          </p>

          <div class="kv">
            <div><div class="k">スナップ</div><div class="ksub">SNAP</div></div>
            <span class="v">端点 + 中点 + 交点</span>
          </div>
          <div class="kv">
            <div><div class="k">グリッド吸着</div><div class="ksub">GRID-SNAP</div></div>
            <span class="v">1 mm</span>
          </div>
          <div class="kv">
            <div><div class="k">直交モード</div><div class="ksub">ORTHO</div></div>
            <span class="v">OFF (Shift)</span>
          </div>
        </div>

        <div class="placeholder-card">
          <div class="ic"><svg><use href="#i-edit-line" /></svg></div>
          <h5>線を選択してください</h5>
          <p>
            キャンバス上の <b style="color:var(--t-2)">頂点</b> または <b style="color:var(--t-2)">線分</b>
            をクリックすると、ここに座標・長さ・角度が表示され、ドラッグで編集できます。
          </p>
          <span class="meta">選択 0 / 162 entities</span>
        </div>
      </template>

      <!-- ========== hole ========== -->
      <template v-else-if="activeTool === 'hole'">
        <div class="section-block">
          <p class="lead">
            外径の内側の任意位置に <em>穴を追加</em> します。座標指定 / クリック配置 / 整列パターン に対応。
          </p>

          <div class="kv">
            <div><div class="k">穴径</div><div class="ksub">DIAMETER</div></div>
            <div class="num-step">
              <button>−</button>
              <input type="text" value="φ9.0" />
              <span class="unit">mm</span>
              <button>+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">配置方式</div><div class="ksub">PLACEMENT</div></div>
            <span class="v">クリックで配置</span>
          </div>
          <div class="kv">
            <div><div class="k">タップ指示</div><div class="ksub">TAP-NOTE</div></div>
            <span class="v">なし</span>
          </div>
        </div>

        <div class="placeholder-card">
          <div class="ic"><svg><use href="#i-hole-add" /></svg></div>
          <h5>キャンバスをクリックして配置</h5>
          <p>
            カーソル位置に <b style="color:var(--t-2)">φ9.0</b> の穴が追加されます。<br />
            連続配置: Shift+クリック、整列パターン: <b style="color:var(--t-2)">A</b>
          </p>
          <span class="meta">追加済み 0 件</span>
        </div>
      </template>

      <!-- ========== note ========== -->
      <template v-else-if="activeTool === 'note'">
        <div class="section-block">
          <p class="lead">
            部品単位の <em>加工指示</em> (溶接記号・面粗さ・熱処理 等) を文字注記として残します。
          </p>

          <div class="kv">
            <div><div class="k">プリセット</div><div class="ksub">NOTE-PRESET</div></div>
            <span class="v">面粗さ / 溶接 / 一般</span>
          </div>
          <div class="kv">
            <div><div class="k">フォント</div><div class="ksub">FONT</div></div>
            <span class="v">isocp · 2.5 mm</span>
          </div>
        </div>

        <div class="placeholder-card">
          <div class="ic"><svg><use href="#i-note" /></svg></div>
          <h5>注記はまだありません</h5>
          <p>
            キャンバス上で右クリック → <b style="color:var(--t-2)">「注記を追加」</b> または
            <b style="color:var(--t-2)">T</b> キーで挿入。
          </p>
          <span class="meta">注記 0 件</span>
        </div>
      </template>

      <!-- ========== bridge ========== -->
      <template v-else-if="activeTool === 'bridge'">
        <div class="section-block">
          <p class="lead">
            レーザ・プラズマ加工で部品が脱落しないよう、外径に <em>ブリッジ(保持タブ)</em> を残します。出力時に切断パスが分断されます。
          </p>

          <div class="kv">
            <div><div class="k">ブリッジ幅</div><div class="ksub">BRIDGE-WIDTH</div></div>
            <div class="num-step">
              <button>−</button>
              <input type="text" value="2.0" />
              <span class="unit">mm</span>
              <button>+</button>
            </div>
          </div>
          <div class="kv">
            <div><div class="k">推奨個数</div><div class="ksub">AUTO-COUNT</div></div>
            <span class="v">4 (重量より算出)</span>
          </div>
          <div class="kv">
            <div><div class="k">配置方式</div><div class="ksub">PLACEMENT</div></div>
            <span class="v">等間隔 (自動)</span>
          </div>
        </div>

        <div class="placeholder-card">
          <div class="ic"><svg><use href="#i-bridge" /></svg></div>
          <h5>外径をクリックして配置</h5>
          <p>
            キャンバス上の外径線をクリックすると、その位置に <b style="color:var(--t-2)">2.0 mm</b> のブリッジが残ります。
          </p>
          <span class="meta">配置 0 / 推奨 4</span>
        </div>
      </template>
    </div>
  </aside>
</template>
