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


def runtime_role_arn() -> str | None:
    """Runtime の実行ロール ARN を context か環境変数から取る。

    **付け忘れが一番痛い設定なので、経路を 2 つ用意してある。**

        cdk deploy ... -c runtimeRoleArn=arn:aws:iam::...:role/...
        export KAI_RUNTIME_ROLE_ARN=arn:aws:iam::...:role/...   # .envrc.local

    付け忘れると `KaiKnowledgeStack` と `KaiToolsStack` の
    `if runtime_role_arn:` が素通りし、**Runtime ロールに与えた権限が差分として
    消える**（`bedrock:Retrieve` / `s3:GetObject` / `InvokeGateway` /
    `DescribeStacks`）。エージェントは 500 を返すようになるが、**CDK は成功する**ので
    デプロイ時には気づけない。

    **実際に踏んだ（2026-08-09）。** L4b で `KaiWritebackStack` が
    `KaiToolsStack` に依存するようにした結果、書き戻しだけをデプロイしたつもりが
    依存で 3 スタック更新され、台本 4 問が 0/8 になった。だから警告を出す。
    """
    arn = app.node.try_get_context("runtimeRoleArn") or os.environ.get("KAI_RUNTIME_ROLE_ARN")
    if not arn:
        print(
            "\n[警告] runtimeRoleArn が無い。Runtime ロールへの権限付与を飛ばす。\n"
            "        すでに Runtime をデプロイ済みなら、これは**権限を消す**変更になる\n"
            "        （エージェントが 500 を返すようになる。CDK 自体は成功する）。\n"
            "        L1d 以降は -c runtimeRoleArn=... か KAI_RUNTIME_ROLE_ARN を必ず渡すこと。\n",
            file=sys.stderr,
        )
    return arn


# スタックを組む前に一度呼んで、付け忘れならここで警告を出す
runtime_role_arn()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "ap-northeast-1"),
)

knowledge = KnowledgeStack(app, "KaiKnowledgeStack", env=env)

# ツールを Lambda に出して Gateway の MCP ターゲットにする（L3a）。
# KB とバケットは KaiKnowledgeStack の持ち物なので、参照だけ渡す
tools = ToolsStack(app, "KaiToolsStack", knowledge=knowledge, env=env)

# 起案（書き戻し）の Lambda（L4a）と、その Gateway ターゲット（L4b）。
# **ターゲットは既定で登録しない**ので、デプロイしてもエージェントの挙動は変わらない。
# 載せるときだけ明示する:  cdk deploy KaiWritebackStack -c writebackTarget=on
#
# **このスタックは KaiToolsStack に依存する**（Gateway と そのロールを参照するため）。
# つまり `deploy KaiWritebackStack` だけを指定しても Tools と Knowledge が一緒に
# 更新される ―― runtimeRoleArn の付け忘れがここで効くので注意
WritebackStack(app, "KaiWritebackStack", tools=tools, env=env)

# Slack は Runtime の ARN が要るので、渡されたときだけ作る。
# ARN は AgentCore CLI 側の CDK が持っているので context で受ける:
#   cdk deploy KaiSlackStack -c runtimeArn=arn:aws:bedrock-agentcore:...
runtime_arn = app.node.try_get_context("runtimeArn")
if runtime_arn:
    SlackStack(app, "KaiSlackStack", runtime_arn=runtime_arn, env=env)

app.synth()
