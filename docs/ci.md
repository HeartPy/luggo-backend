# CI について

GitHub Actions で PR ごとに自動テストを回す。ワークフローは `.github/workflows/ci.yml`。

## 実行タイミング

- `develop` / `main` への Pull Request
- `develop` / `main` への push（マージ後の再確認）

## 実行内容

| ステップ | 内容 |
| --- | --- |
| 依存インストール | `uv pip install -r requirements/development.txt`（Python 3.11） |
| 環境ファイル | `.env.development.example` を `.env.development` にコピー |
| テスト | `python manage.py test`（Postgres 15 + Redis 7 のサービスコンテナに接続） |

ローカルと違い、CI では `DATABASE_URL` を Postgres に向けてテストする（本番同等）。

## ブランチ保護の設定（GitHub 上で手動・初回のみ）

CI が通らない PR をマージ不可にするには、GitHub リポジトリで以下を設定する。

1. リポジトリの **Settings → Branches → Add branch protection rule** を開く
2. **Branch name pattern** に `develop` を入力
3. **Require status checks to pass before merging** にチェック
4. 検索ボックスで `test`（CI ワークフローのジョブ名）を選択
5. 保存し、`main` にも同じルールを作成する

※ ステータスチェックの候補は、一度 CI が実行された後でないと検索に出てこない。
先に PR を1本作って CI を走らせてから設定するとよい。

## スコープ外

- 自動デプロイ（CD）— 本番デプロイ手順が固まってから別途整備
- E2E（Playwright）— frontend リポジトリでローカル実行。CI 化は安定後に検討
