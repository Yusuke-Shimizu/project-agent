"""AgentCore Runtime の入口（L1d）。

architecture_v1.md §4.2 のとおり、`BedrockAgentCoreApp` を使い**自前 FastAPI は書かない**。
`/invocations`・`/ping` の実装と `HealthyBusy` によるセッション keep-alive を SDK が持つ。

このファイルは `build_agent()` を呼ぶだけの薄い皮で、**エージェントの中身（モデル・
出力契約・ツール）には一切触らない**。L1c までに検証した挙動をそのまま持ち上げる。

環境変数（Runtime に渡す）:
    KAI_SEARCH=kb / KAI_KB_ID / KAI_KNOWLEDGE_SOURCE=s3 / KAI_KNOWLEDGE_BUCKET
"""

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from runtime.agent import build_agent

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload, context):
    """1 リクエスト = 1 問。

    問ごとに Agent を作り直すのは、ローカルの `run_demo_script.py` と揃えるため
    （前の問の文脈を持ち越さない）。スレッドの文脈は L2 以降で AgentCore Memory の
    short-term が担う。正本は Memory に入れない（§4.5）。
    """
    prompt = payload.get("prompt", "")
    if not prompt:
        return {"error": "prompt が空"}

    # stream=False：標準出力に流さず、戻り値だけ受け取る
    result = build_agent(stream=False)(prompt)
    return {"result": str(result.message)}


if __name__ == "__main__":
    app.run()
