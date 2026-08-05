"""AgentCore Runtime の入口（L1d）。

architecture_v1.md §4.2 のとおり、`BedrockAgentCoreApp` を使い**自前 FastAPI は書かない**。
`/invocations`・`/ping` の実装と `HealthyBusy` によるセッション keep-alive を SDK が持つ。

このファイルは `build_agent()` を呼ぶだけの薄い皮で、**エージェントの中身（モデル・
出力契約・ツール）には一切触らない**。L1c までに検証した挙動をそのまま持ち上げる。

環境変数（Runtime に渡す）:
    KAI_SEARCH=kb / KAI_KB_ID / KAI_KNOWLEDGE_SOURCE=s3 / KAI_KNOWLEDGE_BUCKET
"""

import os
import time

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from runtime.agent import build_agent

app = BedrockAgentCoreApp()

# 正本を起動時に読み込んでおく（プロセスごとに1回）。
#
# これをやらないと、最初のリクエストの**最中に** S3 の読み込みが走る。core.py の
# キャッシュはロックで守ってあるので同一プロセス内では1回に収まるが、コンテナは
# 複数プロセスで動く（L1d のトレースで instance.id が2つ確認できた）ので、
# プロセス数だけリクエスト中に読み込みが入る。
#
# トレースを見せるデモなので、**リクエストのトレースに S3 アクセスを並べない**方を取る。
# 起動が 0.5 秒ほど伸びるが、初期化のタイムアウト（30秒）には十分収まる。
#
# **L3（Gateway）ではこれ自体が要らなくなった。** ツールが Lambda 側に移り、
# Runtime は正本にも KB にも触らない ―― 触るのは Bedrock と Gateway だけ。
# L3b のトレースに S3.GetObject が 168 本残っていて気づいた。
if os.environ.get("KAI_TOOLS", "local").lower() != "gateway":
    from tools import core

    core._load_all()


#: `ConverseStream` の一時障害に対する呼び出し単位のリトライ。
#:
#: botocore のリトライ（agent.py で 8 回まで上げてある）を使い切っても
#: `InternalServerException` で落ちることがある。**同じ問を少し置いて呼び直すと通る**ので、
#: エージェントの実行ごと畳んでやり直す。ツールは read-only なので再実行しても副作用は無い。
#: リハーサルの実測で 12 回中 6 回落ちたため入れた。
MAX_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 2.0


def _ask_with_retry(prompt: str):
    last: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return build_agent(stream=False)(prompt)
        except Exception as exc:  # noqa: BLE001 ― 型で絞るとモデル側の例外を取りこぼす
            last = exc
            print(f"エージェントの実行に失敗（{attempt}/{MAX_ATTEMPTS}）: {type(exc).__name__}: {exc}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_WAIT_SECONDS * attempt)
    raise last


def _answer(message) -> str:
    """`result.message` から**本文だけ**を取り出す。

    `message` は `{"role": ..., "content": [ブロック, ...]}` という形で、ブロックには
    本文（`text`）以外に `reasoningContent` も混ざる。`str()` してそのまま返すと
    Slack に **Python の dict がそのまま貼られる**（L2 の実測で発覚）。
    出力契約（3ブロック）を守るのは `text` の中身なので、そこだけを繋ぐ。
    """
    blocks = message.get("content") or []
    return "\n".join(b["text"] for b in blocks if "text" in b).strip()


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
    result = _ask_with_retry(prompt)
    return {"result": _answer(result.message)}


if __name__ == "__main__":
    app.run()
