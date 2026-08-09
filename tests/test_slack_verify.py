"""署名検証とボタンの payload 解析のテスト（L4c）。

**公開エンドポイントの唯一の門**なので、通る側だけでなく**弾く側**を固定する。
AWS には触らない（signing secret は直接渡す）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse

import pytest

import verify

#: テスト用のダミー。**16 進の羅列にすると gitleaks の generic-api-key に当たる**ので、
#: 見て偽物と分かる文字列にしてある（HMAC はどんな文字列でも計算できる）
SECRET = "dummy-signing-secret-for-tests"


def sign(body: str, timestamp: str | None = None, secret: str = SECRET) -> dict:
    ts = timestamp or str(int(time.time()))
    digest = hmac.new(
        secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256
    ).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": f"v0={digest}"}


# --- 署名検証 ---------------------------------------------------------------


def test_正しい署名は通る():
    body = '{"type":"event_callback"}'
    assert verify.is_from_slack(sign(body), body, SECRET) is True


def test_ヘッダの大文字小文字を問わない():
    body = "a=1"
    headers = {k.lower(): v for k, v in sign(body).items()}
    assert verify.is_from_slack(headers, body, SECRET) is True


def test_form_urlencoded_でも同じ検証で通る():
    # **Events は JSON、ボタンは form-urlencoded。生ボディの HMAC なので同じ**
    body = "payload=%7B%22type%22%3A%22block_actions%22%7D"
    assert verify.is_from_slack(sign(body), body, SECRET) is True


def test_署名が違えば弾く():
    body = "a=1"
    assert verify.is_from_slack(sign(body, secret="another-dummy-secret"), body, SECRET) is False


def test_ボディが改変されていれば弾く():
    headers = sign("a=1")
    assert verify.is_from_slack(headers, "a=2", SECRET) is False


def test_古いリクエストは弾く():
    body = "a=1"
    old = str(int(time.time()) - verify.MAX_SKEW_SECONDS - 10)
    assert verify.is_from_slack(sign(body, old), body, SECRET) is False


def test_未来に振れたリクエストも弾く():
    body = "a=1"
    future = str(int(time.time()) + verify.MAX_SKEW_SECONDS + 10)
    assert verify.is_from_slack(sign(body, future), body, SECRET) is False


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Slack-Signature": "v0=deadbeef"},
        {"X-Slack-Request-Timestamp": "123"},
        {"X-Slack-Request-Timestamp": "not-a-number", "X-Slack-Signature": "v0=x"},
    ],
)
def test_ヘッダが欠けていれば弾く(headers):
    assert verify.is_from_slack(headers, "a=1", SECRET) is False


# --- 生ボディの取り出し -----------------------------------------------------


def test_base64のボディを復元する():
    import base64

    raw = "payload=%7B%7D"
    event = {"body": base64.b64encode(raw.encode()).decode(), "isBase64Encoded": True}
    assert verify.raw_body(event) == raw


def test_素のボディはそのまま():
    assert verify.raw_body({"body": "a=1"}) == "a=1"


def test_ボディが無ければ空文字():
    assert verify.raw_body({}) == ""


# --- ボタンの payload -------------------------------------------------------


def test_ボタンのpayloadを辞書にする():
    import interactive

    inner = {"type": "block_actions", "actions": [{"action_id": "kai_propose"}]}
    body = urllib.parse.urlencode({"payload": json.dumps(inner)})
    assert interactive.parse_payload(body) == inner


@pytest.mark.parametrize("body", ["", "payload=", "payload=notjson", "other=1"])
def test_壊れたpayloadは空辞書(body):
    import interactive

    assert interactive.parse_payload(body) == {}
