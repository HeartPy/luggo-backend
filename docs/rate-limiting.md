# 公開 API のレート制限

悪用されやすい公開 API（`AllowAny`）に、IP 単位のレート制限（DRF throttle）を掛けている。
実装は `project/throttling.py`、適用は各ビューの `@throttle_classes`。

## 役割分担

| レイヤー | 対象 | 実装 |
| --- | --- | --- |
| Turnstile | ボットかどうか | `project/turnstile.py`（フロントの `NuxtTurnstile` とセット） |
| ログイン失敗 IP ブロック | 認証失敗の連続（5分10回 → 30分ブロック） | `users/utils.py` |
| **レート制限（本書）** | 成否に関わらずリクエスト回数そのもの | `project/throttling.py` |

## スコープと対象 API

| スコープ | デフォルト | 対象 |
| --- | --- | --- |
| `login` | 10/min | ログインコード送信・検証、配達者ログイン、事業者登録完了 |
| `email-send` | 20/hour | パスワード再設定リクエスト、登録メール送信 |
| `public-read` | 30/min | 予約照会、登録トークン検証、パスワード再設定トークン検証 |
| `payment` | 20/min | PaymentIntent 作成 |

ホテル Wi-Fi など共有 IP からの正規利用（旅行者）を想定し、緩めの値にしている。
上限超過時は DRF 標準の 429 レスポンス（`detail` に待ち時間入りメッセージ）。

## 設定（環境変数）

| 変数 | デフォルト | 説明 |
| --- | --- | --- |
| `THROTTLE_ENABLED` | `not DEBUG`（本番のみ有効） | レート制限の有効/無効 |
| `THROTTLE_LOGIN_RATE` | `10/min` | `login` スコープ |
| `THROTTLE_EMAIL_SEND_RATE` | `20/hour` | `email-send` スコープ |
| `THROTTLE_PUBLIC_READ_RATE` | `30/min` | `public-read` スコープ |
| `THROTTLE_PAYMENT_RATE` | `20/min` | `payment` スコープ |

レートは「回数/期間」形式（期間: `s` / `min` / `hour` / `day`）。

- 開発・ユニットテスト・E2E は `DEBUG=True` のためデフォルト無効。手元で動作確認するときだけ `THROTTLE_ENABLED=True` にする
- 本番（`prod_settings`）はデフォルト有効。無効化したい場合のみ `THROTTLE_ENABLED=False`

## 本番の前提

- カウンタは default キャッシュに保存する。本番は `ROUTING_CACHE_URL`（ElastiCache Redis）が
  default キャッシュのため、ECS の複数タスク間でカウンタが共有される
- クライアント IP は `X-Forwarded-For` の**末尾**を採用する
  （`users/utils.py` の `get_client_ip`。先頭はクライアントが偽装できるため使わない）。
  ALB の前段にプロキシ（CloudFront / Cloudflare 等）を挟む構成に変えた場合は、
  この取り方を見直すこと

## テスト

`project/tests.py` の `ThrottleTests` / `GetClientIpTests`。
DRF の `THROTTLE_RATES` は import 時に settings を参照するクラス属性のため、
テストでは `override_settings` ではなく `patch.dict` でレートを差し替えている。
