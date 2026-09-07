# AWS 本番構成方針

ソフトローンチ向けのバックエンド本番方針。フロント（Nuxt）は Cloudflare Pages / Worker のまま、API（Django + Celery）だけを AWS に置く。

現行スタック（Gunicorn・Celery・Postgres・Redis・cookie セッション）をそのまま載せることを優先し、マイクロサービス化や全面サーバレス化はしない。

## リージョン

| 項目 | 値 |
| --- | --- |
| リージョン | Asia Pacific (Osaka) |
| リージョンコード | `ap-northeast-3` |

コンソール・CLI・S3・ECS・RDS・ElastiCache はすべて大阪に揃える。東京（`ap-northeast-1`）は使わない。

## 全体像

```
Cloudflare Pages / Worker                 AWS (ap-northeast-3 / Osaka)
luggo.delivery / *.luggo.delivery
        │
        │  HTTPS + cookie（credentials: include）
        ▼
   DNS ──► api.luggo.delivery
        │
        ▼
   ALB + ACM（HTTPS）
        │
   ┌────┴────┐
   ▼         ▼
 ECS Fargate              ECS Fargate
 （web / Gunicorn）        （Celery worker）
        │                       │
        └───────────┬───────────┘
              ┌─────┴─────┐
              ▼           ▼
   ElastiCache Redis 7    RDS PostgreSQL 15
   （broker + routing      （本データ）
    cache + throttle）

         S3                 … media / static
         Secrets Manager    … 秘密情報
         CloudWatch Logs    … コンテナログ
         Sentry（既存）      … アプリエラー（詳細は monitoring.md）
```

## コンポーネント方針

| 役割 | 採用 | 備考 |
| --- | --- | --- |
| API | ECS Fargate（Gunicorn） | 既存 `Dockerfile.prod` を流用 |
| 非同期ジョブ | ECS Fargate（Celery） | ルート割当・OR-Tools が重いので API と分離。Beat が必要なら別タスク |
| DB | RDS PostgreSQL 15 | 本番の前提。ローカル SQLite は使わない |
| Redis | ElastiCache Redis 7 | `CELERY_BROKER_URL` / `ROUTING_CACHE_URL`（レート制限カウンタも default キャッシュ経由） |
| ファイル | S3 + django-storages / boto3 | `.env.production.example` の AWS 変数を使う |
| 入口 | ALB + ACM | `api.luggo.delivery`。ヘルスチェックは `/api/common/health` |
| 秘密情報 | Secrets Manager（または SSM Parameter Store） | イメージやリポジトリに埋め込まない |
| ログ | CloudWatch Logs | 最低限。エラー詳細は Sentry |
| 監視 | Sentry + 外形監視 | [monitoring.md](./monitoring.md) |

## フロントとの境界

- フロント: Cloudflare（`luggo.delivery` / `www` / 事業者サブドメイン）
- API: AWS（`api.luggo.delivery`）
- ブラウザからは `credentials: "include"` で API を呼ぶ想定
- CORS / CSRF / cookie domain（`.luggo.delivery`）は `prod_settings` 側の既存方針に従う
- Stripe Webhook は ALB 経由で `/api/...` に直接届く

## 初期スペック目安

ソフトローンチ想定。負荷が見えてから上げる。

| リソース | 目安 |
| --- | --- |
| API タスク | 0.5–1 vCPU / 1–2 GB、タスク数 1–2 |
| Celery タスク | 1–2 vCPU / 2–4 GB（ソルバー用に API より厚め） |
| RDS | `db.t4g.micro`〜`small`、開始は Single-AZ 可。自動バックアップは有効 |
| Redis | `cache.t4g.micro` |

ネットワークはコスト優先ならパブリックサブネット + タスク Public IP（または VPC エンドポイント）でも可。本格運用に入ったら Private サブネット + NAT を検討する。

## 採用しないもの（現時点）

| 候補 | 理由 |
| --- | --- |
| EKS | 運用コストがソフトローンチに対して過大 |
| API Gateway + Lambda 全面移行 | 長時間 Celery・セッション cookie・WSGI と相性が悪い |
| App Runner 単独 | Celery worker の分離に向かない |
| ECS on EC2 | Fargate の方が初期運用が軽い |
| API 前段の CloudFront / WAF | 必須ではない。必要になってから |

Heroku 等の PaaS は動作確認用の代替にはなり得るが、本番の正は上記 AWS 構成とする。

## 段階的な拡張

1. **今**: 上記一式 + ECR へのイメージ push、[cd.md](./cd.md) の GitHub Actions CD
2. **安定後**: RDS Multi-AZ、ALB / ECS のオートスケール
3. **必要時**: Celery キュー分離、WAF、Read Replica、より厚いメトリクス

## アプリケーション側で揃えること

- `DJANGO_SETTINGS_MODULE=project.prod_settings`
- `DATABASE_URL` を RDS に向ける
- `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` / `ROUTING_CACHE_URL` を ElastiCache に向ける
- `ALLOWED_HOSTS=api.luggo.delivery`（ALB の IP Host は `HealthCheckMiddleware` が `/api/common/health` のみ例外処理）
- `DJANGO_ADMIN_PATH` で運営 Admin の URL を変更する（デフォルト `admin` は本番で使わない）。例: `https://api.luggo.delivery/<DJANGO_ADMIN_PATH>/`
- ALB の Admin IP 制限ルールのパス条件も、`DJANGO_ADMIN_PATH` に合わせて更新する（旧 `/admin` ルールは削除または置き換え）
- S3: `AWS_STORAGE_BUCKET_NAME` / `AWS_S3_REGION_NAME=ap-northeast-3`。認証は ECS タスクロール（Access Key は通常不要）。`prod_settings` の `STORAGES` で media を S3 に向ける
- 静的ファイル（Admin の CSS/JS 含む）は WhiteNoise が Gunicorn 経由で `/static/` を配信する。イメージビルド時の `collectstatic` が前提
- 秘密情報はタスク定義の平文ではなく Secrets Manager 参照にする
- ALB ヘルスチェックは `GET /api/common/health`（DB 疎通のみ。Redis は含めない）

環境変数の一覧は `.env.production.example` を正とする。

## 関連ドキュメント

- [ci.md](./ci.md) — PR / push 時の自動テスト
- [cd.md](./cd.md) — `main` からの ECR / ECS デプロイ
- [monitoring.md](./monitoring.md) — Sentry・外形監視・ヘルスチェック
- [rate-limiting.md](./rate-limiting.md) — 公開 API のレート制限
- [testing.md](./testing.md) — テスト方針
