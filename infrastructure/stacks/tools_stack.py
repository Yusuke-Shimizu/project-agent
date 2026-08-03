"""ツールを Lambda に出し、AgentCore Gateway の MCP ターゲットとして公開する（L3a）。

architecture_v1.md §4.3。**この段では公開するだけで、エージェントはまだ Gateway を
見ていない**（L1b で KB を作るだけ作って差し替えなかったのと同じ形）。判定は
`check_gateway.py` で MCP を直接叩き、`tools/list` と `tools/call` を確かめる。

    Runtime ──MCP(SigV4)──▶ Gateway ──▶ Lambda: kai-tools ──▶ Managed KB / S3
                                            └ tools/core.py（L0 から不変）

**インバウンド認証は `AWS_IAM`。** §4.3 は Cognito の JWT（M2M）を前提に書いていたが、
`AuthorizerType` に `AWS_IAM` があるので、Cognito ユーザープールもクライアント
シークレットもトークンの更新も丸ごと不要になる。呼べるのは
`bedrock-agentcore:InvokeGateway` を持つ主体だけ ―― **権限を IAM で表現する**という
§5.2 の方針とも揃う。
"""

import json
import pathlib
import shutil
import subprocess

from aws_cdk import Aws, CfnOutput, Duration, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

#: Gateway に登録するターゲット名。ツール名は `<ターゲット名>___<ツール名>` になる
TARGET_NAME = "kai"

#: Lambda に同梱する依存。
#:
#: - `pyyaml` … core.py が front matter の解析に使う
#: - `boto3` … **Lambda 同梱の boto3 は Managed KB の `managedSearchConfiguration`
#:   を知らない**（`Unknown parameter in retrievalConfiguration` で落ちる）。
#:   §10-10 で「`vectorSearchConfiguration` は使えない」と書いた裏返しで、
#:   ランタイム同梱の SDK が新しい API に追いついていない。自分で持ち込む
RUNTIME_DEPENDENCIES = ["pyyaml", "boto3"]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD_DIR = REPO_ROOT / ".build" / "tools_lambda"


def _build_asset() -> str:
    """`code/tools/` と依存を1つのディレクトリにまとめる。

    Docker を使うバンドリング（`aws-lambda-python-alpha` など）は避けた。**L1d で
    「ビルドが通ったことを根拠にできない」を踏んだ**（§10-14）ので、何がどう入るかが
    その場で読める形にしておく。
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


#: Gateway に見せるツール定義。**`core.py` のシグネチャと 1 対 1**。
#: `local_tools.py` では docstring と型注釈から Strands が自動生成していた部分を、
#: Gateway 経由では自分で書く。**書き写す先が増えただけで、契約は同じ**。
TOOL_SCHEMA = [
    {
        "name": "search_project_knowledge",
        "description": (
            "案件 KAI の正本（decision / knowledge / meeting）を検索する。"
            "設計方針・制約・過去の決定について確認したいときに使う。"
            "結果には本文全文が含まれるので、追加で本文を取りに行く必要はない。"
            "該当が無ければ空リストを返す。空リストは「その論点に関する記録が"
            "正本に無い」ことを意味する。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "検索クエリ。自然文でよい"},
                "doc_type": {
                    "type": "string",
                    "description": "decision | knowledge | meeting に絞る。省略時は全種類",
                },
                "status": {
                    "type": "string",
                    "description": "active | superseded | proposed に絞る。省略時は全状態",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_document",
        "description": (
            "doc_id を指定してドキュメントを 1 件取る。検索ではなく ID 参照。"
            "ある decision の supersedes / superseded_by を辿って旧版・新版を"
            "確認するときは、検索順位に依存させないためこちらを使う。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "例 DEC-003a, KNW-002, MTG-2026-03-15",
                },
            },
            "required": ["doc_id"],
        },
    },
]


class ToolsStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, knowledge, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bucket = knowledge.bucket
        kb = knowledge.knowledge_base

        # ------------------------------------------------------------------
        # ツール Lambda
        # ------------------------------------------------------------------
        # L1c までと同じ環境変数で動く。**中身が core.py なので当然そうなる**
        tools = lambda_.Function(
            self,
            "ToolsFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="tools.lambda_handler.handler",
            code=lambda_.Code.from_asset(_build_asset()),
            # KB の Retrieve は 1 秒前後。エージェントの待ち時間に直結するので短く保つ
            timeout=Duration.seconds(30),
            memory_size=512,
            environment={
                "KAI_SEARCH": "kb",
                "KAI_KNOWLEDGE_SOURCE": "s3",
                "KAI_KB_ID": kb.attr_knowledge_base_id,
                "KAI_KNOWLEDGE_BUCKET": bucket.bucket_name,
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        # §5.2：エージェントは read-only。s3:PutObject は与えない
        bucket.grant_read(tools)
        tools.add_to_role_policy(
            iam.PolicyStatement(
                sid="RetrieveFromKnowledgeBase",
                actions=["bedrock:Retrieve"],
                resources=[kb.attr_knowledge_base_arn],
            )
        )

        # ------------------------------------------------------------------
        # Gateway のサービスロール（Gateway → Lambda）
        # ------------------------------------------------------------------
        # アウトバウンドは IAM だけで通る。Lambda ターゲットを選んだ理由がこれ（§4.3）
        gateway_role = iam.Role(
            self,
            "GatewayRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                },
            ),
            description="Lets the AgentCore gateway invoke the tool lambda",
        )
        tools.grant_invoke(gateway_role)

        # ------------------------------------------------------------------
        # Gateway 本体
        # ------------------------------------------------------------------
        self.gateway = agentcore.CfnGateway(
            self,
            "Gateway",
            name="kai-tools",
            role_arn=gateway_role.role_arn,
            protocol_type="MCP",
            authorizer_type="AWS_IAM",
            description="Exposes the KAI project knowledge tools as MCP tools",
            # 既定ではツールが失敗しても理由が返らない。デモ中に「何も言わずに
            # 空が返る」のが一番困るので、理由を返させる。
            # **CLI の help は `NONE, ALL` と書いているが、CloudFormation が
            # 受け付けるのは `DEBUG` だけ**（スキーマの enum は ['DEBUG']）
            exception_level="DEBUG",
        )

        self.target = agentcore.CfnGatewayTarget(
            self,
            "GatewayTarget",
            gateway_identifier=self.gateway.attr_gateway_identifier,
            name=TARGET_NAME,
            # **スキーマ上は任意だが、Lambda ターゲットでは必須**
            # （`CredentialProviderConfigurations is required for Lambda targets`)。
            # Gateway → Lambda は §4.3 のとおりサービスロールの IAM だけで通るので、
            # 選ぶのは GATEWAY_IAM_ROLE。ここに OAuth や API キーが並ぶのは
            # 外部 API をターゲットにする場合の話
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE",
                )
            ],
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                        lambda_arn=tools.function_arn,
                        tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                            inline_payload=[
                                agentcore.CfnGatewayTarget.ToolDefinitionProperty(
                                    name=t["name"],
                                    description=t["description"],
                                    input_schema=t["inputSchema"],
                                )
                                for t in TOOL_SCHEMA
                            ],
                        ),
                    ),
                ),
            ),
        )

        # ------------------------------------------------------------------
        # Runtime の実行ロールへの付与
        # ------------------------------------------------------------------
        # L3b でエージェントが Gateway を叩く。KnowledgeStack と同じく context で受ける:
        #   cdk deploy KaiToolsStack -c runtimeRoleArn=arn:aws:iam::...:role/...
        runtime_role_arn = self.node.try_get_context("runtimeRoleArn")
        if runtime_role_arn:
            runtime_role = iam.Role.from_role_arn(
                self, "RuntimeRole", runtime_role_arn, mutable=True
            )
            runtime_role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="InvokeToolGateway",
                    actions=["bedrock-agentcore:InvokeGateway"],
                    resources=[self.gateway.attr_gateway_arn],
                )
            )

        # ------------------------------------------------------------------
        CfnOutput(
            self,
            "GatewayUrl",
            value=self.gateway.attr_gateway_url,
            description="MCP のエンドポイント。KAI_GATEWAY_URL に設定する",
        )
        CfnOutput(
            self,
            "ToolsFunctionName",
            value=tools.function_name,
            description="ツール Lambda。Gateway を通さず直接叩いて切り分けるのに使う",
        )
        CfnOutput(
            self,
            "ToolSchemaNames",
            value=json.dumps([t["name"] for t in TOOL_SCHEMA]),
            description="Gateway が公開するツール名（前置きは <ターゲット名>___）",
        )
