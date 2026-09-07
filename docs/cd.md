# CD（本番自動デプロイ）について

`main` への push（または Actions の手動実行）で、ECR へイメージを push し ECS（API / Celery）を更新する。
ワークフローは `.github/workflows/deploy.yml`。

CI（テスト）とは別。テストは従来どおり [ci.md](./ci.md)。

## デプロイの流れ

1. `Dockerfile.prod` を `linux/amd64` で build
2. ECR `luggo-backend` に `{git sha}` と `latest` を push
3. ECS クラスター `luggo` の API / Celery サービスを force-new-deployment
4. 安定を待つ
5. GitHub Variables にサブネット等があれば `migrate` ワンショットを実行

## 初回セットアップ（AWS）

GitHub Actions から永久 Access Key を置かず、**OIDC** でロールを引き受ける。

### 1. GitHub OIDC プロバイダ（アカウントに未作成なら）

1. IAM → **ID プロバイダ** → **プロバイダを追加**
2. 設定例:

| 項目 | 値 |
| --- | --- |
| プロバイダのタイプ | OpenID Connect |
| プロバイダの URL | `https://token.actions.githubusercontent.com` |
| 対象者（Audience） | `sts.amazonaws.com` |

### 2. デプロイ用 IAM ロール

1. IAM → **ロールを作成** → **ウェブアイデンティティ** → 上記 OIDC
2. ロール名例: `luggo-github-deploy`
3. 信頼ポリシーの条件で、**このリポジトリの main のみ**に絞る（例）:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<AWS_ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:<GITHUB_ORG_OR_USER>/<BACKEND_REPO>:ref:refs/heads/main"
        }
      }
    }
  ]
}
```

`<GITHUB_ORG_OR_USER>/<BACKEND_REPO>` と `<AWS_ACCOUNT_ID>` は自分のものに置き換える。

4. 許可ポリシー（インライン例。`<AWS_ACCOUNT_ID>`・名前は環境に合わせる）:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ECRAuth",
      "Effect": "Allow",
      "Action": ["ecr:GetAuthorizationToken"],
      "Resource": "*"
    },
    {
      "Sid": "ECRPush",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:CompleteLayerUpload",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:DescribeRepositories"
      ],
      "Resource": "arn:aws:ecr:ap-northeast-3:<AWS_ACCOUNT_ID>:repository/luggo-backend"
    },
    {
      "Sid": "ECSDeploy",
      "Effect": "Allow",
      "Action": [
        "ecs:UpdateService",
        "ecs:DescribeServices",
        "ecs:DescribeTasks",
        "ecs:RunTask",
        "ecs:DescribeTaskDefinition",
        "ecs:RegisterTaskDefinition"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PassRolesForRunTask",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
        "arn:aws:iam::<AWS_ACCOUNT_ID>:role/luggo-ecs-execution-role",
        "arn:aws:iam::<AWS_ACCOUNT_ID>:role/luggo-ecs-task-role"
      ]
    }
  ]
}
```

`RunTask`（migrate）を使わないなら `PassRole` / `RunTask` は外してよい。

5. ロールの ARN を控える
   例: `arn:aws:iam::<AWS_ACCOUNT_ID>:role/luggo-github-deploy`

## 初回セットアップ（GitHub）

バックエンドリポジトリ（`backend/.github` がルートのリポジトリ）で設定する。

### Secrets

| Name | 値 |
| --- | --- |
| `AWS_ROLE_TO_ASSUME` | 上記デプロイロールの ARN |

### Variables（Repository variables）

| Name | 例 | 必須 |
| --- | --- | --- |
| `ECS_SERVICE_API` | `luggo-api` | デフォルトあり |
| `ECS_SERVICE_CELERY` | `luggo-api-celery-worker-service` | デフォルトあり |
| `ECS_TASK_DEFINITION_API` | `luggo-api` | migrate 用。デフォルトあり |
| `ECS_CONTAINER_API` | `api` | migrate 用。デフォルトあり |
| `ECS_SUBNETS` | `subnet-aaa,subnet-bbb` | migrate する場合 |
| `ECS_SECURITY_GROUP` | `sg-...`（`luggo-ecs-sg`） | migrate する場合 |

サービス名やコンテナ名がデフォルトと違う場合は Variables で上書きする。
サブネットは API サービスと同じ（パブリック + Public IP）を指定する。

## 使い方

- **自動**: `main` にマージ／push すると Deploy が走る
- **手動**: Actions → **Deploy** → **Run workflow**（migrate の ON/OFF 可）

マイグレーションだけ先に手動で流したい場合は、従来どおり ECS Run task でもよい。

## 注意

- タスク定義のイメージが `.../luggo-backend:latest` であることが前提（force 再デプロイで新 `latest` を取る）
- `develop` ではデプロイしない（本番のみ）
- フロントの CD は Cloudflare Pages（フロントリポジトリ側）

## トラブルシュート

| 症状 | 確認 |
| --- | --- |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | 信頼ポリシーの `sub`（リポジトリ名・branch）・Audience |
| ECR push 拒否 | ロールの ECR 権限・リポジトリ名 `luggo-backend` |
| サービス更新失敗 | サービス名 Variables、クラスター `luggo`、リージョン大阪 |
| migrate スキップのまま | `ECS_SUBNETS` / `ECS_SECURITY_GROUP` 未設定 |
| migrate exit ≠ 0 | CloudWatch ログ・`DATABASE_URL`・SG（RDS 5432） |
