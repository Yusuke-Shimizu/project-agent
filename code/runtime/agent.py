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

from botocore.config import Config as BotoConfig
from strands import Agent
from strands.models import BedrockModel

from runtime.prompts import SYSTEM_PROMPT

#: 既定のモデル。ap-northeast-1 から Sonnet 5 を使えるのは global. プロファイルだけで、
#: 推論は国外に出うる（日本国内に閉じたいなら jp.anthropic.claude-sonnet-4-6）。
#: デモデータは完全架空なので実データの所在制約は関係しない。
DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-5"
DEFAULT_REGION = "ap-northeast-1"

#: Gateway に繋いだクライアント。**プロセスに 1 つだけ持つ。**
#: 問ごとに張り直すと、接続の確立がリクエストのトレースに毎回並ぶ（L1d で S3 の
#: 読み込みに同じことをして直した。§10-20 と同じ判断）
_mcp_client = None


def load_tools() -> list:
    """エージェントに渡すツールを組み立てる。

    **ツールの実体は L0 から `core.py` 1 つで、変わるのは呼ばれ方だけ**（§4.3）:

        KAI_TOOLS=local    プロセス内の `@tool`（L0〜L2）
        KAI_TOOLS=gateway  Gateway 経由の MCP（L3）

    どちらも同じ 2 つのツールが同じ引数で生える。切り替えても出力契約は動かない
    ―― これが「Gateway で詰まったら直呼びに退避できる」の実体。
    """
    global _mcp_client

    if os.environ.get("KAI_TOOLS", "local").lower() != "gateway":
        from tools.local_tools import TOOLS

        return TOOLS

    from tools import gateway

    if _mcp_client is None:
        _mcp_client = gateway.build_mcp_client()
        # start() したまま持つ。with で囲むと Agent が使う前に閉じてしまう
        _mcp_client.start()
    return _mcp_client.list_tools_sync()


def build_agent(stream: bool = True) -> Agent:
    """出力契約とツールを載せた Agent を組み立てる。

    Args:
        stream: True なら生成中の出力を標準出力に流す（CLI 用）。
            False なら黙らせて戻り値だけ返す（台本の一括実行用）。

    環境変数:
        KAI_MODEL_ID   モデル/推論プロファイル ID（既定 global.anthropic.claude-sonnet-5）
        KAI_TOOLS      local | gateway（既定 local）。ツールの呼ばれ方
        AWS_REGION     リージョン（既定 ap-northeast-1）
    """
    kwargs = {} if stream else {"callback_handler": None}
    return Agent(
        model=BedrockModel(
            model_id=os.environ.get("KAI_MODEL_ID", DEFAULT_MODEL_ID),
            region_name=os.environ.get("AWS_REGION", DEFAULT_REGION),
            # `ConverseStream` が `InternalServerException` を返すことがある。
            # botocore の既定（標準モード・3回）では足りずに落ちたので上げる。
            # **これだけでは足りない**（4回リトライしても落ちた実測がある）ので、
            # app.py 側で呼び出しごとのリトライも重ねている
            boto_client_config=BotoConfig(
                retries={"max_attempts": 8, "mode": "adaptive"},
                read_timeout=120,
            ),
        ),
        system_prompt=SYSTEM_PROMPT,
        tools=load_tools(),
        **kwargs,
    )
