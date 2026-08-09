"""起案（書き戻し）の Lambda（`writeback_pr_tool.md` §9 の L4a）。

**この段では作るだけで、Gateway には登録しない。** エージェントからは見えないので、
台本 4 問の挙動を1文字も変えない（L1b で KB を作るだけ作って差し替えなかったのと
同じ形）。判定は `check_propose.py` で直接呼ぶ。

    Runtime ──▶（L4b でここに線がつながる）
                    │
                    ▼
              Lambda: kai-writeback ──▶ GitHub（PR を立てるところまで）
                    │
                    └─ Secrets Manager: kai/github-app（App ID と秘密鍵）

**なぜ読みのツールと別スタック・別 Lambda なのか**（§6）:

- **このロールだけが GitHub App の秘密鍵を読める。** 読みの Lambda と Runtime には
  与えない。「エージェント自身は正本に書けない」を IAM で表現する
  （`s3:PutObject` を与えないのと同じ手）
- 壊れ方を分ける。起案の不具合で `search_project_knowledge` が落ちてはいけない
"""

import json
import pathlib
import shutil
import subprocess

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

#: 起案先。**架空案件 KAI の正本リポジトリ**（public）
DEFAULT_REPO = "Yusuke-Shimizu/kai-knowledge"

#: Lambda に同梱する依存。
#:
#: - `pyyaml` … `core.py` が front matter の解析に使う
#: - `pyjwt[crypto]` … GitHub App の JWT（RS256）。インストールトークンを取るのに要る。
#:   **HTTP は `urllib` で書いてあるので `requests` は入れない**（Slack の worker と同じ方針）
RUNTIME_DEPENDENCIES = ["pyyaml", "pyjwt[crypto]"]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD_DIR = REPO_ROOT / ".build" / "writeback_lambda"


def _build_asset() -> str:
    """`code/tools/` と依存を1つのディレクトリにまとめる。

    `tools_stack.py` と同じやり方。Docker を使うバンドリングは避けている
    ―― 何がどう入るかがその場で読める形にしておくため（§10-14）。
    """
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True)

    shutil.copytree(REPO_ROOT / "code" / "tools", BUILD_DIR / "tools")
    subprocess.run(
        [
            "uv", "pip", "install", "--quiet",
            "--target", str(BUILD_DIR),
            # Lambda は x86_64（既定）。手元の macOS の wheel を入れないよう明示する
            "--python-platform", "x86_64-manylinux2014",
            "--only-binary", ":all:",
            *RUNTIME_DEPENDENCIES,
        ],
        check=True,
    )
    return str(BUILD_DIR)


class WritebackStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        writeback_repo: str = DEFAULT_REPO,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # 秘匿情報の箱（値は入れない）
        #
        # KaiSlackStack と同じ扱い。**GitHub App の秘密鍵は本人が入れる。**
        # CDK に鍵を通す経路を作らないこと自体が防御になる
        # ------------------------------------------------------------------
        secret = secretsmanager.Secret(
            self,
            "GitHubAppSecret",
            secret_name="kai/github-app",
            description="GitHub App id and private key for writeback (fill in manually)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps(
                    {"app_id": "REPLACE_ME", "private_key": "REPLACE_ME"}
                ),
                generate_string_key="unused",
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        writeback = lambda_.Function(
            self,
            "WritebackFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="tools.writeback_handler.handler",
            code=lambda_.Code.from_asset(_build_asset()),
            # GitHub を数回叩く。20 秒あれば足りるが、コールドスタートを見て 60 秒
            timeout=Duration.seconds(60),
            memory_size=512,
            log_retention=logs.RetentionDays.ONE_WEEK,
            tracing=lambda_.Tracing.ACTIVE,
            environment={
                "KAI_WRITEBACK_REPO": writeback_repo,
                "KAI_WRITEBACK_BASE": "main",
                "KAI_GITHUB_APP_SECRET_ID": secret.secret_name,
                "KAI_PROPOSAL_LABEL": "proposed-by-agent",
                # open な起案 PR の上限（§2 のバックプレッシャー）。
                # 詰まっているときは新しく起案しない
                "KAI_MAX_OPEN_PROPOSALS": "5",
            },
        )

        # **秘密鍵を読めるのはこの関数だけ。** Runtime にも読みの Lambda にも与えない
        secret.grant_read(writeback)

        CfnOutput(
            self,
            "WritebackFunctionName",
            value=writeback.function_name,
            description="起案 Lambda。L4a ではまだ Gateway に登録しない",
        )
        CfnOutput(
            self,
            "GitHubAppSecretName",
            value=secret.secret_name,
            description="App ID と秘密鍵を入れる先（値は本人が put-secret-value で入れる）",
        )
