"""Runtime のリトライ（`app._ask_with_retry`）の検証。

リハーサルで Bedrock の `ConverseStream` が `InternalServerException` を返し、
Runtime 経由 12 回中 6 回が 500 になった。対策のリトライを入れたあと 12/12 に
戻ったが、**ログを見るとリトライは一度も発火していなかった** ―― Bedrock が
復旧しただけで、リトライが効くことの証明にはなっていない。

**当日に初めて動く経路にしない。**ここで挙動を固定しておく。
"""

from __future__ import annotations

import pytest

from runtime import app


class _Boom(RuntimeError):
    """`InternalServerException` の代役。"""


def _agent_failing(times: int):
    """呼ばれた回数を数え、最初の `times` 回だけ失敗する Agent もどきを返す。"""
    calls = {"n": 0}

    def build(stream: bool = True):
        def invoke(prompt: str):
            calls["n"] += 1
            if calls["n"] <= times:
                raise _Boom("The system encountered an unexpected error")
            return type("Result", (), {"message": {"content": [{"text": "ok"}]}})()

        return invoke

    return build, calls


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # 待ち時間はテストの対象ではない
    monkeypatch.setattr(app.time, "sleep", lambda _: None)


def test_一時的な失敗なら呼び直して成功する(monkeypatch):
    build, calls = _agent_failing(times=2)
    monkeypatch.setattr(app, "build_agent", build)

    result = app._ask_with_retry("これで進めます")

    assert app._answer(result.message) == "ok"
    assert calls["n"] == 3  # 2 回失敗して 3 回目で通る


def test_初回で通れば呼び直さない(monkeypatch):
    build, calls = _agent_failing(times=0)
    monkeypatch.setattr(app, "build_agent", build)

    app._ask_with_retry("これで進めます")

    assert calls["n"] == 1


def test_上限まで失敗したら例外を投げ直す(monkeypatch):
    build, calls = _agent_failing(times=app.MAX_ATTEMPTS)
    monkeypatch.setattr(app, "build_agent", build)

    # 握りつぶすと worker が「エラーで回答できませんでした」を出せない
    with pytest.raises(_Boom):
        app._ask_with_retry("これで進めます")

    assert calls["n"] == app.MAX_ATTEMPTS
