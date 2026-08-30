# テストについて

全体のテスト方針（クリティカルパス、層の役割、E2E の範囲）は **frontend リポジトリの `docs/testing.md`** を参照してください。

## このリポジトリで使うコマンド

```bash
# Django 結合・単体
python manage.py test
```

## テストファイルの配置

- テストが **1 ファイルだけ**のアプリは `アプリ/tests.py` でよい
  （例: `users/tests.py`、`routing/tests.py`、`administrators/tests.py`）
- テストが **2 ファイル以上**になったら `アプリ/tests/` パッケージにまとめ、
  ファイル名は `test_*.py` に統一する（`tests.py` は置かない）
  （例: `bookings/tests/`、`drivers/tests/`、`business_owners/tests/`）
- 共通ヘルパーは同パッケージ内の `helpers.py` に置く
  （例: `bookings/tests/helpers.py`）

## E2E 関連

```bash
# E2E 用: Stripe・ログインコードのモックを有効にして起動
E2E_STRIPE_MOCK=1 E2E_LOGIN_CODE_MOCK=1 docker compose up -d backend

# E2E 用シード（通常は frontend の Playwright globalSetup が実行する）
python manage.py seed_e2e_booking
```

| 内容 | 場所 |
| --- | --- |
| Stripe モック（DEBUG 時のみ） | `bookings/stripe_mock.py` / `project/settings.py` の `E2E_STRIPE_MOCK` |
| ログインコード固定モック（DEBUG 時のみ・`000000` 固定） | `users/utils.py` / `project/settings.py` の `E2E_LOGIN_CODE_MOCK` |
| E2E シード（事業者・配達者・owner-booking / driver-delivery 用予約） | `bookings/management/commands/seed_e2e_booking.py` |

E2E 本体（Playwright）の実行は frontend リポジトリで `pnpm test:e2e` です。
