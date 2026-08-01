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

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "ap-northeast-1"),
)

KnowledgeStack(app, "KaiKnowledgeStack", env=env)

app.synth()
