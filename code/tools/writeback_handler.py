"""起案 Lambda のハンドラ（`writeback_pr_tool.md` §9 の L4a）。

`lambda_handler.py` と同じ形の**薄い皮**。中身は書かず `writeback.py` に委譲する。
`___` の剥がし方も同じなので、**L4b で Gateway のターゲットにするときにこのファイルは
1 行も変わらない**（読みの 2 ツールで答え合わせが済んでいる形をそのまま使う）。

なぜ読みのツールと**別の Lambda**にするか:

- 実行ロールに与える権限が違う。**このロールだけが GitHub App の秘密鍵を読める**。
  読みの Lambda に与えないことが「エージェント自身は書けない」の裏返しになる（§6）
- 壊れ方を分けたい。起案の不具合で `search_project_knowledge` が落ちてはいけない
"""

from __future__ import annotations

from tools import writeback

#: Gateway がツール名に付ける前置きの区切り
DELIMITER = "___"


def _tool_name(context) -> str:
    custom = getattr(context, "client_context", None)
    raw = (getattr(custom, "custom", None) or {}).get("bedrockAgentCoreToolName", "")
    _, _, name = raw.partition(DELIMITER)
    return name or raw


def handler(event, context):
    name = _tool_name(context)

    try:
        if name == "propose_knowledge":
            return writeback.propose_knowledge(
                files=event.get("files") or [],
                summary=event.get("summary") or "",
                based_on=event.get("based_on") or [],
                source_url=event.get("source_url") or "",
                requested_by=event.get("requested_by") or "",
            )

        if name == "propose_append":
            return writeback.propose_append(
                doc_id=event.get("doc_id") or "",
                body=event.get("body") or "",
                based_on=event.get("based_on") or [],
                source_url=event.get("source_url") or "",
                requested_by=event.get("requested_by") or "",
            )
    except writeback.ProposalRejected as exc:
        # **理由を返す。**握りつぶすとエージェントからは「空の結果」と区別できない
        return {"rejected": True, "reason": str(exc)}

    raise ValueError(f"未知のツール: {name!r}")
