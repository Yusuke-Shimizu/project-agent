"""Slack Events API の受け口（architecture_v1.md §4.1）。

**このハンドラは3秒以内に必ず 200 を返す。** Slack は3秒で応答が無いとリトライし、
同じ質問に3回答えることになる（§10-8）。エージェントの実行は 20 秒かかるので、
ここでは受けるだけにして worker に非同期で投げる ―― この構成自体が3秒ルールへの答え。

やることは3つだけ:

1. 署名検証（HMAC-SHA256 とタイムスタンプ）
2. リトライヘッダがあれば **何もせず 200**（200 を返し損ねたときの保険。下記参照）
3. worker を Event 型で非同期 invoke して即 200

Lambda の標準ライブラリと boto3 だけで書く（依存を足さない）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

import boto3

lambda_client = boto3.client("lambda")
secrets = boto3.client("secretsmanager")

WORKER_FUNCTION_NAME = os.environ["WORKER_FUNCTION_NAME"]
SECRET_ID = os.environ["SLACK_SECRET_ID"]

#: 署名の許容ずれ。Slack の推奨は5分
MAX_SKEW_SECONDS = 60 * 5

#: Secrets Manager の値はコンテナが生きている間だけ使い回す
_signing_secret: str | None = None


def _get_signing_secret() -> str:
    global _signing_secret
    if _signing_secret is None:
        value = secrets.get_secret_value(SecretId=SECRET_ID)["SecretString"]
        _signing_secret = json.loads(value)["signing_secret"]
    return _signing_secret


def _verify(headers: dict, raw_body: str) -> bool:
    """Slack からのリクエストであることを確かめる。

    署名が合わないものは**黙って捨てる**。エンドポイントは公開されているので、
    ここが唯一の門になる。
    """
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    if not timestamp or not signature:
        return False

    # 古いリクエストのリプレイを防ぐ
    try:
        if abs(time.time() - int(timestamp)) > MAX_SKEW_SECONDS:
            return False
    except ValueError:
        return False

    expected = (
        "v0="
        + hmac.new(
            _get_signing_secret().encode("utf-8"),
            f"v0:{timestamp}:{raw_body}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    )
    # タイミング攻撃を避けるため == で比べない
    return hmac.compare_digest(expected, signature)


def _ok(body: str = "") -> dict:
    return {"statusCode": 200, "body": body}


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    # ① 署名検証。エンドポイントは公開されているので、ここが唯一の門になる。
    #    何もしない経路（リトライ）であっても、門は先に置く
    if not _verify(headers, raw_body):
        print("署名検証に失敗した")
        return {"statusCode": 401, "body": "invalid signature"}

    # ② リトライは何もせず 200。
    #
    #    **3秒ルール（§10-8）のためではない。** このハンドラはエージェントを待たずに
    #    数ミリ秒で 200 を返すので、「エージェントが 20 秒かかるから Slack がリトライする」
    #    ということは起きない。
    #
    #    ここが効くのは **200 を返し損ねたとき**。とくに当日の1問目は必ず
    #    コールドスタートで、Secrets Manager の取得も入るため 3 秒を超えうる。
    #    「worker の invoke には成功したが 200 を返す前にタイムアウトした」場合、
    #    Slack がリトライして **worker が2回起動し、回答が2回投稿される**。それを防ぐ。
    if "x-slack-retry-num" in headers:
        print(f"retry を無視: reason={headers.get('x-slack-retry-reason')}")
        return _ok()

    payload = json.loads(raw_body) if raw_body else {}

    # ③ Event Subscriptions の URL 登録時に一度だけ来るチャレンジ
    if payload.get("type") == "url_verification":
        return _ok(payload.get("challenge", ""))

    inner = payload.get("event") or {}
    # bot 自身の発言に反応すると無限ループになる
    if inner.get("bot_id") or inner.get("subtype") == "bot_message":
        return _ok()
    if inner.get("type") not in ("app_mention", "message"):
        return _ok()

    # ④ worker に投げて即 200。Event 型なので応答を待たない
    lambda_client.invoke(
        FunctionName=WORKER_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(inner).encode("utf-8"),
    )
    return _ok()
