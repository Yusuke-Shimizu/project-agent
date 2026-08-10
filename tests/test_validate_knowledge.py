"""正本の規約検証のテスト。

**書き戻し PR ツールのゲートになるスクリプト**なので、「壊れたものを弾けること」を
1 件ずつ固定する。通ることだけ確かめても、弾けているかは分からない。

AWS には触らない。
"""

import pathlib

import pytest

import validate_knowledge as vk

GOOD_DECISION = """\
---
doc_id: DEC-001
doc_type: decision
title: 認証は Cognito にする
date: 2025-07-01
status: active
supersedes: null
superseded_by: null
decided_by: アーキテクト
owner: SREリード
review_by: 2026-12-31
topic: auth
---

# DEC-001: 認証は Cognito にする

## 決定
Amazon Cognito を使う。
"""

GOOD_KNOWLEDGE = """\
---
doc_id: KNW-001
doc_type: knowledge
title: 連携先のレート制限
date: 2025-10-02
status: active
owner: SREリード
review_by: 2026-12-31
topic: integration
---

# KNW-001: 連携先のレート制限

## 制約
指数バックオフを入れる。
"""


def write(root: pathlib.Path, where: str, text: str) -> pathlib.Path:
    path = root / where
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    base = tmp_path / "knowledge_base"
    for name in ("decisions", "knowledge", "meetings"):
        (base / name).mkdir(parents=True)
    return base


# --- 通る側 -----------------------------------------------------------------


def test_規約どおりなら問題なし(root):
    write(root, "decisions/DEC-001.md", GOOD_DECISION)
    write(root, "knowledge/KNW-001.md", GOOD_KNOWLEDGE)
    assert vk.validate(root) == []


def test_空のディレクトリは通る(root):
    # 正本リポジトリを作った直後は 0 件。ここで落ちると初期状態が作れない
    assert vk.validate(root) == []


def test_READMEは検証しない(root):
    write(root, "README.md", "# これは正本ではない\n")
    assert vk.validate(root) == []


def test_rootが無ければ落ちる(tmp_path):
    with pytest.raises(SystemExit):
        vk.validate(tmp_path / "存在しない")


# --- front matter -----------------------------------------------------------


def test_front_matterが無いファイルを弾く(root):
    write(root, "knowledge/KNW-001.md", "# 素の markdown\n")
    assert any("front matter が無い" in e for e in vk.validate(root))


def test_必須キーの欠落を弾く(root):
    write(root, "knowledge/KNW-001.md", GOOD_KNOWLEDGE.replace("status: active\n", ""))
    assert any("status" in e for e in vk.validate(root))


def test_知らないstatusを弾く(root):
    write(root, "knowledge/KNW-001.md", GOOD_KNOWLEDGE.replace("status: active", "status: draft"))
    assert any("status が" in e for e in vk.validate(root))


def test_ISOでない日付を弾く(root):
    write(root, "knowledge/KNW-001.md", GOOD_KNOWLEDGE.replace("date: 2025-10-02", "date: 2025/10/02"))
    assert any("ISO 日付" in e for e in vk.validate(root))


@pytest.mark.parametrize("bad", ["2025/10/02", "2026-03-XX", "YYYY-MM-DD"])
def test_日付になっていない文字列を弾く(root, bad):
    # **形だけ見ると 2026-03-XX を通してしまう**（10 文字・ハイフン 2 個）。
    # エージェントが今日の日付を知らずにプレースホルダを書いてきたのを実測で見た
    write(root, "knowledge/KNW-001.md", GOOD_KNOWLEDGE.replace("date: 2025-10-02", f"date: {bad}"))
    assert any("ISO 日付" in e for e in vk.validate(root))


@pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-30"])
def test_存在しない日付も弾く(root, bad):
    # こちらは **PyYAML 自身が ValueError を投げる**（month must be in 1..12 など）ので、
    # メッセージは違うが同じく弾かれる。「弾かれること」だけを固定する
    write(root, "knowledge/KNW-001.md", GOOD_KNOWLEDGE.replace("date: 2025-10-02", f"date: {bad}"))
    errors = vk.validate(root)
    assert errors and "KNW-001" in errors[0]


def test_knowledgeにproposedは使えない(root):
    # proposed は「決定に昇格していない」という meeting 専用の語。起案は active で出す
    write(root, "knowledge/KNW-001.md", GOOD_KNOWLEDGE.replace("status: active", "status: proposed"))
    assert any("proposed は meeting だけ" in e for e in vk.validate(root))


def test_decisionにproposedも使えない(root):
    write(root, "decisions/DEC-001.md", GOOD_DECISION.replace("status: active", "status: proposed"))
    assert any("proposed は meeting だけ" in e for e in vk.validate(root))


def test_本文が空なら弾く(root):
    head, _, _ = GOOD_KNOWLEDGE.partition("\n\n#")
    write(root, "knowledge/KNW-001.md", head + "\n")
    assert any("本文が空" in e for e in vk.validate(root))


# --- 置き場所と doc_id ------------------------------------------------------


def test_ファイル名とdoc_idの食い違いを弾く(root):
    # get_document の ID 参照が空振りする（§6「doc_id で辿る」）
    write(root, "knowledge/KNW-999.md", GOOD_KNOWLEDGE)
    assert any("ファイル名と doc_id が違う" in e for e in vk.validate(root))


def test_置き場所と型の食い違いを弾く(root):
    write(root, "knowledge/DEC-001.md", GOOD_DECISION)
    assert any("doc_type=knowledge を置く" in e for e in vk.validate(root))


def test_3ディレクトリの外に置いたら弾く(root):
    write(root, "DEC-001.md", GOOD_DECISION)
    assert any("いずれかの直下に置く" in e for e in vk.validate(root))


def test_doc_idの重複を弾く(root):
    # 並行 PR で同じ番号を採番したときにここで落ちて、番号を振り直せば直る
    write(root, "knowledge/KNW-001.md", GOOD_KNOWLEDGE)
    write(root, "decisions/KNW-001.md", GOOD_KNOWLEDGE.replace("doc_type: knowledge", "doc_type: decision"))
    assert any("重複" in e for e in vk.validate(root))


# --- 版の連鎖 ---------------------------------------------------------------


def test_supersedesの逆参照が無ければ弾く(root):
    write(root, "decisions/DEC-002.md", GOOD_DECISION.replace("doc_id: DEC-001", "doc_id: DEC-002").replace("supersedes: null", "supersedes: DEC-001"))
    write(root, "decisions/DEC-001.md", GOOD_DECISION)
    assert any("逆参照が張られていない" in e for e in vk.validate(root))


def test_supersedesの相手が無ければ弾く(root):
    write(root, "decisions/DEC-002.md", GOOD_DECISION.replace("doc_id: DEC-001", "doc_id: DEC-002").replace("supersedes: null", "supersedes: DEC-000"))
    assert any("supersedes の相手が無い" in e for e in vk.validate(root))


def test_双方向に張られていれば通る(root):
    old = (
        GOOD_DECISION.replace("status: active", "status: superseded")
        .replace("superseded_by: null", "superseded_by: DEC-002")
    )
    new = (
        GOOD_DECISION.replace("doc_id: DEC-001", "doc_id: DEC-002")
        .replace("supersedes: null", "supersedes: DEC-001")
    )
    write(root, "decisions/DEC-001.md", old)
    write(root, "decisions/DEC-002.md", new)
    assert vk.validate(root) == []


def test_superseded_byがあるのにstatusがactiveなら弾く(root):
    write(root, "decisions/DEC-001.md", GOOD_DECISION.replace("superseded_by: null", "superseded_by: DEC-002"))
    assert any("status: superseded" in e for e in vk.validate(root))


def test_supersededなのにsuperseded_byが無ければ弾く(root):
    write(root, "decisions/DEC-001.md", GOOD_DECISION.replace("status: active", "status: superseded"))
    assert any("superseded_by が要る" in e for e in vk.validate(root))


def test_decision以外は版の連鎖を持てない(root):
    # エージェントが knowledge に supersedes を書いてしまうのを弾く
    write(root, "knowledge/KNW-001.md", GOOD_KNOWLEDGE.replace("topic: integration", "supersedes: KNW-000\ntopic: integration"))
    assert any("decision だけが持てる" in e for e in vk.validate(root))


# --- meeting ----------------------------------------------------------------


def test_meetingがproposed以外なら弾く(root):
    text = (
        GOOD_KNOWLEDGE.replace("doc_id: KNW-001", "doc_id: MTG-2026-03-15")
        .replace("doc_type: knowledge", "doc_type: meeting")
        .replace("# KNW-001", "# MTG-2026-03-15")
    )
    write(root, "meetings/MTG-2026-03-15.md", text)
    assert any("status: proposed" in e for e in vk.validate(root))


# --- topic の allowlist -----------------------------------------------------


def test_allowlistに無いtopicを弾く(root):
    write(root, "knowledge/KNW-001.md", GOOD_KNOWLEDGE)
    errors = vk.validate(root, topics={"auth", "async"})
    assert any("allowlist に無い" in e for e in errors)


def test_allowlistを渡さなければtopicを見ない(root):
    write(root, "knowledge/KNW-001.md", GOOD_KNOWLEDGE.replace("topic: integration", "topic: 好き勝手な値"))
    assert vk.validate(root) == []


def test_topicが無ければ弾く(root):
    write(root, "knowledge/KNW-001.md", GOOD_KNOWLEDGE.replace("topic: integration\n", ""))
    assert any("topic が無い" in e for e in vk.validate(root, topics={"auth"}))


def test_topicsファイルを読める(tmp_path):
    path = tmp_path / "topics.yml"
    path.write_text("topics:\n  - auth\n  - async\n", encoding="utf-8")
    assert vk.load_topics(path) == {"auth", "async"}


def test_topicsファイルが無ければ落ちる(tmp_path):
    with pytest.raises(SystemExit):
        vk.load_topics(tmp_path / "無い.yml")


def test_topicsを渡さなければNone():
    assert vk.load_topics(None) is None


# --- まとめて報告する -------------------------------------------------------


def test_問題は最初の1件で止めずに全部返す(root):
    # PR のレビューで「直して push、また別の指摘」を繰り返させないため
    write(root, "knowledge/KNW-001.md", GOOD_KNOWLEDGE.replace("status: active", "status: draft").replace("date: 2025-10-02", "date: 2025/10/02"))
    assert len(vk.validate(root)) >= 2
