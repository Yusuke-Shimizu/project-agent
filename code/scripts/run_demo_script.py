"""台本 3 問 + 予備 1 問を連続実行して、回答の安定性を確認する。

architecture_v1.md §1 のとおり、v1 の要件は汎用性ではなく
**この 4 問が安定して同じ答えを返すこと**。当日前にこれを複数回流す。

    uv run python code/scripts/run_demo_script.py
    uv run python code/scripts/run_demo_script.py --repeat 3
    uv run python code/scripts/run_demo_script.py --only Q1 --show

判定は「期待する doc_id を根拠に挙げているか」だけを機械的に見る。文章の良し悪しは
人が読んで判断する（--show で全文を出す）。
"""

from __future__ import annotations

import argparse
import re
import sys

from runtime.agent import build_agent

#: doc_id の書式
DOC_ID = re.compile(r"\b(?:DEC-\d+[ab]?|KNW-\d+|MTG-\d{4}-\d{2}-\d{2})\b")

#: 「正本に該当の記録が無い」と明言できているかの判定に使う言い回し
ABSENCE = re.compile(r"見つか(らな|りませ)|存在し(ない|ませ)|記録は(無|な)い")

DEMO_SCRIPT = [
    {
        "id": "Q1",
        "title": "矛盾検知・非断定（マルチテナント方式）",
        "prompt": (
            "設計書ドラフトのレビューお願いします。マルチテナント、"
            "大口テナント向けにテナントごとにDBを物理分離する方針で書きました。"
        ),
        # DEC-004（active）と MTG-2026-03-15（proposed）の両方を出せて初めて
        # 「検討記録はあるが正式決定ではない」が言える
        "expect": ["DEC-004", "MTG-2026-03-15"],
    },
    {
        "id": "Q2",
        "title": "superseded・版管理（非同期処理基盤）",
        "prompt": "非同期処理は Step Functions で組む予定です。",
        # 旧版と新版の両方。片方だけなら「置換された」が言えていない
        "expect": ["DEC-003a", "DEC-003b"],
    },
    {
        "id": "Q3",
        "title": "暗黙知の想起（外部連携）",
        "prompt": "PartnerSync連携、まずは即時リトライのシンプル実装でいきます。",
        "expect": ["KNW-002"],
    },
    {
        "id": "Q4",
        "title": "根拠なしの明言（監視ツール）― 予備問",
        "prompt": "監視は Datadog を入れる案、どう思う？",
        # 監視ツール選定の decision は正本に無い。「無い」と言えることが要件。
        # 周辺の制約（IaC 統一・SLA など）を本文を読んだ上で併記するのは正しい挙動なので、
        # doc_id を挙げること自体は禁じない
        "expect": [],
        "require_absence": True,
    },
]


def answer_text(result) -> str:
    """AgentResult から本文テキストだけを取り出す。"""
    content = result.message.get("content", []) if isinstance(result.message, dict) else []
    return "\n".join(block["text"] for block in content if "text" in block)


def check(question: dict, answer: str) -> tuple[bool, list[str]]:
    """期待する doc_id を根拠に挙げているか。"""
    problems = []

    for doc_id in question["expect"]:
        if doc_id not in answer:
            problems.append(f"{doc_id} を挙げていない")

    if question.get("require_absence") and not ABSENCE.search(answer):
        problems.append("「正本に該当の記録が無い」と明言していない")

    return not problems, problems


def run_once(agent, question: dict, show: bool) -> bool:
    answer = answer_text(agent(question["prompt"]))

    ok, problems = check(question, answer)
    mark = "PASS" if ok else "FAIL"
    cited = sorted(set(DOC_ID.findall(answer)))

    print(f"  [{mark}] {question['id']} {question['title']}")
    print(f"        参照: {', '.join(cited) if cited else '(なし)'}")
    for problem in problems:
        print(f"        ! {problem}")
    if show:
        print("        --- 回答 ---")
        for line in answer.splitlines():
            print(f"        {line}")
        print("        ------------")

    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="台本 4 問の連続実行")
    parser.add_argument("--repeat", type=int, default=1, help="各問を何回流すか")
    parser.add_argument("--only", help="Q1 など、特定の問だけ流す")
    parser.add_argument("--show", action="store_true", help="回答の全文を表示する")
    args = parser.parse_args()

    questions = DEMO_SCRIPT
    if args.only:
        questions = [q for q in DEMO_SCRIPT if q["id"] == args.only.upper()]
        if not questions:
            parser.error(f"そんな問は無い: {args.only}")

    failures = 0
    total = 0
    for attempt in range(1, args.repeat + 1):
        if args.repeat > 1:
            print(f"\n=== {attempt} 回目 ===")
        for question in questions:
            # 問ごとに新しい Agent を作る＝前の問の文脈を持ち越さない。
            # 当日も 1 問ずつ独立したスレッドで流す想定
            agent = build_agent(stream=False)
            total += 1
            if not run_once(agent, question, args.show):
                failures += 1

    print(f"\n{total - failures}/{total} PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
