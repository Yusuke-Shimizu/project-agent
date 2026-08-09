"""起案（書き戻し）のテスト。

**GitHub には触らない。** `Client(transport=...)` に偽の transport を差して、
呼び出しの並びと弾き方を固定する。

ここで一番大事なのは「通ること」ではなく **閉じていること** ―― path の allowlist、
根拠なしの拒否、既存本文を書き換えないこと、マージを呼ばないこと。
"""

from __future__ import annotations

import base64
import json

import pytest

from tools import writeback

EXISTING = """\
---
doc_id: KNW-006
doc_type: knowledge
title: 夜間の在庫締め処理中は更新イベントが滞留する
date: 2026-05-22
status: active
owner: SREリード
review_by: 2026-12-31
topic: async
---

# KNW-006: 夜間の在庫締め処理中は更新イベントが滞留する

## 制約
可視性タイムアウトを縮めると二重に適用される。
"""

NEW_DOC = """\
---
doc_id: KNW-007
doc_type: knowledge
title: 棚卸し中は在庫 API が読み取り専用になる
date: 2026-07-01
status: active
owner: SREリード
review_by: 2026-12-31
topic: async
---

# KNW-007: 棚卸し中は在庫 API が読み取り専用になる

## 制約
書き込みは 409 を返す。
"""

SOURCE_URL = "https://example.slack.com/archives/C0/p0"


class FakeGitHub:
    """呼ばれた API を記録する偽の transport。"""

    def __init__(self, open_pulls=None, files=None, existing_head=None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.open_pulls = open_pulls or []
        self.files = files or {}
        self.existing_head = existing_head

    def __call__(self, method: str, url: str, headers: dict, body: dict | None):
        path = url.split("/repos/", 1)[1].split("/", 2)[2]
        self.calls.append((method, "/" + path, body))

        if method == "GET" and path.startswith("pulls?state=open&head="):
            return [self.existing_head] if self.existing_head else []
        if method == "GET" and path.startswith("pulls?state=open"):
            return self.open_pulls
        if method == "GET" and path.startswith("git/ref/heads/"):
            return {"object": {"sha": "basesha"}}
        if method == "GET" and path.startswith("contents/"):
            name = path.removeprefix("contents/").split("?")[0]
            if name not in self.files:
                raise writeback.ProposalRejected(f"404 {name}")
            return {
                "content": base64.b64encode(self.files[name].encode("utf-8")).decode("ascii"),
                "sha": "filesha",
            }
        if method == "POST" and path == "pulls":
            return {"number": 42, "html_url": "https://github.com/o/r/pull/42"}
        return {}

    # --- 記録の読み方 ---

    def methods(self, prefix: str) -> list[tuple[str, str, dict | None]]:
        return [c for c in self.calls if c[1].startswith(prefix)]

    def written(self, path: str) -> str:
        for method, where, body in self.calls:
            if method == "PUT" and where == f"/contents/{path}":
                return base64.b64decode(body["content"]).decode("utf-8")
        raise AssertionError(f"{path} は書かれていない")

    @property
    def pull_body(self) -> str:
        for method, where, body in self.calls:
            if method == "POST" and where == "/pulls":
                return body["body"]
        raise AssertionError("PR が立っていない")


@pytest.fixture
def gh():
    return FakeGitHub(files={"knowledge_base/knowledge/KNW-006.md": EXISTING})


def client(fake) -> writeback.Client:
    return writeback.Client(repo="o/r", base="main", token="t", transport=fake)


def propose(fake, **over):
    kwargs = {
        "files": [{"path": "knowledge_base/knowledge/KNW-007.md", "content": NEW_DOC}],
        "summary": "棚卸し中の読み取り専用を起案する",
        "based_on": ["DEC-008b"],
        "source_url": SOURCE_URL,
        "requested_by": "U123",
        "client": client(fake),
    }
    kwargs.update(over)
    return writeback.propose_knowledge(**kwargs)


def append(fake, **over):
    kwargs = {
        "doc_id": "KNW-006",
        "body": "可視性タイムアウトの既定値は 1800 秒。",
        "based_on": ["KNW-006"],
        "source_url": SOURCE_URL,
        "requested_by": "U123",
        "client": client(fake),
    }
    kwargs.update(over)
    return writeback.propose_append(**kwargs)


# --- 新規起案 ---------------------------------------------------------------


def test_新規起案はブランチを切ってPRを立てる(gh):
    result = propose(gh)

    assert result["created"] is True
    assert result["doc_id"] == "KNW-007"
    assert result["pr_url"].endswith("/pull/42")
    assert [c[0] for c in gh.calls if c[0] in ("POST", "PUT")] == ["POST", "PUT", "POST", "POST"]
    assert gh.written("knowledge_base/knowledge/KNW-007.md") == NEW_DOC


def test_起案PRにラベルが付く(gh):
    propose(gh)
    labels = gh.methods("/issues/42/labels")
    assert labels and labels[0][2] == {"labels": [writeback.LABEL]}


def test_PR本文に根拠と出どころと依頼者が載る(gh):
    propose(gh)
    body = gh.pull_body
    assert "DEC-008b" in body
    assert SOURCE_URL in body
    assert "Requested-by: U123" in body


def test_マージするAPIは呼ばない(gh):
    # 「正本を変えるのは人のマージだけ」を呼び出しの並びで固定する
    propose(gh)
    assert not [c for c in gh.calls if "merge" in c[1]]


# --- 閉じている側 -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "knowledge_base/decisions/DEC-009.md",  # decision は人が起こす
        "knowledge_base/meetings/MTG-2026-01-01.md",
        ".github/workflows/evil.yml",  # プロンプトインジェクションの本命
        "code/tools/core.py",
        "knowledge_base/knowledge/../../.github/workflows/evil.yml",
        "knowledge_base/knowledge/notes.txt",
    ],
)
def test_allowlistの外のpathを弾く(gh, path):
    with pytest.raises(writeback.ProposalRejected, match="path"):
        propose(gh, files=[{"path": path, "content": NEW_DOC}])
    assert gh.calls == []  # GitHub を1回も叩かずに落ちる


def test_1PRに2ファイルは弾く(gh):
    with pytest.raises(writeback.ProposalRejected, match="1 ファイル"):
        propose(gh, files=[{"path": "knowledge_base/knowledge/KNW-007.md", "content": NEW_DOC}] * 2)


def test_based_onが空なら弾く(gh):
    with pytest.raises(writeback.ProposalRejected, match="based_on"):
        propose(gh, based_on=[])


def test_source_urlが無ければ弾く(gh):
    with pytest.raises(writeback.ProposalRejected, match="source_url"):
        propose(gh, source_url="")


def test_front_matterが壊れていればPRにする前に落とす(gh):
    with pytest.raises(writeback.ProposalRejected, match="front matter"):
        propose(gh, files=[{"path": "knowledge_base/knowledge/KNW-007.md", "content": "# 素\n"}])
    assert gh.calls == []


def test_ファイル名とdoc_idが違えば弾く(gh):
    with pytest.raises(writeback.ProposalRejected, match="doc_id"):
        propose(gh, files=[{"path": "knowledge_base/knowledge/KNW-999.md", "content": NEW_DOC}])


def test_未処理の起案が溜まっていたら起案しない(gh, monkeypatch):
    # §2 のバックプレッシャー。レビューが詰まっているときは提案もしない
    monkeypatch.setattr(writeback, "MAX_OPEN_PROPOSALS", 2)
    gh.open_pulls = [{"labels": [{"name": writeback.LABEL}]}] * 2
    with pytest.raises(writeback.ProposalRejected, match="未処理の起案"):
        propose(gh)


def test_人のPRはバックプレッシャーに数えない(gh, monkeypatch):
    monkeypatch.setattr(writeback, "MAX_OPEN_PROPOSALS", 2)
    gh.open_pulls = [{"labels": [{"name": "documentation"}]}] * 5
    assert propose(gh)["created"] is True


def test_同じ内容なら二度目はPRを増やさない(gh):
    gh.existing_head = {"html_url": "https://github.com/o/r/pull/7"}
    result = propose(gh)
    assert result["created"] is False
    assert result["pr_url"].endswith("/pull/7")
    assert not gh.methods("/git/refs")  # ブランチを切っていない


# --- 追記 -------------------------------------------------------------------


def test_追記は既存本文の末尾に足す(gh):
    append(gh)
    written = gh.written("knowledge_base/knowledge/KNW-006.md")

    # **既存本文が prefix のまま**＝1 文字も書き換えていない
    assert written.startswith(EXISTING.rstrip("\n"))
    assert written.endswith("可視性タイムアウトの既定値は 1800 秒。\n")


def test_追記の見出しに日付が入る(gh):
    append(gh)
    written = gh.written("knowledge_base/knowledge/KNW-006.md")
    assert f"## 追記 {writeback._today()}" in written


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        # **Lambda は UTC で動く。** JST 00:00〜09:00 に起案すると、UTC 基準では前日。
        # 正本の date は JST 基準なので、ここがずれると静かに 1 日古い日付が入る
        ("2026-08-09T16:00:00+00:00", "2026-08-10"),  # JST 8/10 01:00
        ("2026-08-09T14:59:00+00:00", "2026-08-09"),  # JST 8/9 23:59
        ("2026-12-31T15:00:00+00:00", "2027-01-01"),  # 年をまたぐ
    ],
)
def test_追記の日付はJSTで打つ(utc, expected):
    import datetime

    assert writeback._today(datetime.datetime.fromisoformat(utc)) == expected


def test_追記でfront_matterは変わらない(gh):
    append(gh)
    written = gh.written("knowledge_base/knowledge/KNW-006.md")
    before = EXISTING.split("---\n")[1]
    after = written.split("---\n")[1]
    assert before == after


def test_追記は既存のshaを渡す(gh):
    # sha を渡さないと GitHub は「新規作成」として 422 を返す（＝上書き事故は起きない）
    append(gh)
    put = [c for c in gh.calls if c[0] == "PUT" and "KNW-006" in c[1]]
    assert put and put[0][2]["sha"] == "filesha"


def test_追記の本文が空なら弾く(gh):
    with pytest.raises(writeback.ProposalRejected, match="空"):
        append(gh, body="  \n ")
    assert gh.calls == []


@pytest.mark.parametrize("doc_id", ["../../.github/workflows/evil", "KNW 006", "dec-008b", ""])
def test_追記先のdoc_idを検査する(gh, doc_id):
    with pytest.raises(writeback.ProposalRejected):
        append(gh, doc_id=doc_id)


def test_追記先が無ければ弾く(gh):
    with pytest.raises(writeback.ProposalRejected):
        append(gh, doc_id="KNW-404")


def test_追記でもマージするAPIは呼ばない(gh):
    append(gh)
    assert not [c for c in gh.calls if "merge" in c[1]]


# --- ハンドラ ---------------------------------------------------------------


class _Context:
    def __init__(self, name):
        self.client_context = type("C", (), {"custom": {"bedrockAgentCoreToolName": name}})()


def test_ハンドラはGatewayの前置きを剥がす(monkeypatch):
    from tools import writeback_handler

    seen = {}
    monkeypatch.setattr(
        writeback, "propose_append", lambda **kw: seen.update(kw) or {"pr_url": "x"}
    )
    out = writeback_handler.handler(
        {"doc_id": "KNW-006", "body": "b", "based_on": ["X"], "source_url": "u", "requested_by": "U"},
        _Context("kai___propose_append"),
    )
    assert out == {"pr_url": "x"}
    assert seen["doc_id"] == "KNW-006"


def test_ハンドラは拒否の理由を返す(monkeypatch):
    from tools import writeback_handler

    def boom(**_kw):
        raise writeback.ProposalRejected("based_on が空")

    monkeypatch.setattr(writeback, "propose_knowledge", boom)
    out = writeback_handler.handler({}, _Context("kai___propose_knowledge"))
    assert out == {"rejected": True, "reason": "based_on が空"}
    # 握りつぶすとエージェントから「空の結果」と区別できない
    assert json.dumps(out, ensure_ascii=False)


def test_ハンドラは未知のツールで落ちる():
    from tools import writeback_handler

    with pytest.raises(ValueError, match="未知のツール"):
        writeback_handler.handler({}, _Context("kai___nope"))
