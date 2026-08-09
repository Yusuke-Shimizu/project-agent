#!/usr/bin/env python3
"""CDK のエントリポイント。

    uv run --extra infra cdk deploy KaiKnowledgeStack

このアカウントには他の用途のスタックも同居しているので、名前は Kai で始めて衝突を避ける。
"""

import os
import pathlib
import sys

import aws_cdk as cdk

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from stacks.knowledge_stack import KnowledgeStack  # noqa: E402
from stacks.slack_stack import SlackStack  # noqa: E402
from stacks.tools_stack import ToolsStack  # noqa: E402
from stacks.writeback_stack import WritebackStack  # noqa: E402

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "ap-northeast-1"),
)

knowledge = KnowledgeStack(app, "KaiKnowledgeStack", env=env)

# ツールを Lambda に出して Gateway の MCP ターゲットにする（L3a）。
# KB とバケットは KaiKnowledgeStack の持ち物なので、参照だけ渡す
ToolsStack(app, "KaiToolsStack", knowledge=knowledge, env=env)

# Slack は Runtime の ARN が要るので、渡されたときだけ作る。
# ARN は AgentCore CLI 側の CDK が持っているので context で受ける:
#   cdk deploy KaiSlackStack -c runtimeArn=arn:aws:bedrock-agentcore:...
# 起案（書き戻し）の Lambda（L4a）。**Gateway には登録しない**ので、
# デプロイしてもエージェントの挙動は変わらない
WritebackStack(app, "KaiWritebackStack", env=env)

runtime_arn = app.node.try_get_context("runtimeArn")
if runtime_arn:
    SlackStack(app, "KaiSlackStack", runtime_arn=runtime_arn, env=env)

app.synth()
