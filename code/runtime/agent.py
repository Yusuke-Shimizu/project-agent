"""エージェント本体。

L0 ではローカルの CLI から `build_agent()` を呼ぶ。L1 で AgentCore Runtime に上げるときは
このファイルに `BedrockAgentCoreApp` の薄いラッパを足すだけで、`build_agent()` の中身は
変えない（architecture_v1.md §4.2）。

    app = BedrockAgentCoreApp()

    @app.entrypoint
    def invoke(payload, context):
        return {"result": str(build_agent()(payload["prompt"]).message)}
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from runtime.prompts import SYSTEM_PROMPT
from tools.local_tools import TOOLS

#: 既定のモデル。ap-northeast-1 から Sonnet 5 を使えるのは global. プロファイルだけで、
#: 推論は国外に出うる（日本国内に閉じたいなら jp.anthropic.claude-sonnet-4-6）。
#: デモデータは完全架空なので実データの所在制約は関係しない。
DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-5"
DEFAULT_REGION = "ap-northeast-1"


def build_agent(stream: bool = True) -> Agent:
    """出力契約とツールを載せた Agent を組み立てる。

    Args:
        stream: True なら生成中の出力を標準出力に流す（CLI 用）。
            False なら黙らせて戻り値だけ返す（台本の一括実行用）。

    環境変数:
        KAI_MODEL_ID   モデル/推論プロファイル ID（既定 global.anthropic.claude-sonnet-5）
        AWS_REGION     リージョン（既定 ap-northeast-1）
    """
    kwargs = {} if stream else {"callback_handler": None}
    return Agent(
        model=BedrockModel(
            model_id=os.environ.get("KAI_MODEL_ID", DEFAULT_MODEL_ID),
            region_name=os.environ.get("AWS_REGION", DEFAULT_REGION),
        ),
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        **kwargs,
    )
