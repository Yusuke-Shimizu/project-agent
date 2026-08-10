"""台本の判定（`run_demo_script.py`）のテスト。

**判定そのもののバグを 3 回踏んだ**ので、物差しの側をテストで固定する:

1. 予備問（記録が無いと答える問）に引用を無条件に要求して FAIL させた
2. 「…問題ないですか？」を語尾の列挙で拾えず FAIL させた
3. 「決定は正本に無い」を `記録は無い` しか見ていなくて FAIL させた

**3 つとも答えは正しくて判定が落としていた。** 物差しを変えたら物差しを検証する。
"""

from __future__ import annotations

import pytest

import run_demo_script as judge


# --- 「記録が無い」と言えているか -------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "現行の正本に該当する記録は見つからない。",
        "監視ツール選定に関する記録は正本に見つからない。",
        "Datadogを使うツール自体の決定は正本に無いけど、先に確認すべき制約がある。",
        "監視基盤についての decision も knowledge も存在しない。",
        "該当する記録はありません。",
    ],
)
def test_正本に無いと言えていれば通る(answer):
    assert judge.ABSENCE.search(answer)


@pytest.mark.parametrize(
    "answer",
    [
        # **相手の状況**の話であって、正本を引いた結果ではない
        "コスト面を決めていないので、方針を決めて decision を起こしてもらう必要がある。",
        "判断材料が無いので決めてもらえますか。",
        # 逆に「ある」と言っている
        "監視の decision は DEC-009 に存在します。",
    ],
)
def test_正本への否定になっていなければ落とす(answer):
    assert not judge.ABSENCE.search(answer)


# --- 根拠の書式 -------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "DEC-004（2025-09-18, active）で決まっています。",
        "KNW-002（2025-10-02, active）に事例がある。",
        "DEC-003a（2025-07-24, superseded）→ DEC-003b（2025-08-20, active）。",
    ],
)
def test_日付とstatusが付いていれば通る(answer):
    assert judge.EVIDENCE.search(answer)


@pytest.mark.parametrize(
    "answer",
    [
        "DEC-004 で決まっています。",  # 日付も status も無い
        "DEC-004（2025-09-18）で決まっています。",  # status が無い
        "DEC-004（active）で決まっています。",  # 日付が無い
    ],
)
def test_日付かstatusが欠けていれば落とす(answer):
    assert not judge.EVIDENCE.search(answer)


def test_原文の引用を見る():
    assert judge.QUOTE.search("本文\n> 引用した一文\n続き")
    assert not judge.QUOTE.search("本文だけで引用が無い")


# --- 人に返しているか -------------------------------------------------------


@pytest.mark.parametrize(
    "closing",
    [
        "どちらにしますか。",
        "SQS + Lambda 前提で進める形で問題ないですか？",
        "見積もりは揃っていますか？",
        "書き直すべきですか？",
        "バックオフの間隔をどう決めますか。",
        "起案してもらえますか。",
    ],
)
def test_最後に人へ返していれば通る(closing):
    assert judge.ASK.search(judge.closing_line("本文\n\n" + closing))


@pytest.mark.parametrize(
    "closing",
    ["SQS + Lambda に変更されています。", "即時リトライは禁止です。"],
)
def test_締めが断定なら落とす(closing):
    assert not judge.ASK.search(judge.closing_line("本文\n\n" + closing))


def test_締めは最後の行だけを見る():
    # **途中に疑問文があっても締めが断定なら契約は守れていない**
    text = "これは合っていますか？\n\n現行方針は SQS + Lambda です。"
    assert not judge.ASK.search(judge.closing_line(text))


def test_空行は無視して最後の中身を取る():
    assert judge.closing_line("あ\n\nい\n\n\n") == "い"
