"""起案を**エージェントを通さずに**確かめる（`writeback_pr_tool.md` §9 の L4a の判定）。

`check_gateway.py` と同じ位置づけ。Gateway にもエージェントにも載せていない段で、
「PR が正しい形で立つか」だけを確かめる。**デモ経路を人質に取らない。**

    # 起案が受け付けられない側（AWS も GitHub も叩かない）
    uv run python code/scripts/check_propose.py --dry-run

    # 実際に PR を立てる（GitHub App の設定が済んでいること）
    uv run python code/scripts/check_propose.py --append KNW-006
    uv run python code/scripts/check_propose.py --new KNW-007
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools import writeback  # noqa: E402

SOURCE_URL = "https://example.slack.com/archives/C0000000000/p0000000000000000"

#: `--new` で出す doc の雛形。**front matter はエージェントが書く**ので、
#: このスクリプトも自分で書いて渡す（人の PR と同じ経路）
NEW_TEMPLATE = """\
---
doc_id: {doc_id}
doc_type: knowledge
title: 起案の配線確認（あとで消す）
date: {date}
status: active
owner: SREリード
review_by: 2026-12-31
topic: integration
---

# {doc_id}: 起案の配線確認（あとで消す）

## 制約
これは `check_propose.py` が立てた確認用の起案。**中身に意味は無いので閉じてよい。**
"""


def rejections() -> list[tuple[str, str]]:
    """**受け付けないもの**を並べる。通る側だけ試しても、閉じているかは分からない。"""
    cases: list[tuple[str, str]] = []

    def run(label: str, fn) -> None:
        try:
            fn()
        except writeback.ProposalRejected as exc:
            cases.append((label, str(exc)))
        else:
            cases.append((label, "★ 通ってしまった"))

    # transport を呼ぶ前に落ちる場合だけを並べる（GitHub に触らない）
    stub = writeback.Client(token="dummy", transport=_never)

    run(
        "path が allowlist の外（decisions/）",
        lambda: writeback.propose_knowledge(
            files=[{"path": "knowledge_base/decisions/DEC-009.md", "content": "---\n---\n"}],
            summary="x", based_on=["DEC-001"], source_url=SOURCE_URL,
            requested_by="check", client=stub,
        ),
    )
    run(
        "path が knowledge_base の外（ワークフロー）",
        lambda: writeback.propose_knowledge(
            files=[{"path": ".github/workflows/evil.yml", "content": "---\n---\n"}],
            summary="x", based_on=["DEC-001"], source_url=SOURCE_URL,
            requested_by="check", client=stub,
        ),
    )
    run(
        "1 PR に 2 ファイル",
        lambda: writeback.propose_knowledge(
            files=[{"path": "knowledge_base/knowledge/KNW-900.md", "content": "x"}] * 2,
            summary="x", based_on=["DEC-001"], source_url=SOURCE_URL,
            requested_by="check", client=stub,
        ),
    )
    run(
        "front matter が無い",
        lambda: writeback.propose_knowledge(
            files=[{"path": "knowledge_base/knowledge/KNW-900.md", "content": "# 素の markdown\n"}],
            summary="x", based_on=["DEC-001"], source_url=SOURCE_URL,
            requested_by="check", client=stub,
        ),
    )
    run(
        "based_on が空",
        lambda: writeback.propose_append(
            doc_id="KNW-006", body="x", based_on=[], source_url=SOURCE_URL,
            requested_by="check", client=stub,
        ),
    )
    run(
        "追記の本文が空",
        lambda: writeback.propose_append(
            doc_id="KNW-006", body="   ", based_on=["DEC-001"], source_url=SOURCE_URL,
            requested_by="check", client=stub,
        ),
    )
    return cases


def _never(*_args, **_kwargs):
    raise AssertionError("GitHub を叩く前に落ちるはずの経路で transport が呼ばれた")


def main() -> None:
    parser = argparse.ArgumentParser(description="起案をエージェントを通さず確かめる")
    parser.add_argument("--dry-run", action="store_true", help="拒否される側だけ試す（GitHub に触らない）")
    parser.add_argument("--new", metavar="DOC_ID", help="新規起案の PR を実際に立てる")
    parser.add_argument("--append", metavar="DOC_ID", help="追記の PR を実際に立てる")
    args = parser.parse_args()

    print("--- 受け付けない側 ---")
    bad = 0
    for label, reason in rejections():
        marker = "★" if reason.startswith("★") else " "
        if marker == "★":
            bad += 1
        print(f" {marker} {label}\n      → {reason}")
    if bad:
        raise SystemExit(f"{bad} 件が閉じていない")

    if args.dry_run:
        print("\n(--dry-run のため GitHub には触らない)")
        return

    if args.new:
        import datetime

        result = writeback.propose_knowledge(
            files=[
                {
                    "path": f"knowledge_base/knowledge/{args.new}.md",
                    "content": NEW_TEMPLATE.format(
                        doc_id=args.new, date=datetime.date.today().isoformat()
                    ),
                }
            ],
            summary=f"{args.new} を起案する（配線確認）",
            based_on=["KNW-006"],
            source_url=SOURCE_URL,
            requested_by="check_propose.py",
        )
        print(f"\n新規起案: {result}")

    if args.append:
        result = writeback.propose_append(
            doc_id=args.append,
            body="配線確認のための追記。**中身に意味は無いので閉じてよい。**",
            based_on=["DEC-008b"],
            source_url=SOURCE_URL,
            requested_by="check_propose.py",
        )
        print(f"\n追記の起案: {result}")

    if not args.new and not args.append:
        print("\n（--new / --append を指定すると実際に PR を立てる）")


if __name__ == "__main__":
    main()
