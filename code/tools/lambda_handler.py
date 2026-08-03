"""AgentCore Gateway（Lambda ターゲット）用のハンドラ（architecture_v1.md §4.3）。

`local_tools.py` と対になる **もう一枚の薄い皮**。中身は書かず `core.py` に委譲する。
この 2 ファイルが薄いままでいられることが、§4.3 の退避ライン
（「Gateway で詰まったら同じロジックを `@tool` として直接同梱する」）の実体そのもの。

Gateway からの呼ばれ方は Lambda の普通の invoke とは違う:

- `event` は **ツールの引数そのもの**（`{"query": "...", "doc_type": "decision"}`）
- 呼ばれたツール名は `context.client_context.custom['bedrockAgentCoreToolName']` に入る
- その名前は **`<ターゲット名>___<ツール名>`** という形（区切りは `___`）なので、
  ハンドラ側で前置きを剥がす必要がある

戻り値はそのまま MCP のツール結果になる。**JSON にできる形で返す。**
"""

from __future__ import annotations

from tools import core

#: Gateway がツール名に付ける前置きの区切り
DELIMITER = "___"


def _tool_name(context) -> str:
    """`<ターゲット名>___<ツール名>` からツール名だけを取り出す。

    前置きは Gateway が付けるものなので、**ツールの I/F の一部ではない**。
    ここで剥がしておけば、`core.py` は Gateway の存在を知らずに済む。
    """
    custom = getattr(context, "client_context", None)
    raw = (getattr(custom, "custom", None) or {}).get("bedrockAgentCoreToolName", "")
    _, _, name = raw.partition(DELIMITER)
    return name or raw


def handler(event, context):
    name = _tool_name(context)

    if name == "search_project_knowledge":
        return core.search_project_knowledge(
            event["query"],
            doc_type=event.get("doc_type"),
            status=event.get("status"),
        )

    if name == "get_document":
        return core.get_document(event["doc_id"])

    # 握りつぶすとエージェント側は「空の結果」と区別できない
    raise ValueError(f"未知のツール: {name!r}")
