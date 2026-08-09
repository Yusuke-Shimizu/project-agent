"""Slack に返す側（architecture_v1.md §4.1）。

ingress から Event 型で非同期に呼ばれる。ここは時間をかけてよい（タイムアウト 300 秒）。

1. スレッドに「考え中…」を先に投げる（**エージェントは 20 秒かかる**ので、これが無いと
   沈黙が続く。§4.6）
2. `InvokeAgentRuntime` でエージェントを呼ぶ
3. 暫定投稿を `chat.update` で回答に差し替える

Slack SDK は入れず `urllib` で叩く。Lambda に依存を足さないため。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

import boto3

from proposal import blocks as proposal_blocks
from proposal import parse_marker

agentcore = boto3.client("bedrock-agentcore")
secrets = boto3.client("secretsmanager")

RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
SECRET_ID = os.environ["SLACK_SECRET_ID"]

SLACK_API = "https://slack.com/api"
THINKING = "考え中…"

#: やりとりをログに残すか。
#:
#: **リハーサルのたびに Runtime の otel-rt-logs を掘るのが手間だった**ので入れた。
#: worker のログだけで「何を聞かれて何を返したか」が読めるようにする。
#: 正本もデモの質問も**完全架空**なので、ここに本文が出ても差し支えない。
#: 実データを扱う構成に持っていくときは、まずここを落とすこと。
LOG_CONVERSATION = os.environ.get("KAI_LOG_CONVERSATION", "1") == "1"

_bot_token: str | None = None


def _get_bot_token() -> str:
    global _bot_token
    if _bot_token is None:
        value = secrets.get_secret_value(SecretId=SECRET_ID)["SecretString"]
        _bot_token = json.loads(value)["bot_token"]
    return _bot_token


def _slack(method: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{SLACK_API}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {_get_bot_token()}",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not body.get("ok"):
        # トークン不足やスコープ不足はここに出る。握りつぶすと原因が分からなくなる
        print(f"Slack API {method} が失敗: {body.get('error')}")
    return body


def _session_id(thread_ts: str) -> str:
    """`runtimeSessionId` を作る。

    **33 文字以上でないと `ValidationException`**（§10-1）。`thread_ts` は 17 文字ほどしか
    無いのでそのままでは通らない。uuid5 で 36 文字にしつつ、**同じスレッドなら同じ ID**
    になるようにする（スレッド＝会話の単位）。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kai-slack-{thread_ts}"))


def _ask_agent(prompt: str, thread_ts: str) -> str:
    response = agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=_session_id(thread_ts),
        payload=json.dumps({"prompt": prompt}).encode("utf-8"),
    )
    body = json.loads(response["response"].read().decode("utf-8"))
    if "error" in body:
        raise RuntimeError(body["error"])
    return str(body.get("result", ""))


def _clean(text: str) -> str:
    """メンション（`<@U123>`）を落とす。エージェントに渡すのは本文だけでよい。"""
    out = []
    depth = 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">" and depth:
            depth -= 1
        elif not depth:
            out.append(ch)
    return "".join(out).strip()


def handler(event, context):
    channel = event.get("channel")
    # スレッド内なら thread_ts、そうでなければ発言そのものを親にする
    thread_ts = event.get("thread_ts") or event.get("ts")
    prompt = _clean(event.get("text") or "")

    if not channel or not thread_ts or not prompt:
        print(f"必要な項目が無いので何もしない: channel={channel} ts={thread_ts}")
        return

    if LOG_CONVERSATION:
        print(f"--- 質問 channel={channel} thread_ts={thread_ts}\n{prompt}")

    placeholder = _slack(
        "chat.postMessage",
        {"channel": channel, "thread_ts": thread_ts, "text": THINKING},
    )
    placeholder_ts = placeholder.get("ts")

    started = time.monotonic()
    try:
        answer = _ask_agent(prompt, thread_ts)
    except Exception as exc:  # 落ちても沈黙させない。何が起きたかはスレッドに残す
        print(f"エージェントの呼び出しに失敗: {exc}")
        answer = f"エラーで回答できませんでした（{type(exc).__name__}）。ログを確認してください。"

    elapsed = time.monotonic() - started
    if LOG_CONVERSATION:
        # 所要時間を一緒に出す。**Slack で体感する待ち時間はここがほぼ全部**なので、
        # 「遅い」と感じたときに Runtime を掘る前に切り分けられる
        print(f"--- 回答 {elapsed:.1f}秒\n{answer}")

    # 回答の最終行に起案の提案が付いていたら、剥がしてボタンにする（L4d）。
    # **人が見る文面は 3 ブロックのまま。**マーカーが壊れていたら提案が出ないだけ
    text, proposal = parse_marker(answer)
    payload = {"channel": channel, "text": text}
    if proposal:
        payload["blocks"] = proposal_blocks(text, proposal)
        print(f"--- 起案の提案 kind={proposal['kind']} doc_id={proposal.get('doc_id')}")

    if placeholder_ts:
        _slack("chat.update", {**payload, "ts": placeholder_ts})
    else:
        # 暫定投稿に失敗していた場合は普通に投稿する
        _slack("chat.postMessage", {**payload, "thread_ts": thread_ts})
