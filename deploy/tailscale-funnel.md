# Tailscale Funnel — CutFlow•CAD 社内公開手順

Phase 4 では LINE WORKS VPS から Tailscale Funnel 経由で社内ユーザに
CutFlow•CAD を公開する。本書は VPS 上での 1 回限りのセットアップと、
日常的なデプロイ手順をまとめる。詳細は `docs/DEV-SETUP.md` も参照。

## 0. 前提条件

| 項目 | 値 |
| --- | --- |
| VPS OS | Ubuntu 22.04 LTS (LINE WORKS 提供) |
| 接続情報 | `lineworks-vps-ssh-info.md` 参照 |
| Tailscale プラン | Personal (Funnel 1 ノード) または Team |
| アプリ配置先 | `/opt/cutflow-cad/` (`CUTFLOW_HOME` で上書き可) |
| 公開ポート | 443 (Tailscale Funnel が暗号化 + 公開) |
| バックエンド | `web` コンテナ → `127.0.0.1:5173` |

> Funnel は Tailscale 側で `443 / 8443 / 10000` のみ公開可能。443 を選ぶと
> URL が `https://<host>.<tailnet>.ts.net/` とポート無しになり覚えやすい。

## 1. 初回セットアップ

```bash
# 1) リポジトリ配置
sudo git clone <repo> /opt/cutflow-cad
cd /opt/cutflow-cad

# 2) コンテナ起動 (バックエンド+フロント)
sudo bash deploy/vps-deploy.sh

# 3) Tailscale をインストールしてログイン (初回のみ)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale login        # ブラウザで Tailnet 認証

# 4) Funnel を有効化
sudo bash deploy/tailscale-setup.sh
```

最後の出力に表示される `https://<host>.<tailnet>.ts.net/` が社内公開 URL。
Tailnet 名は `tailscale status` で確認できる。

## 2. 日常デプロイ

最新の `main` をデプロイし、コンテナを差し替えるだけ:

```bash
ssh <vps>
sudo bash /opt/cutflow-cad/deploy/vps-deploy.sh
```

スクリプトは `git pull --ff-only` → `docker compose down` → `up --build -d`
を実行する。Funnel 設定はそのまま継続するため再設定不要。

## 3. Funnel の停止 / 復帰

```bash
sudo tailscale funnel 443 off   # 公開停止 (Tailnet 内アクセスは継続)
sudo tailscale funnel 443 on    # 再公開
```

`tailscale serve status` で現在の公開状態を確認できる。

## 4. アクセス制御の方針

- Tailscale Funnel は URL を知っていれば誰でもアクセス可能。
- Phase 4 時点では Cookie/Auth は未実装。URL を社内チャットで配布 →
  共有先を制限する運用とする。
- 強化が必要になったタイミングで FastAPI 側に Basic Auth ミドルウェア
  を差し込むか、Cloudflare Access の `cloudflared` 経由に切替予定。

## 5. トラブルシュート

| 症状 | 確認コマンド |
| --- | --- |
| Funnel URL が 502 を返す | `docker compose ps`, `docker compose logs web` |
| Tailscale が認証切れ | `tailscale status` → expired 表示の場合 `tailscale up` |
| 443 が他プロセスに専有 | `sudo ss -tlnp | grep 443` |
| Funnel 設定の確認 | `tailscale serve status`, `tailscale funnel status` |

CORS で蹴られる場合は API 側 `CUTFLOW_CORS_ORIGINS` に Funnel URL を
追加する (compose の `environment:` ブロック)。
