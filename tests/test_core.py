"""ツール単体のテスト。

L1 で `search_project_knowledge` の中身を Managed KB の Retrieve に差し替えたとき、
ここが通れば呼び出し側の契約は壊れていない。AWS には一切触らない。
"""

import pytest

from tools import core


@pytest.fixture(autouse=True)
def _fresh_cache():
    core.reset_cache()
    yield
    core.reset_cache()


# --- front matter -----------------------------------------------------------


def test_全ドキュメントが仕様どおりのfront_matterを持つ():
    docs = core._load_all()
    assert len(docs) == 14

    for doc in docs:
        assert doc.doc_type in ("decision", "knowledge", "meeting")
        assert doc.status in ("active", "superseded", "proposed", "rejected")
        # date は ISO 日付。範囲フィルタと「旧→新」の提示がこれに乗る
        assert len(doc.date) == 10 and doc.date.count("-") == 2
        assert doc.body


def test_meetingは正本ではないのでproposed():
    for doc in core._load_all():
        if doc.doc_type == "meeting":
            assert doc.status == "proposed"


def test_supersedesが双方向に張られている():
    old = core.get_document("DEC-003a")
    new = core.get_document("DEC-003b")
    assert old["status"] == "superseded"
    assert old["superseded_by"] == "DEC-003b"
    assert new["status"] == "active"
    assert new["supersedes"] == "DEC-003a"


# --- get_document -----------------------------------------------------------


def test_get_documentはdoc_idで引ける():
    doc = core.get_document("KNW-002")
    assert doc["title"].startswith("PartnerSync")
    assert "10 req/sec" in doc["body"]


def test_get_documentは大文字小文字を無視する():
    assert core.get_document("dec-004")["doc_id"] == "DEC-004"


def test_存在しないdoc_idはfound_Falseを返す():
    # 例外にしないのは、エージェントに「無い」と言わせるため
    assert core.get_document("DEC-999") == {"doc_id": "DEC-999", "found": False}


# --- search -----------------------------------------------------------------


@pytest.mark.parametrize(
    "query, expected",
    [
        # 問1: 現行の正本（DEC-004）と未昇格の検討メモ（MTG）が両方出ること
        (
            "マルチテナント 大口テナント 物理分離",
            {"DEC-004", "MTG-2026-03-15"},
        ),
        # 問2: 旧版と新版が両方出ること
        ("非同期処理は Step Functions で組む予定です", {"DEC-003a", "DEC-003b"}),
        # 問3: 固有名詞での引き当て
        ("PartnerSync連携 即時リトライ", {"KNW-002"}),
    ],
)
def test_台本の各問で期待するdocが上位に出る(query, expected):
    hits = core.search_project_knowledge(query, limit=5)
    assert expected <= {hit["doc_id"] for hit in hits}


def test_検索結果に本文全文が乗る():
    # chunkingStrategy NONE 相当。エージェントは body を読んで根拠にする
    hits = core.search_project_knowledge("マルチテナント", limit=1)
    assert hits[0]["body"] == core.get_document(hits[0]["doc_id"])["body"]


def test_doc_typeで絞れる():
    hits = core.search_project_knowledge("テナント", doc_type="decision")
    assert {hit["doc_type"] for hit in hits} == {"decision"}


def test_statusで絞れる():
    hits = core.search_project_knowledge("非同期処理", status="superseded")
    assert [hit["doc_id"] for hit in hits] == ["DEC-003a"]


def test_該当なしは空リスト():
    # エージェント側に「根拠なし」と言わせるための契約
    assert core.search_project_knowledge("量子コンピュータ") == []


def test_limitを超えて返さない():
    assert len(core.search_project_knowledge("テナント", limit=2)) == 2


# --- KB 結果の束ね（L1c）-----------------------------------------------------
# Managed KB は同じ doc を「抜粋」と「全文」の2エントリで返す。束ねないと
# エージェントが同じ doc を2回見る。AWS は呼ばず、返却形だけを模して検証する。


def _hit(doc_id, score):
    return {"metadata": {"doc_id": doc_id}, "score": score}


def test_同じdocの複数エントリが1件に束ねられる():
    merged = core.merge_kb_results([_hit("DEC-004", 0.55), _hit("DEC-004", 0.41)])
    assert merged == [("DEC-004", 0.55)]  # スコアは最大値を採る


def test_スコアの降順に並ぶ():
    merged = core.merge_kb_results(
        [_hit("DEC-004", 0.41), _hit("MTG-2026-03-15", 0.58), _hit("DEC-004", 0.55)]
    )
    assert merged == [("MTG-2026-03-15", 0.58), ("DEC-004", 0.55)]


def test_同点なら_doc_id順で安定する():
    merged = core.merge_kb_results([_hit("KNW-002", 0.5), _hit("DEC-001", 0.5)])
    assert merged == [("DEC-001", 0.5), ("KNW-002", 0.5)]


def test_doc_idの無いエントリは捨てる():
    # メタデータが乗っていない結果は正本と突き合わせられないので使わない
    assert core.merge_kb_results([{"score": 0.9}, {"metadata": {}, "score": 0.8}]) == []


def test_スコア欠落は0扱いで落ちない():
    assert core.merge_kb_results([_hit("DEC-001", None)]) == [("DEC-001", 0.0)]
