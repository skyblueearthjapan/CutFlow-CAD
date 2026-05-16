# CutFlow•CAD — アーキテクチャ設計

---

## 1. システム全体構成

```
┌────────────────────────────────────────────────────────────────┐
│ ブラウザ (社内PC / Chrome・Edge)                               │
│ ┌────────────────────────────────────────────────────────────┐ │
│ │ Vue 3 + Vite (SPA)                                         │ │
│ │  - SVGキャンバス (編集の即時反映)                          │ │
│ │  - ツールレール / インスペクタ / DXFタブ                   │ │
│ │  - WebSocket セッション保持                                │ │
│ └────────────────────────────────────────────────────────────┘ │
└────────────────────────┬───────────────────────────────────────┘
                         │ HTTPS (自動)
                         ▼
┌────────────────────────────────────────────────────────────────┐
│ Tailscale Funnel (本番) → https://cutflow.<tailnet>.ts.net     │
│   ・ドメイン購入不要、HTTPS自動                                  │
│   ・訪問者はブラウザだけ (Tailscaleアプリ不要)                  │
│   ・公開範囲は Tailscale ACL で制御可能                         │
└────────────────────────┬───────────────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────────────┐
│ Hostinger VPS (srv1508169 / 31.97.109.137 / Ubuntu 24.04)      │
│ /opt/cutflow-cad/                                              │
│ ┌──────────────┐  ┌─────────────────────────────────────────┐ │
│ │ Caddy 2      │  │ FastAPI (Python 3.12, uvicorn)          │ │
│ │ - 静的SPA配信│  │  - /api/upload   DXF取込                │ │
│ │ - リバプロ   │→ │  - /api/detect   外径検出               │ │
│ │ - SSL        │  │  - /api/offset   加工代計算             │ │
│ └──────────────┘  │  - /api/export   DXF/PDF書き出し        │ │
│                   │  - WebSocket /ws セッション             │ │
│                   │                                          │ │
│                   │ Libraries:                               │ │
│                   │  - ezdxf       (DXF読み書き)            │ │
│                   │  - shapely     (幾何処理)               │ │
│                   │  - pyclipper   (オフセット計算)         │ │
│                   │  - ReportLab   (PDF生成)                │ │
│                   └─────────────────────────────────────────┘ │
│                                                                │
│ /var/cutflow/sessions/{session_id}/                            │
│   一時ファイル (24時間で自動削除)                              │
│                                                                │
│ 既存プロジェクト (隔離して同居):                                │
│   /opt/lineworks-x-ops/   (Discord Bot, Claude/Codex)         │
└────────────────────────────────────────────────────────────────┘
```

---

## 2. フロントエンド設計

### 技術選定理由

| 技術 | 理由 |
|---|---|
| **Vue 3** | リアクティブ性、シングルファイルコンポーネント、学習コスト低、保守性が高い |
| **Vite** | 高速HMR、開発体験◎ |
| **SVG (直接DOM操作)** | Canvas/WebGLは不要 (2D、エンティティ数最大数千)。CSSで色制御できる利点が大きい |
| **vanilla CSS + CSS変数** | デザインシステム (v3) との完全一致を保つ |

### ディレクトリ構造（予定）

> **Note**: 以下は最終形の構造案。Phase 0時点では、Canvas → CanvasArea, components.css 単一ファイル等、簡略化された形で実装されている。Phase 1以降で本構造に近づけていく。

```
web/
├─ index.html
├─ src/
│  ├─ main.ts
│  ├─ App.vue
│  ├─ components/
│  │  ├─ Header.vue
│  │  ├─ ToolRail.vue          ← 左64px ツール一覧
│  │  ├─ Canvas.vue            ← SVGキャンバス本体
│  │  ├─ Inspector.vue         ← 右340px ツール別UI
│  │  ├─ TabBar.vue            ← 下部DXFタブ
│  │  ├─ StatusBar.vue
│  │  └─ inspectors/
│  │     ├─ OuterInspector.vue
│  │     ├─ DeleteInspector.vue
│  │     ├─ OffsetInspector.vue
│  │     ├─ ChamferInspector.vue
│  │     └─ (...future tools)
│  ├─ stores/
│  │  ├─ session.ts            ← 現在のセッション・図面リスト
│  │  ├─ activeTool.ts         ← 選択中ツール
│  │  └─ entities.ts           ← DXFエンティティ
│  ├─ services/
│  │  ├─ api.ts                ← FastAPI呼出
│  │  └─ ws.ts                 ← WebSocket
│  ├─ assets/
│  │  ├─ icons.svg             ← SVGアイコンsymbol定義 (v3から流用)
│  │  └─ styles/
│  │     ├─ tokens.css         ← CSS変数 (v3から流用)
│  │     ├─ base.css
│  │     └─ entities.css       ← .ent.outer, .ent.dim, etc.
│  └─ types/
│     └─ dxf.ts                ← TypeScript型定義
└─ vite.config.ts
```

---

## 3. バックエンド設計

### 技術選定理由

| 技術 | 理由 |
|---|---|
| **Python 3.12** | DXF処理ライブラリの主戦場 |
| **FastAPI** | 型ヒント＋高速、自動OpenAPI、async対応 |
| **ezdxf** | DXFの読み書き＆生成。AutoCAD R12〜2018+対応の業界標準 |
| **Shapely** | 2D幾何処理、Polygon操作 |
| **pyclipper / Clipper2** | ポリラインオフセットの数学的に正しい実装。これがないと自前で書くのは地獄 |
| **ReportLab** | PDF生成、ベクター出力 |

### APIエンドポイント（予定）

| Method | Path | 用途 |
|---|---|---|
| POST | `/api/upload` | DXFファイル(複数)アップロード、セッション作成 |
| GET | `/api/session/{sid}` | セッション情報・図面一覧 |
| GET | `/api/session/{sid}/file/{fid}` | エンティティ取得（SVG変換可能形式） |
| POST | `/api/session/{sid}/file/{fid}/detect-outer` | 外径自動検出実行 |
| POST | `/api/session/{sid}/file/{fid}/offset` | 加工代オフセット計算 |
| POST | `/api/session/{sid}/file/{fid}/delete` | エンティティ削除 |
| POST | `/api/session/{sid}/export` | DXF/PDF書き出し |
| WS | `/ws/{sid}` | リアルタイム同期（タブ切替・進捗通知） |

### DXF→SVG変換戦略

ブラウザに送るのはDXFそのものではなく、SVG描画用のJSON:
```json
{
  "entities": [
    { "id": "e001", "type": "LINE", "x1": 0, "y1": 0, "x2": 100, "y2": 0,
      "category": "outer", "color": 256 },
    { "id": "e002", "type": "ARC", "cx": 100, "cy": 0, "r": 20,
      "start_angle": -90, "end_angle": 0, "category": "outer" },
    { "id": "e003", "type": "DIMENSION", "category": "dimension",
      "anchors": [...], "text": "440" },
    ...
  ],
  "outer_loop": ["e001", "e002", ...],
  "delete_candidates": { "DIMENSION": [...], "BALLOON": [...], "TAP": [...] }
}
```

これにより:
- ブラウザは軽量（数MBのDXFを直接ダウンロードしない）
- カテゴリ分類済みなのでCSSクラスで色制御が容易
- 編集差分だけサーバーに送る設計が可能

---

## 4. デプロイ戦略

### 環境別構成

| 環境 | 用途 | アクセス方法 |
|---|---|---|
| **ローカル** | 個人開発・初期テスト | `docker-compose up` → http://localhost:5173 |
| **VPSテスト (1人)** | 触り心地確認 | SSHポートフォワード `ssh -L 5173:localhost:5173 lineworks-vps-user` |
| **VPS本番 (社内共有)** | 運用 | **Tailscale Funnel** → `https://cutflow.<tailnet>.ts.net` |

### Tailscale Funnel の役割（本番）

- **ドメイン購入不要**: `<好きな名前>.<tailnet名>.ts.net` 形式の固定URLが無料で発行される
- **HTTPS自動**: Tailscale側が証明書を管理、Let's Encrypt不要
- **訪問者は何も入れる必要なし**: ブラウザでURLを叩くだけ（Tailscaleアプリ不要）
- **アクセス制御**: 公開範囲は Tailscale ACL (Funnel 公開 / Tailscaleユーザーのみ / 非公開) で制御
- **トライアル中は無効化**: 設定なしでローカル/SSHトンネルから始める

### Tailscale Funnel セットアップ手順 (本番デプロイ時)

```bash
# VPS側で1回だけ
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                       # ブラウザでログイン
sudo tailscale serve --bg --https=443 http://localhost:8080
sudo tailscale funnel 443 on            # インターネットに公開

# 確認
tailscale status                        # 割り当てられたURLを表示
# → https://cutflow.<tailnet名>.ts.net
```

### 補足: Cloudflare Tunnel という選択肢

将来「綺麗なURL (cutflow.your-domain.com 等)」が必要になった場合、Tailscale Funnel から Cloudflare Tunnel に切替可能（要ドメイン購入、年1,500円程度）。アプリ本体には変更不要。

### VPS同居方針

既存 `/opt/lineworks-x-ops/` (Discord Bot, Claude/Codex) と完全分離:
```
/opt/lineworks-x-ops/   ← 既存、tmux session "xops" で常時稼働
/opt/cutflow-cad/       ← 新規、別ポート・別ユーザー権限で隔離
```

- リソースは余裕あり (15GB RAM中13GB空き、4 vCPU、175GB空き)
- 内部ポート 8080 (FastAPI) / 5173 (Vite dev) は cutflow 専用
- Caddy を 80/443 に立てる場合、既存サービスとパス分割で共存

---

## 5. セキュリティ方針

- **認証なし**（社内ネットワーク前提、後日 Cloudflare Access 等で追加可能）
- **データ一時保存のみ** (`/var/cutflow/sessions/{sid}/`)、24時間で自動削除
- **ファイルアップロード制限**: DXFのみ、5MB/件、50件/フォルダ
- **WebSocket** はセッションID必須、他セッションへのアクセス不可
- **VPS SSH** は ed25519鍵認証のみ、パスワードログイン無効化

---

## 6. パフォーマンス想定

| 操作 | 想定処理時間 | 戦略 |
|---|---|---|
| DXF 1MB読込 | < 1秒 | サーバー側 ezdxf |
| 外径検出 | < 2秒 | Pythonで一括処理 |
| オフセット計算 | < 1秒 | pyclipper (C++) |
| SVG表示 | < 100ms | カテゴリ分類済みJSON → Vue リアクティブ |
| 編集モード切替 | < 50ms | クライアント側CSSのみ |
| DXF書き出し | < 2秒 | ezdxf |

---

## 7. 監視・ログ

- アプリログ: `/opt/cutflow-cad/logs/app.log` (uvicorn) / `/opt/cutflow-cad/logs/access.log` (Caddy)
- セッション数・処理時間メトリクスは将来 Prometheus + Grafana で
- 既存 `lineworks-x-ops` の Discord Bot 通知パイプラインに乗せる選択肢もあり（VPS共通機能として）

---

## 8. 将来の拡張ポイント

| 拡張 | 必要な変更 |
|---|---|
| ネスティング (材料取り最適化) | バックエンドに別ジョブキュー、フロントは右パネルのみ追加 |
| 多人数同時編集 | WebSocketのプロトコル拡張、CRDT等を検討 |
| バージョン管理 | DXFバージョン履歴を git-like に保存 |
| OAuth認証 | Cloudflare Access か FastAPI側に追加 |
| 加工指示の自動生成 | DXFメタデータ + ルールエンジン |
