# project-agent

案件の decision / knowledge を根拠付きで参照する Slack 常駐エージェント（v1 Project
Context Agent）。技術勉強会の登壇デモとして作っている。

段階的に積み上げる。**1 段 = 1 つの差し替え、判定は毎回 `run_demo_script.py` の全問 PASS。**
現在 **L3（Gateway）まで完了**。

| 段 | 中身 | 状態 |
| --- | --- | --- |
| L0 | ローカルの Strands + `knowledge_base/` 直読み。台本 4 問に根拠付きで答えられる | ✅ |
| L1a | 正本を S3 に置く（読み口は直読みのまま。KB はまだ作らない） | ✅ |
| L1b | Managed KB を作って Ingestion（`search` はまだ差し替えない） | ✅ |
| L1c | `search` の中身を KB の Retrieve に差し替え | ✅ |
| L1d | AgentCore Runtime にデプロイ | ✅ |
| L2 | Slack App + API GW + Lambda×2 | ✅ |
| L3a | ツールを Lambda に出し Gateway の MCP ターゲットとして公開（エージェントは繋がない） | ✅ |
| L3b | エージェントを Gateway 経由の MCP に差し替え | ✅ |

## 構成

| パス | 中身 |
| --- | --- |
| [knowledge_base/](knowledge_base/) | Bedrock Knowledge Base のデータソース。デモ用の**完全架空**案件データ。仕様は [knowledge_base/README.md](knowledge_base/README.md) |
| [code/tools/core.py](code/tools/core.py) | ツールの実体（`search_project_knowledge` / `get_document`）。L0〜L3 で I/F を変えない |
| [code/tools/local_tools.py](code/tools/local_tools.py) | Strands `@tool` の薄いラッパ（L0〜L2 の呼ばれ方） |
| [code/tools/lambda_handler.py](code/tools/lambda_handler.py) | Gateway の Lambda ターゲット用の薄いラッパ（L3 の呼ばれ方） |
| [code/tools/gateway.py](code/tools/gateway.py) | Gateway に MCP で繋ぐ側。SigV4 署名とエンドポイントの解決 |
| [code/runtime/prompts.py](code/runtime/prompts.py) | 出力契約（3 ブロック + 不変ルール 5 つ） |
| [code/runtime/agent.py](code/runtime/agent.py) | Agent の組み立て。L1 で AgentCore のラッパを足す |
| [code/scripts/ask.py](code/scripts/ask.py) | L0 の入口（CLI）。L2 で Slack に置き換わる |
| [code/scripts/run_demo_script.py](code/scripts/run_demo_script.py) | 台本 4 問の連続実行と判定 |
| [code/scripts/seed_knowledge.py](code/scripts/seed_knowledge.py) | 正本を S3 に同期し `.metadata.json` を生成し、KB の Ingestion まで回す |
| [code/scripts/check_retrieve.py](code/scripts/check_retrieve.py) | KB の引き当てだけを確かめる（エージェントを通さない） |
| [code/scripts/check_gateway.py](code/scripts/check_gateway.py) | Gateway の MCP を直接叩いて確かめる（エージェントを通さない） |
| [code/scripts/teardown.sh](code/scripts/teardown.sh) | デモ用リソースの後片付け |
| [code/slack/ingress.py](code/slack/ingress.py) | Slack の受け口。署名検証して worker に非同期で投げ、即 200 を返す |
| [code/slack/worker.py](code/slack/worker.py) | 「考え中…」を先に投げ、`InvokeAgentRuntime` の結果で `chat.update` する |
| [infrastructure/](infrastructure/) | CDK (Python)。`KaiKnowledgeStack` が S3 と Managed KB、`KaiToolsStack` がツール Lambda と Gateway、`KaiSlackStack` が Slack の入口を持つ |
| [code/runtime/app.py](code/runtime/app.py) | AgentCore Runtime の入口。`build_agent()` を呼ぶだけ |
| [agentcore/](agentcore/) | AgentCore CLI の設定と CDK。`aws-targets.json` は各自で作る |
| [.github/workflows/seed-knowledge.yml](.github/workflows/seed-knowledge.yml) | main へのマージで `seed_knowledge.py` を回す。PR では front matter の検証だけ |

## 使い方

```sh
uv sync --extra dev
```

ツール単体（AWS 不要）:

```sh
uv run python -m tools.core "マルチテナント 物理分離"
uv run python -m tools.core --doc DEC-003a
uv run pytest
```

エージェント（Bedrock を呼ぶ）:

```sh
uv run python code/scripts/ask.py "非同期処理は Step Functions で組む予定です"
uv run python code/scripts/run_demo_script.py --repeat 3
```

正本を S3 に置き、KB に取り込む（L1a / L1b）:

```sh
npx -y aws-cdk@2.1134.0 deploy KaiKnowledgeStack
uv run python code/scripts/seed_knowledge.py
```

`seed_knowledge.py` は S3 に同期したあと Ingestion job を起こして完了まで待つ
（`--no-ingest` で同期だけ）。索引の出来は**エージェントを通さずに**確かめる:

```sh
uv run python code/scripts/check_retrieve.py --show
```

CDK CLI を `npx` で固定しているのは、`aws-cdk-lib` 2.263 が CLI 2.1134 以上を要求する一方、
グローバルに入れた CLI がそれより古いことがあるため。バケット名は
`seed_knowledge.py` がスタックの出力から自動で引く。

### 同期を CI に任せる

`knowledge_base/` を触る PR が main にマージされると
[seed-knowledge.yml](.github/workflows/seed-knowledge.yml) が同じスクリプトを流す。
PR の側では AWS に触らず `--dry-run` で front matter を検証するだけなので、
壊れた Markdown は**マージ前に**落ちる。一度だけ以下の設定が要る:

```sh
npx -y aws-cdk@2.1134.0 deploy KaiKnowledgeStack -c runtimeRoleArn=<RuntimeRoleArn>
gh secret set AWS_SEED_ROLE_ARN --body <出力された SeedKnowledgeRoleArn>
```

L1d 以降は `-c runtimeRoleArn` を**必ず付ける**。付け忘れると `if runtime_role_arn:` が
素通りして、Runtime ロールに与えた `bedrock:Retrieve` と `s3:GetObject` が
差分として消える（エージェントが KB を読めなくなる）。`cdk diff` に
`[-] AWS::IAM::Policy RuntimeRole/Policy` が出たらそれ。

ロールは GitHub の OIDC で引き受ける（アクセスキーは置かない）。信頼するのは
**このリポジトリの main の ref だけ**で、権限も `knowledge_base/` prefix への
読み書きと Ingestion job の起動に限る。`raw/` には触れない。
OIDC プロバイダはアカウントに 1 つしか作れないため CDK では作らず ARN で参照している。
無いアカウントで使うなら先に `aws iam create-open-id-connect-provider` が要る。

読み口を S3 に向けて台本を流すと、ローカル直読みと**同じ答え**が返る（L1a の完了条件）:

```sh
export KAI_KNOWLEDGE_SOURCE=s3
uv run python code/scripts/run_demo_script.py
```

検索そのものを KB に差し替える（L1c）:

```sh
export KAI_SEARCH=kb KAI_KB_ID=...   # ID は seed_knowledge.py の出力に出る
uv run python code/scripts/run_demo_script.py --repeat 3
```

AgentCore Runtime にデプロイして、そこで台本を流す（L1d）:

```sh
cp agentcore/aws-targets.json.example agentcore/aws-targets.json   # 自分のアカウントを書く
npx -y @aws/agentcore@latest deploy --target personal --yes
npx -y aws-cdk@2.1134.0 deploy KaiKnowledgeStack -c runtimeRoleArn=<出力された RoleArn>
uv run python code/scripts/run_demo_script.py --runtime --repeat 3
```

Runtime のロールは AgentCore CLI 側の CDK が作るが、**KB とバケットは
`KaiKnowledgeStack` の持ち物**なので、そこへのアクセス許可は後者から context 経由で
与える。§5.2 のとおり `s3:PutObject` は与えない（エージェントは read-only）。

Slack から使えるようにする（L2）:

```sh
npx -y aws-cdk@2.1134.0 deploy KaiSlackStack -c runtimeArn=<出力された RuntimeArn>
```

`KaiSlackStack` は **Secrets Manager の箱だけ作り、値は入れない**（`REPLACE_ME` のまま）。
Bot Token と Signing Secret は**本人が**入れる:

```sh
aws secretsmanager put-secret-value --secret-id kai/slack \
  --secret-string '{"bot_token":"xoxb-...","signing_secret":"..."}'
```

Slack App 側では、出力された `SlackEventsUrl` を **Event Subscriptions** の Request URL に
登録し、**Subscribe to bot events に `app_mention` を足して Save**、そのうえで
**Reinstall to Workspace** する。必要なスコープは `app_mention:read` と `chat:write`。

**Socket Mode は Off にする。** ON のままだと Slack は WebSocket でしか配信せず、
Request URL が Verified でも API Gateway には何も届かない（§10-22）。

届いているかは ingress のログで分かる（**API Gateway に到達していなければ Slack 側の設定**）:

```sh
aws logs tail /aws/lambda/<SlackIngress の関数名> --follow
```

ツールを Lambda に出して Gateway の MCP ターゲットにする（L3a）:

```sh
npx -y aws-cdk@2.1134.0 deploy KaiToolsStack -c runtimeRoleArn=<出力された RoleArn>
```

インバウンド認証は **`AWS_IAM`**。Cognito は使わない（トークンの取得も更新も無く、
呼べるのは `bedrock-agentcore:InvokeGateway` を持つ主体だけ）。エージェントを通さずに
MCP を直接叩いて確かめる:

```sh
uv run python code/scripts/check_gateway.py
```

エージェントを Gateway 経由に差し替える（L3b）。`agentcore.json` の `envVars` に
`KAI_TOOLS=gateway` を足してデプロイし直す:

```sh
npx -y @aws/agentcore@latest deploy --target personal --yes
uv run python code/scripts/run_demo_script.py --runtime --repeat 3
```

`KAI_TOOLS` を `local` に戻せば、**同じコードのまま**プロセス内 `@tool` に戻る
（§4.3 の退避ライン）。差し替わるのはツールの呼ばれ方だけで、`core.py` の I/F も
`prompts.py` も動かない。

`run_demo_script.py` は台本 4 問（矛盾／superseded／暗黙知／根拠なし）を流し、
期待する doc_id を根拠に挙げているかを機械的に判定する。**当日前にこれを複数回流して
回答が安定していることを確認する。**

### 環境変数

| 変数 | 既定 | 用途 |
| --- | --- | --- |
| `KAI_KNOWLEDGE_SOURCE` | `local` | 正本の読み口。`s3` に切り替えると S3 を読む |
| `KAI_KNOWLEDGE_BUCKET` | — | `s3` のときのバケット名 |
| `KAI_SEARCH` | `local` | 検索の実装。`kb` にすると Managed KB の Retrieve を使う |
| `KAI_TOOLS` | `local` | ツールの呼ばれ方。`gateway` にすると Gateway 経由の MCP になる |
| `KAI_GATEWAY_URL` | — | 省略時は `KaiToolsStack` の出力から引く |
| `KAI_KB_ID` | — | `kb` のときの Knowledge Base ID |
| `KAI_RERANK` | `managed` | マネージド reranker。`none` で切る |
| `KAI_MODEL_ID` | `global.anthropic.claude-sonnet-5` | Bedrock の推論プロファイル |
| `AWS_REGION` | `ap-northeast-1` | リージョン |

## セットアップ

clone 後に一度だけ実行する。どちらも git config / direnv のローカル設定なので、
リポジトリに設定ファイルが入っていても自動では有効にならない。

```sh
direnv allow                          # .envrc を許可する
git config core.hooksPath .githooks   # pre-commit hook を有効にする
```

依存ツール:

```sh
brew install direnv gitleaks
```

`direnv` は shell hook の設定も必要（`eval "$(direnv hook zsh)"` を `.zshrc` に）。

## AWS

このリポジトリでの作業は AWS の `personal` プロファイルを既定で使う。
`--profile` を毎回指定する必要はない。

| ファイル | 効く範囲 |
| --- | --- |
| [.envrc](.envrc) | ターミナル。direnv がこのディレクトリで `AWS_PROFILE` を設定する |
| [.claude/settings.json](.claude/settings.json) | Claude Code が実行するコマンド |

`.claude/settings.json` の変更はセッションを開始し直すまで反映されない。

## 秘匿情報の扱い

**public リポジトリなので、認証情報をコミットしないこと。**

環境変数として秘匿値が必要になったら `.envrc.local` に書く。gitignore 済みで、
direnv が `.envrc` から自動で読み込む。

多層で防いでいる:

1. [.gitignore](.gitignore) — `.env*`、鍵ファイル、`.envrc.local` を追跡対象から外す
2. [.githooks/pre-commit](.githooks/pre-commit) — コミット前に gitleaks でステージ済み差分を検査する
3. [.gitleaks.toml](.gitleaks.toml) — 検出ルール。gitleaks の既定ルールは AWS
   アクセスキーと秘密鍵ブロックを検出しないため、ここで補っている
4. GitHub の secret scanning / push protection — push 時のサーバー側チェック

pre-commit hook は `--no-verify` で回避でき、clone 先では設定するまで動かない。
事故防止であって保証ではないので、最後の砦は GitHub 側という前提で扱う。

gitleaks が誤検知したときは、該当行末に `gitleaks:allow` コメントを付けるか、
出力された Fingerprint を `.gitleaksignore` に追記する。
