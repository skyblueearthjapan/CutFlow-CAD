# CutFlow•CAD — 開発セットアップ

---

## 必要な環境

| ツール | バージョン | 用途 |
|---|---|---|
| **Docker Desktop** | 最新安定版 | コンテナ実行 (Windows / macOS / Linux) |
| **Git** | 2.x 以上 | ソースコード管理 |
| Node.js 20 | (任意) | Docker を使わずローカル直接実行する場合 |
| Python 3.12 | (任意) | Docker を使わずローカル直接実行する場合 |

> **Windows ユーザーへ**: Docker Desktop は WSL 2 バックエンドを推奨。
> インストール時に「Use WSL 2 instead of Hyper-V」を選択してください。

---

## クイックスタート (Docker 使用・推奨)

### 1. リポジトリクローン

```powershell
git clone https://github.com/skyblueearthjapan/CutFlow-CAD.git
cd CutFlow-CAD
```

### 2. 起動

```powershell
docker-compose up
```

初回はイメージビルドで数分かかります。以下のログが出れば起動完了です:

```
cutflow-cad-api  | INFO:     Application startup complete.
cutflow-cad-web  |   VITE v5.x.x  ready in xxx ms
```

バックグラウンドで起動したい場合:

```powershell
docker-compose up -d
```

### 3. アクセス

| サービス | URL |
|---|---|
| フロントエンド (Vue 3 SPA) | http://localhost:5173 |
| バックエンド API | http://localhost:8080 |
| API ドキュメント (Swagger UI) | http://localhost:8080/docs |
| API ドキュメント (ReDoc) | http://localhost:8080/redoc |

### 4. 停止

```powershell
# フォアグラウンド起動の場合: Ctrl+C で停止
# バックグラウンド起動の場合:
docker-compose down
```

---

## 開発のフロー

### コード変更の即時反映

`docker-compose up` の状態でコードを編集すると、自動的に反映されます。

| レイヤー | 仕組み | 反映速度 |
|---|---|---|
| フロントエンド (`.vue`, `.ts`, `.css`) | Vite HMR (Hot Module Replacement) | 即時 (~100ms) |
| バックエンド (`main.py` 等) | uvicorn `--reload` | 即時 (~1秒) |

ボリュームマウントにより、ホスト側のファイル変更がコンテナ内に即座に伝わります。
依存関係の追加 (`pip install`, `npm install`) はコンテナのリビルドが必要です:

```powershell
# 依存関係変更後 (requirements.txt / package.json を編集した場合)
docker-compose up --build
```

### ログ確認

```powershell
# 全サービスのログをリアルタイム表示
docker-compose logs -f

# api のみ
docker-compose logs -f api

# web のみ
docker-compose logs -f web
```

### コンテナ内でコマンド実行

```powershell
# API コンテナ内で Python シェル
docker-compose exec api python

# Web コンテナ内で npm コマンド
docker-compose exec web npm run build
```

---

## 個別起動 (Docker なし)

Docker Desktop が使えない環境や、起動を素早くしたい場合の手順です。

### バックエンドのみ (Python 3.12 必須)

```powershell
cd api

# 仮想環境の作成と有効化
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 依存パッケージのインストール
pip install -r requirements.txt

# 開発サーバー起動 (ホットリロード有効)
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

macOS / Linux の場合:
```bash
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

### フロントエンドのみ (Node.js 20 必須)

```powershell
cd web

# 依存パッケージのインストール
npm install

# 開発サーバー起動
npm run dev
```

> **注意**: バックエンドなしでフロントを起動した場合、API 通信部分はエラーになります。
> Phase 0 では `/api/health` の疎通確認のみなので、フロントのみでも UI 確認は可能です。

---

## トラブルシューティング

### docker-compose up でエラー

**ポートが既に使用されている**

```
Error starting userland proxy: listen tcp4 0.0.0.0:8080: bind: address already in use
```

8080 または 5173 を使用しているプロセスを確認して終了してください:

```powershell
# Windows: 使用中のポートを確認
netstat -ano | findstr :8080
netstat -ano | findstr :5173

# PID を指定してプロセスを終了 (例: PID=12345)
taskkill /PID 12345 /F
```

**Docker Desktop が起動していない**

```
error during connect: ... Is the docker daemon running?
```

タスクバーの Docker Desktop アイコンを確認し、起動してください。
Windows の場合、スタートメニューから「Docker Desktop」を起動します。

**ビルドキャッシュのクリア**

```powershell
# イメージを再ビルド (キャッシュ無視)
docker-compose build --no-cache
docker-compose up
```

**コンテナとボリュームを完全クリア**

```powershell
docker-compose down -v
docker-compose up --build
```

---

### Vite HMR が効かない

Docker 上では inotify が使えないため、Polling watcher を使用します。
`docker-compose.yml` に以下の環境変数が設定されていることを確認してください:

```yaml
environment:
  - CHOKIDAR_USEPOLLING=true
```

また `web/vite.config.ts` に `usePolling: true` が設定されていることを確認:

```typescript
server: {
  host: '0.0.0.0',
  port: 5173,
  watch: {
    usePolling: true,  // ← Docker ボリューム変更検知に必須
  },
}
```

それでも反映されない場合は、コンテナを再起動してください:

```powershell
docker-compose restart web
```

---

### API ヘルスチェックが失敗する

```powershell
# ヘルスチェック状態を確認
docker-compose ps

# API コンテナのログを確認
docker-compose logs api
```

ヘルスチェックエンドポイントを直接テスト:

```powershell
# PowerShell から
Invoke-WebRequest -Uri http://localhost:8080/api/health | Select-Object StatusCode, Content

# または curl.exe (Windows 10/11 に標準搭載)
curl.exe http://localhost:8080/api/health
```

正常なレスポンス例:
```json
{"status": "ok", "version": "0.1.0", "service": "cutflow-cad-api"}
```

---

### Phase 0 でやれること / やれないこと

| | 操作 | 状態 |
|---|---|---|
| やれること | UI の目視確認 (v3 デザイン) | Phase 0 で実装済み |
| やれること | API 疎通確認 (`/api/health`) | Phase 0 で実装済み |
| やれること | Swagger UI で API 仕様を確認 | Phase 0 で実装済み |
| やれないこと | DXF ファイルの読み込み | Phase 1 で実装予定 |
| やれないこと | 図面の表示・編集 | Phase 1 以降で実装予定 |
| やれないこと | 加工代オフセット計算 | Phase 2 で実装予定 |
| やれないこと | DXF / PDF 書き出し | Phase 3 で実装予定 |

---

## 環境変数リファレンス

現時点 (Phase 0) では `.env` ファイルは不要です。
Phase 1 以降で以下の変数が追加される予定です:

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `SESSION_DIR` | `/var/cutflow/sessions` | 一時ファイル保存先 |
| `SESSION_TTL_HOURS` | `24` | セッション自動削除時間 |
| `MAX_UPLOAD_MB` | `5` | DXF アップロード上限 (MB) |
| `CORS_ORIGINS` | `http://localhost:5173` | 許可するオリジン |

---

## 関連ドキュメント

- [README.md](../README.md) — プロジェクト概要・フェーズ一覧
- [ARCHITECTURE.md](ARCHITECTURE.md) — 技術構成・システム設計
- [DESIGN.md](DESIGN.md) — 機能仕様・UI 設計
- [ROADMAP.md](ROADMAP.md) — 段階リリース計画 (Phase 0〜5)
