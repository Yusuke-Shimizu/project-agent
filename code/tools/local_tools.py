"""Strands の `@tool` ラッパ。

中身は書かず `core.py` に委譲する。architecture_v1.md §4.3 の退避ラインで、
L3（Gateway + Lambda）に上げたあとも同じ `core.py` を Lambda 側から呼ぶので、
このファイルと `lambda_handler.py` はどちらも「薄い皮」のまま変わらない。

docstring と型注釈がそのままツールのスキーマになるので、ここの文言は
エージェントがツールを選ぶときの判断材料になる。
"""

from __future__ import annotations

from strands import tool

from tools import core


@tool
def search_project_knowledge(
    query: str,
    doc_type: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """案件 KAI の正本（decision / knowledge / meeting）を検索する。

    設計方針・制約・過去の決定について確認したいときに使う。結果には本文全文が
    含まれるので、追加で本文を取りに行く必要はない。

    Args:
        query: 検索クエリ。自然文でよい
        doc_type: decision | knowledge | meeting に絞る。省略時は全種類
        status: active | superseded | proposed に絞る。省略時は全状態

    Returns:
        ヒットしたドキュメント（doc_id / doc_type / title / date / status /
        supersedes / body / s3_uri）のリスト。該当が無ければ空リスト。
        空リストは「その論点に関する記録が正本に無い」ことを意味する。
    """
    return core.search_project_knowledge(query, doc_type=doc_type, status=status)


@tool
def get_document(doc_id: str) -> dict:
    """doc_id を指定してドキュメントを 1 件取る。

    検索ではなく ID 参照。ある decision の `supersedes` / `superseded_by` を辿って
    旧版・新版を確認するときは、検索順位に依存させないためこちらを使う。

    Args:
        doc_id: 例 DEC-003a, KNW-002, MTG-2026-03-15

    Returns:
        ドキュメント 1 件。見つからない場合は found: False を含む dict。
    """
    return core.get_document(doc_id)


#: エージェントに渡すツール一覧
TOOLS = [search_project_knowledge, get_document]
