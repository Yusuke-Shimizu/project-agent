"""ボタンが押されたあとを引き受ける（`writeback_pr_tool.md` §9 の L4c）。

`worker.py` と対になるが、仕事が違う:

- `worker.py`        … 質問に答える（`InvokeAgentRuntime` → `chat.update`）
- `propose_worker.py`… **承認された起案を作る**（`InvokeAgentRuntime` → PR → `chat.update`）

**下書きはここで初めて作られる。** 提案の時点では作らないので、押されなかった提案の
コストはゼロ（§2 の「コストが非対称」の実装）。エージェントは正本を引き直してから
起案ツールを呼ぶので、スレッドを読み直すスコープ（`channels:history`）が要らない。

**この Lambda も GitHub には触らない。** 起案 Lambda を呼ぶのはエージェント側
（Gateway 経由）で、ここは Slack とエージェントの間をつなぐだけ。
"""

from __future__ import annotations

import json
import os
import urllib.request
import uuid

import boto3

from proposal import ACTION_DISMISS, directive

agentcore = boto3.client("bedrock-agentcore")
secrets = boto3.client("secretsmanager")

RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
SECRET_ID = os.environ["SLACK_SECRET_ID"]
SLACK_API = "https://slack.com/api"

WORKING = "起案を作っています…"
DISMISSED = "_起案しないことにしました。_"

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
        print(f"Slack API {method} が失敗: {body.get('error')}")
    return body


def _permalink(channel: str, ts: str) -> str:
    """起案の `source_url` になる。**どのやりとりから出た起案か辿れるようにする。**

    取れなければ空で返す（起案ツール側で拒否されるので、静かに間違えることはない）。
    """
    got = _slack("chat.getPermalink", {"channel": channel, "message_ts": ts})
    return str(got.get("permalink") or "")


def _session_id(thread_ts: str) -> str:
    """`runtimeSessionId` は 33 文字以上でないと通らない（§10-1）。

    **`worker.py` と別の名前空間にする。** 同じスレッドでも「質問に答える」と
    「起案を作る」は別の文脈なので、短期記憶を混ぜない
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kai-propose-{thread_ts}"))


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


def _replace(channel: str, ts: str, text: str) -> None:
    """ボタンを消して結果に差し替える。**二度押しを構造で防ぐ**（ボタンが無くなる）。"""
    _slack("chat.update", {"channel": channel, "ts": ts, "text": text, "blocks": []})


def handler(event, context):
    channel = event.get("channel")
    message_ts = event.get("message_ts")
    thread_ts = event.get("thread_ts") or message_ts
    user = event.get("user") or "unknown"

    if not channel or not message_ts:
        print(f"必要な項目が無いので何もしない: channel={channel} ts={message_ts}")
        return

    if event.get("action_id") == ACTION_DISMISS:
        # 「不要」も記録する。押されなかった提案と区別できないと的中率が測れない（§10）
        print(f"起案を却下: channel={channel} thread_ts={thread_ts} user={user}")
        _replace(channel, message_ts, DISMISSED)
        return

    try:
        raw = json.loads(event.get("value") or "{}")
        proposal = {
            "kind": raw["k"],
            "doc_id": raw.get("d", ""),
            "based_on": raw.get("b") or [],
            "summary": raw.get("s", ""),
        }
    except (json.JSONDecodeError, KeyError) as exc:
        print(f"ボタンの value が読めない: {exc}")
        _replace(channel, message_ts, "起案できませんでした（ボタンの情報が壊れています）。")
        return

    _replace(channel, message_ts, WORKING)

    source_url = _permalink(channel, thread_ts)
    prompt = directive(proposal, source_url, user)

    try:
        answer = _ask_agent(prompt, thread_ts)
    except Exception as exc:  # noqa: BLE001 ― 落ちても沈黙させない
        print(f"起案の呼び出しに失敗: {exc}")
        answer = f"起案に失敗しました（{type(exc).__name__}）。ログを確認してください。"

    print(f"--- 起案の結果 channel={channel} thread_ts={thread_ts}\n{answer}")
    _replace(channel, message_ts, answer)
