"""起案の提案（マーカー行 → ボタン）のテスト。

**worker と interactive の間の契約**なので、両側から見た形を固定する。
壊れたマーカーで回答が消えないこと、根拠なしの提案が出ないことが要点。

AWS にも Slack にも触らない。
"""

from __future__ import annotations

import json

import pytest

import proposal

ANSWER = """\
【指摘】
PartnerSync の即時リトライは KNW-002 の制約とズレています。

【根拠】
KNW-002（2025-10-02, active）

【確認してほしいこと】
バックオフの実装方針を決めてください。"""

MARKED = (
    ANSWER
    + "\n[[PROPOSE]] kind=append doc_id=KNW-002 based_on=KNW-002,DEC-003b "
    "summary=リトライ実装の既定値を追記する"
)


# --- マーカーの読み取り -----------------------------------------------------


def test_マーカーを剥がして提案を取り出す():
    text, got = proposal.parse_marker(MARKED)

    # 人に見せる文面は 3 ブロックのまま
    assert text == ANSWER
    assert "[[PROPOSE]]" not in text
    assert got == {
        "kind": "append",
        "doc_id": "KNW-002",
        "based_on": ["KNW-002", "DEC-003b"],
        "summary": "リトライ実装の既定値を追記する",
    }


def test_マーカーが無ければそのまま返す():
    text, got = proposal.parse_marker(ANSWER)
    assert text == ANSWER
    assert got is None


def test_新規起案のマーカー():
    text, got = proposal.parse_marker(
        ANSWER + "\n[[PROPOSE]] kind=new based_on=DEC-004 summary=棚卸し中の制約を残す"
    )
    assert got["kind"] == "new"
    assert got["doc_id"] == ""
    assert got["summary"] == "棚卸し中の制約を残す"


def test_要旨に空白が入っていても最後まで取る():
    _, got = proposal.parse_marker(
        ANSWER + "\n[[PROPOSE]] kind=new based_on=DEC-004 summary=A B C  D"
    )
    assert got["summary"] == "A B C  D"


@pytest.mark.parametrize(
    "marker",
    [
        "[[PROPOSE]] kind=whatever based_on=X summary=y",  # 知らない kind
        "[[PROPOSE]] kind=append based_on=X summary=y",  # append なのに doc_id が無い
        "[[PROPOSE]] kind=new summary=y",  # 根拠が無い
        "[[PROPOSE]] kind=new based_on=X",  # 要旨が無い
        "[[PROPOSE]]",  # 空
    ],
)
def test_壊れたマーカーは提案なしとして扱う(marker):
    # **回答は届かないといけない。**提案が出ないだけで済ませる
    text, got = proposal.parse_marker(ANSWER + "\n" + marker)
    assert got is None
    assert text == ANSWER


# --- ボタン -----------------------------------------------------------------


@pytest.fixture
def prop():
    return {
        "kind": "append",
        "doc_id": "KNW-002",
        "based_on": ["KNW-002"],
        "summary": "リトライ実装の既定値を追記する",
    }


def test_ボタンは起案と不要の2つ(prop):
    actions = [b for b in proposal.blocks(ANSWER, prop) if b["type"] == "actions"][0]
    ids = [e["action_id"] for e in actions["elements"]]
    # **「不要」が無いと、押されていないのか断られたのか区別できない**（的中率が測れない）
    assert ids == [proposal.ACTION_PROPOSE, proposal.ACTION_DISMISS]


def test_ボタンのラベルに追記先が出る(prop):
    actions = [b for b in proposal.blocks(ANSWER, prop) if b["type"] == "actions"][0]
    assert "KNW-002" in actions["elements"][0]["text"]["text"]


def test_新規起案のラベルはdoc_idを出さない():
    prop = {"kind": "new", "doc_id": "", "based_on": ["DEC-004"], "summary": "x"}
    actions = [b for b in proposal.blocks(ANSWER, prop) if b["type"] == "actions"][0]
    assert actions["elements"][0]["text"]["text"] == "knowledge として起案する"


def test_回答本文がblocksに載る(prop):
    section = proposal.blocks(ANSWER, prop)[0]
    assert section["text"]["text"] == ANSWER


def test_マージされないことを添える(prop):
    text = json.dumps(proposal.blocks(ANSWER, prop), ensure_ascii=False)
    assert "正本には入りません" in text


def test_ボタンのvalueは往復できる(prop):
    raw = json.loads(proposal.button_value(prop))
    assert raw == {"k": "append", "d": "KNW-002", "b": ["KNW-002"], "s": prop["summary"]}


def test_長い要旨でもvalueの上限に収める():
    prop = {"kind": "new", "doc_id": "", "based_on": ["DEC-004"], "summary": "あ" * 3000}
    value = proposal.button_value(prop)
    assert len(value) <= proposal.MAX_VALUE
    # **根拠 doc_id は落とさない**（起案の前提なので）
    assert json.loads(value)["b"] == ["DEC-004"]


# --- クリック後の指示文 -----------------------------------------------------


def test_指示文に根拠と出どころと依頼者が入る(prop):
    text = proposal.directive(prop, "https://slack/x", "U123")
    assert "KNW-002" in text
    assert "https://slack/x" in text
    assert "U123" in text
    assert "propose_append" in text


def test_新規起案の指示文はpropose_knowledgeを指す():
    prop = {"kind": "new", "doc_id": "", "based_on": ["DEC-004"], "summary": "x"}
    text = proposal.directive(prop, "https://slack/x", "U1")
    assert "propose_knowledge" in text
    assert "採番" in text


def test_新規起案の指示文に今日の日付が入る():
    # **エージェントは今日の日付を知らない。**実測で date: 2026-03-XX を書いてきた
    import datetime

    prop = {"kind": "new", "doc_id": "", "based_on": ["DEC-004"], "summary": "x"}
    text = proposal.directive(prop, "u", "U1")
    today = datetime.datetime.now(proposal.JST).date().isoformat()
    assert today in text


def test_front_matterの要求は日付を注入できる():
    import datetime

    # JST 8/10 01:00（UTC では 8/9）
    now = datetime.datetime.fromisoformat("2026-08-09T16:00:00+00:00")
    assert "2026-08-10" in proposal.front_matter_rules(now)


def test_新規起案の指示文はstatusをactiveと言う():
    prop = {"kind": "new", "doc_id": "", "based_on": ["DEC-004"], "summary": "x"}
    text = proposal.directive(prop, "u", "U1")
    assert "active" in text and "proposed にはしない" in text


def test_追記の指示文にはfront_matterの要求を入れない(prop):
    # 追記は front matter を触らないので、渡すと逆に迷わせる
    assert "front matter は次を満たすこと" not in proposal.directive(prop, "u", "U1")


def test_指示文は根拠を読ませる(prop):
    # 出力契約ルール1（読んでいない doc を根拠に挙げない）の書き込み側
    assert "実際に読んでから" in proposal.directive(prop, "u", "U1")


# --- ボタンだけ外す ---------------------------------------------------------


def test_ボタンの行だけ外して本文は残す(prop):
    original = proposal.blocks(ANSWER, prop)
    left = proposal.without_actions(original)

    # **回答が消えないこと**が要点。以前は本文ごと差し替えていて追えなくなっていた
    assert [b["type"] for b in left] == ["section", "context"]
    assert left[0]["text"]["text"] == ANSWER
    assert not [b for b in left if b["type"] == "actions"]


@pytest.mark.parametrize("blocks", [None, [], [{"type": "actions", "elements": []}]])
def test_外した結果が空でも落ちない(blocks):
    assert proposal.without_actions(blocks) == []
