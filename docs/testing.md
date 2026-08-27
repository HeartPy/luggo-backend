# テストについて

全体のテスト方針（クリティカルパス、層の役割、E2E の範囲）は **frontend リポジトリの `docs/testing.md`** を参照してください。

## このリポジトリで使うコマンド

```bash
# Django 結合・単体
python manage.py test

# E2E 用: Stripe モックを有効にして起動
E2E_STRIPE_MOCK=1 docker compose up -d backend

# E2E 用シード（通常は frontend の Playwright globalSetup が実行する）
python manage.py seed_e2e_booking
```

## E2E 関連の実装場所

| 内容 | 場所 |
| --- | --- |
| Stripe モック（DEBUG 時のみ） | `bookings/stripe_mock.py` / `project/settings.py` の `E2E_STRIPE_MOCK` |
| E2E 事業者シード | `bookings/management/commands/seed_e2e_booking.py` |

E2E 本体（Playwright）の実行は frontend リポジトリで `pnpm test:e2e` です。
