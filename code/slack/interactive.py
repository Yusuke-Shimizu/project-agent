"""ボタンの受け口（`writeback_pr_tool.md` §9 の L4c）。

`ingress.py` と**同じ責務**（3 秒以内に 200 を返して、あとは非同期に投げる）だが
**別 Lambda にしてある**。理由:

- `app_mention` の経路（当日のデモのクリティカルパス）に一切触らない。
  起案の不具合で質問応答が落ちてはいけない
- ログが分かれて切り分けが楽
- そして **worker はどうせ別になる**（PR を立てる仕事と、エージェントに聞く仕事は違う）。
  入口を共有しても振り分けが増えるだけで何も共有できない

Events API との違いは payload の形だけ:

- Content-Type が **`application/x-www-form-urlencoded`**
- 本体は `payload=<URL エンコードされた JSON>`
- 署名検証は**生ボディに対して**なので `verify.py` がそのまま使える
"""

from __future__ import annotations

import json
import os
import urllib.parse

import boto3

from proposal import ACTION_DISMISS, ACTION_PROPOSE
from verify import is_from_slack, raw_body

lambda_client = boto3.client("lambda")


def _worker_name() -> str:
    """import 時ではなく呼ばれたときに読む。**テストが環境変数を要らなくなる。**"""
    return os.environ["PROPOSE_WORKER_FUNCTION_NAME"]


def _ok(body: str = "") -> dict:
    return {"statusCode": 200, "body": body}


def parse_payload(body: str) -> dict:
    """`payload=<JSON>` を辞書にする。壊れていれば空辞書。"""
    fields = urllib.parse.parse_qs(body)
    raw = (fields.get("payload") or [""])[0]
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def handler(event, context):
    body = raw_body(event)

    # ① 署名検証。公開エンドポイントなのでここが唯一の門
    if not is_from_slack(event.get("headers"), body):
        print("署名検証に失敗した")
        return {"statusCode": 401, "body": "invalid signature"}

    payload = parse_payload(body)
    if payload.get("type") != "block_actions":
        # ショートカットやモーダルは使っていない。**黙って 200**（Slack に赤を出さない）
        return _ok()

    actions = payload.get("actions") or []
    action_id = (actions[0] or {}).get("action_id") if actions else None
    if action_id not in (ACTION_PROPOSE, ACTION_DISMISS):
        return _ok()

    # ② worker に投げて即 200。3 秒を超えると Slack がボタンにエラーを出す
    lambda_client.invoke(
        FunctionName=_worker_name(),
        InvocationType="Event",
        Payload=json.dumps(
            {
                "action_id": action_id,
                "value": (actions[0] or {}).get("value") or "",
                "channel": (payload.get("channel") or {}).get("id"),
                # ボタンが乗っているメッセージ。ここを差し替える
                "message_ts": (payload.get("message") or {}).get("ts"),
                "thread_ts": (payload.get("message") or {}).get("thread_ts"),
                "user": (payload.get("user") or {}).get("id"),
                # 30 分・5 回まで使える返信用 URL（chat.update が使えないときの保険）
                "response_url": payload.get("response_url"),
            },
            ensure_ascii=False,
        ).encode("utf-8"),
    )
    return _ok()
