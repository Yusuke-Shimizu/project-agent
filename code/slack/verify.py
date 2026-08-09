"""Slack からのリクエストであることを確かめる（`ingress` と `interactive` の共通部分）。

**Events API とボタン（Interactivity）で検証の仕方は同じ。** どちらも
`v0:<timestamp>:<生ボディ>` の HMAC-SHA256 で、**生ボディを触らない**限り
Content-Type には依存しない（Events は JSON、ボタンは form-urlencoded）。

だから2か所に複製せずここに置く ―― **秘匿処理を二重管理しない**。
L4c で受け口を別 Lambda にしたときに切り出した。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

import boto3

secrets = boto3.client("secretsmanager")

#: 署名の許容ずれ。Slack の推奨は5分
MAX_SKEW_SECONDS = 60 * 5

_signing_secret: str | None = None


def signing_secret(secret_id: str | None = None) -> str:
    """Secrets Manager の値はコンテナが生きている間だけ使い回す。"""
    global _signing_secret
    if _signing_secret is None:
        value = secrets.get_secret_value(
            SecretId=secret_id or os.environ["SLACK_SECRET_ID"]
        )["SecretString"]
        _signing_secret = json.loads(value)["signing_secret"]
    return _signing_secret


def raw_body(event: dict) -> str:
    """API Gateway のイベントから**生のボディ**を取り出す。

    署名は生ボディに対して計算されているので、**パースしてから組み直してはいけない**。
    """
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return body


def is_from_slack(headers: dict, body: str, secret: str | None = None) -> bool:
    """署名とタイムスタンプを検証する。**合わないものは黙って捨てる。**

    エンドポイントは公開されているので、ここが唯一の門になる。
    """
    lower = {k.lower(): v for k, v in (headers or {}).items()}
    timestamp = lower.get("x-slack-request-timestamp", "")
    signature = lower.get("x-slack-signature", "")
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
            (secret or signing_secret()).encode("utf-8"),
            f"v0:{timestamp}:{body}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    )
    # タイミング攻撃を避けるため == で比べない
    return hmac.compare_digest(expected, signature)


def reset_cache() -> None:
    """テスト用。キャッシュした signing secret を捨てる。"""
    global _signing_secret
    _signing_secret = None
