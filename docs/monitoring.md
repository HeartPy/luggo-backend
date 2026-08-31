# 監視について

ソフトローンチ向けの最小構成。エラー監視（Sentry）と外形監視（アップタイム監視）の2本立てとする。
ログ集約基盤・APM・メトリクスダッシュボードはスコープ外（必要になってから導入する）。

## 構成

| 種類 | 手段 | 対象 |
| --- | --- | --- |
| エラー監視 | Sentry | Backend（Django + Celery）、Frontend（Nuxt クライアント） |
| 外形監視 | UptimeRobot 等の外部サービス | フロントのトップページ、`/api/common/health` |

## エラー監視（Sentry）

### 仕組み

- Backend: `project/prod_settings.py` で `SENTRY_DSN` が設定されているときだけ初期化される。
  未設定なら完全に無効（ローカル・テストに影響なし）。
  `DjangoIntegration` + `CeleryIntegration` を有効化し、エラー監視のみ
  （`traces_sample_rate=0`）、個人情報は送らない（`send_default_pii=False`）。
- Frontend: `sentry.client.config.ts` で `NUXT_PUBLIC_SENTRY_DSN` が設定されているときだけ
  初期化される。クライアント側のエラーのみ送信する。

### 設定手順

1. [sentry.io](https://sentry.io) でアカウントを作成（無料枠で開始可）
2. プロジェクトを2つ作成
   - `luggo-backend`（Platform: Django）
   - `luggo-frontend`（Platform: Nuxt）
3. 各プロジェクトの DSN を控える
4. 環境変数に設定
   - Backend: `.env.production` に `SENTRY_DSN=<backend の DSN>`
   - Frontend: 本番のビルド/実行環境に `NUXT_PUBLIC_SENTRY_DSN=<frontend の DSN>`
5. Sentry の Alerts で通知先（メール等）を設定
6. 動作確認: 本番相当環境で意図的に例外を発生させ、Sentry に届くことを確認

## 外形監視（アップタイム監視）

コードは不要。外部サービスに URL を登録するだけ。

### 監視対象 URL

| URL | 見るもの |
| --- | --- |
| `https://<フロントのドメイン>/` | フロントが応答するか |
| `https://<API のドメイン>/api/common/health` | API と DB が生きているか（200 / 503） |

### 設定手順（UptimeRobot の例）

1. [uptimerobot.com](https://uptimerobot.com) でアカウントを作成（無料枠で開始可）
2. Monitor を2つ作成（HTTP(s) 監視・間隔5分）
   - フロントのトップページ
   - `/api/common/health`
3. Alert Contacts に通知先メールアドレスを設定

## ヘルスチェック API

- URL: `GET /api/common/health`（認証・CSRF 不要）
- DB へ `SELECT 1` を発行し、成功なら `200 {"status": "ok"}`、失敗なら `503 {"status": "error"}`
- Redis の疎通は含めない（Redis 停止でサイト全体を「down」と誤報しないため）
- 実装: `project/views.py` の `health` / テスト: `project/tests.py`
