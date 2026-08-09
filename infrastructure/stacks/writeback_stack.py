"""起案（書き戻し）の Lambda と、その Gateway ターゲット（`writeback_pr_tool.md` §9 の L4a / L4b）。

    Runtime ──MCP──▶ Gateway ──┬─▶ Lambda: kai-tools     （読み 2 つ）
                               └─▶ Lambda: kai-writeback （書き 2 つ）── GitHub の PR まで
                                        │
                                        └─ Secrets Manager: kai/github-app

**ターゲットの登録は既定で off。** 載せると `tools/list` が 2 → 4 になってエージェントの
挙動が変わりうる（＝台本 4 問を人質に取る）ので、明示したときだけ登録する:

    cdk deploy KaiWritebackStack -c writebackTarget=on    # L4b を入れる
    cdk deploy KaiWritebackStack -c writebackTarget=off   # 退避（L4a の状態に戻る）

L4a（＝off）の判定は `check_propose.py`、L4b（＝on）の判定は `check_gateway.py` で
`tools/list` に 4 つ出ること＋`run_demo_script.py --runtime` の全問 PASS。

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
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

#: Gateway に登録するターゲット名。ツール名は `<ターゲット名>___<ツール名>` になる
TARGET_NAME = "kaiwb"

#: Gateway に見せる書きのツール定義（L4b）。
#:
#: **description が唯一の歯止めになる。** プロンプトを変えずにこの段を入れるので、
#: 「いつ呼ぶか」はここに書いた文言だけがエージェントへの指示になる。台本 4 問は
#: どれも「聞かれたことに答える」だけなので、**明示的に頼まれたときしか呼ばない**と
#: 言い切っておく。判定は `run_demo_script.py` の全問 PASS（ツールが 2 つ増えて
#: 挙動が変わらないこと）。
WRITE_TOOL_SCHEMA = [
    {
        "name": "propose_knowledge",
        "description": (
            "案件 KAI の正本に新しい knowledge を1件起案する（PR を立てるだけで、"
            "正本に入るのは人がマージした時点）。"
            "**人から「知見として残して」「これを記録して」と明示的に頼まれたときだけ使う。**"
            "質問に答えるだけのとき、設計のズレを指摘するだけのときは呼ばない。"
            "同じ内容が既にあるなら propose_append を使う。"
            "decision（正式な意思決定）は人が起こすので、ここでは起案できない。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "description": (
                        "1 件だけ。path は knowledge_base/knowledge/KNW-<連番>.md、"
                        "content は front matter 込みの全文"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                },
                "summary": {"type": "string", "description": "PR タイトルになる 1 行"},
                "based_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "根拠にした doc_id。空だと拒否される",
                },
                "source_url": {"type": "string", "description": "元になったやりとりの URL"},
                "requested_by": {"type": "string", "description": "誰の依頼か"},
            },
            "required": ["files", "summary", "based_on", "source_url"],
        },
    },
    {
        "name": "propose_append",
        "description": (
            "既存の knowledge の**末尾に**追記する起案（PR を立てるだけ）。"
            "**人から明示的に頼まれたときだけ使う。**"
            "既存の記述は書き換えられない（末尾に足すことしかできない）。"
            "同じ論点の doc が既にあるときは、新規で起こすよりこちらを優先する。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "追記先。例 KNW-002"},
                "body": {"type": "string", "description": "追記する本文だけ（見出しと日付は自動で付く）"},
                "based_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "根拠にした doc_id。空だと拒否される",
                },
                "source_url": {"type": "string", "description": "元になったやりとりの URL"},
                "requested_by": {"type": "string", "description": "誰の依頼か"},
            },
            "required": ["doc_id", "body", "based_on", "source_url"],
        },
    },
]

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
        tools=None,
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

        # ------------------------------------------------------------------
        # Gateway に 2 つめのターゲットとして登録する（L4b）
        # ------------------------------------------------------------------
        # **既定では登録しない。** 登録すると `tools/list` が 2 → 4 になり、
        # エージェントの挙動が変わりうる（＝台本 4 問を人質に取る）。
        # 入れるときは明示する:
        #   cdk deploy KaiWritebackStack -c writebackTarget=on
        # 落ちたら `-c writebackTarget=off` で戻せる ―― これが L4b の退避ライン。
        target_on = str(self.node.try_get_context("writebackTarget") or "off").lower() == "on"

        if target_on and tools is not None:
            # **ロール側ではなく Lambda 側に許可を書く。** Gateway のサービスロールは
            # KaiToolsStack の持ち物なので、そちらに grant を足すと
            # 「ToolsStack → WritebackStack（関数 ARN）」と
            # 「WritebackStack → ToolsStack（Gateway ID）」で循環する。
            # リソースベースのポリシーなら参照は片方向で済む
            writeback.add_permission(
                "AllowGatewayInvoke",
                principal=iam.ArnPrincipal(tools.gateway_role.role_arn),
                action="lambda:InvokeFunction",
            )

            agentcore.CfnGatewayTarget(
                self,
                "WritebackGatewayTarget",
                gateway_identifier=tools.gateway.attr_gateway_identifier,
                name=TARGET_NAME,
                credential_provider_configurations=[
                    agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                        credential_provider_type="GATEWAY_IAM_ROLE",
                    )
                ],
                target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                    mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                        lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                            lambda_arn=writeback.function_arn,
                            tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                                inline_payload=[
                                    agentcore.CfnGatewayTarget.ToolDefinitionProperty(
                                        name=t["name"],
                                        description=t["description"],
                                        input_schema=t["inputSchema"],
                                    )
                                    for t in WRITE_TOOL_SCHEMA
                                ],
                            ),
                        ),
                    ),
                ),
            )

        CfnOutput(
            self,
            "WritebackFunctionName",
            value=writeback.function_name,
            description="起案 Lambda。Gateway に載せるかは -c writebackTarget=on|off",
        )
        CfnOutput(
            self,
            "WritebackGatewayTargetState",
            value=f"{TARGET_NAME} ({'on' if target_on else 'off'})",
            description="Gateway のターゲット登録状態。off ならエージェントからは見えない",
        )
        CfnOutput(
            self,
            "GitHubAppSecretName",
            value=secret.secret_name,
            description="App ID と秘密鍵を入れる先（値は本人が put-secret-value で入れる）",
        )
