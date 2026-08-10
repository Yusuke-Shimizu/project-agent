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
import urllib.parse
import urllib.request
import uuid

import boto3

from proposal import ACTION_DISMISS, directive

SLACK_API = "https://slack.com/api"

# **環境変数も import 時には読まない。**モジュール直下で読むとテストが
# 環境変数を要求し、CI で収集ごと落ちる（boto3 のクライアントと同じ理由で踏んだ）

WORKING = "起案を作っています…"
DISMISSED = "_起案しないことにしました。_"

_bot_token: str | None = None


def _get_bot_token() -> str:
    global _bot_token
    if _bot_token is None:
        value = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["SLACK_SECRET_ID"]
        )["SecretString"]
        _bot_token = json.loads(value)["bot_token"]
    return _bot_token


def encode(payload: dict, form: bool) -> tuple[bytes, str]:
    """Slack に送る本体を作る。

    **`chat.getPermalink` は JSON ボディを受け付けない**（`invalid_arguments` が返る）。
    `chat.postMessage` や `chat.update` は JSON で通るので気づきにくく、**実測で踏んだ**
    ―― 起案が `source_url` 無しで拒否され続けた。読み系のメソッドは form で送る。
    """
    if form:
        return (
            urllib.parse.urlencode(payload).encode("utf-8"),
            "application/x-www-form-urlencoded; charset=utf-8",
        )
    return json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8"


def _slack(method: str, payload: dict, form: bool = False) -> dict:
    data, content_type = encode(payload, form)
    request = urllib.request.Request(
        f"{SLACK_API}/{method}",
        data=data,
        headers={
            "Content-Type": content_type,
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

    `source_url` が空だと起案ツールが拒否するので、ここが**起案全体の単一障害点**になる。
    取れなかったときはワークスペースの URL から組み立てて凌ぐ（形式は固定）。
    """
    got = _slack("chat.getPermalink", {"channel": channel, "message_ts": ts}, form=True)
    link = str(got.get("permalink") or "")
    if link:
        return link

    base = str(_slack("auth.test", {}, form=True).get("url") or "").rstrip("/")
    if not base:
        return ""
    # 形式は `<team>/archives/<channel>/p<ts の . を抜いたもの>`
    return f"{base}/archives/{channel}/p{ts.replace('.', '')}"


def _session_id(thread_ts: str) -> str:
    """`runtimeSessionId` は 33 文字以上でないと通らない（§10-1）。

    **`worker.py` と別の名前空間にする。** 同じスレッドでも「質問に答える」と
    「起案を作る」は別の文脈なので、短期記憶を混ぜない
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kai-propose-{thread_ts}"))


def _ask_agent(prompt: str, thread_ts: str) -> str:
    response = boto3.client("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=os.environ["AGENT_RUNTIME_ARN"],
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
