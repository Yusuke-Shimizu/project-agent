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
import os
import re
import sys

from runtime.agent import build_agent

#: doc_id の書式
DOC_ID = re.compile(r"\b(?:DEC-\d+[ab]?|KNW-\d+|MTG-\d{4}-\d{2}-\d{2})\b")

#: 「正本に該当の記録が無い」と明言できているかの判定に使う言い回し。
#: 同じ意味を複数の言い方でするので、機械判定はここを取りこぼしやすい。
#: 「見当たらない」を落として一度 FAIL を出したので足した（意味は同じ）。
ABSENCE = re.compile(
    r"見つか(らな|りませ)|見当たら(な|ませ)|存在し(ない|ませ)|記録は(無|な)い"
)

#: 出力契約（§7）が崩れていないかの検証。**doc_id の含有では測れない**ので別に見る。
#: L2 で「全段 PASS なのに人の目に触れる場所で壊れていた」を踏んで入れた。
#:
#: **2026-08-10 に見るものを変えた。** 以前は 【指摘】【根拠】【確認してほしいこと】 の
#: ラベルの有無を見ていたが、**ラベル自体をやめた**（人は Slack でそう書かない）。
#: 代わりに契約の中身を見る ―― 根拠の書式・引用・人に返しているか。
#: 形だけ整っていても中身が無ければ意味がないので、こちらのほうが物差しとして正しい。

#: 根拠は `doc_id（日付, status）` の形で出す（§7）。日付と status が無いと
#: 「いつの・現行かどうか」が読めない
EVIDENCE = re.compile(r"（\d{4}-\d{2}-\d{2},\s*(active|superseded|proposed|rejected)）")

#: 原文の引用（行頭の `>`）。**読んだ証拠**であって、doc_id を挙げただけでは足りない
QUOTE = re.compile(r"^\s*>", re.M)

#: 判断を人に返しているか。断定して終わらせていないこと。
#:
#: **最後の行だけを見る。** 出力契約は「最後に、人に決めてほしいことを 1 つ聞く」なので、
#: 途中に疑問文があっても締めが断定なら契約は守れていない。
#:
#: 語尾の列挙で判定しようとして一度失敗した（「…問題ないですか？」を拾えず Q2 が
#: 2 回 FAIL した）。**答えは正しく聞いていたのに判定が落としていた**ので、
#: 疑問符と依頼表現を広く取る形にしてある
ASK = re.compile(r"[?？]|ますか|ください|ほしい|どちら|でしょうか|決め")


def closing_line(answer: str) -> str:
    """最後の空でない行。ここで人に返しているかを見る。"""
    lines = [line.strip() for line in answer.strip().split("\n") if line.strip()]
    return lines[-1] if lines else ""

DEMO_SCRIPT = [
    {
        "id": "Q1",
        "title": "矛盾検知・非断定（マルチテナント方式）",
        # **レビューを頼むなら、レビュー対象が要る。** 当初は「物理分離する方針で
        # 書きました」という一行だけだったが、それでは設計書が存在しない。
        # Slack に抜粋を貼るのは実際にやる行為でもあるので、そのままの形にした
        "prompt": (
            "設計書ドラフトのレビューお願いします。\n\n"
            "## 4. データ分離方式\n"
            "本システムはマルチテナント構成とする。大口テナント（想定5社）については、"
            "性能要件とデータ保全要件を満たすため、テナントごとに専用DBを割り当てる"
            "物理分離（サイロ方式）を採用する。中小テナントは共有DBに収容する。"
        ),
        # DEC-004（active）と MTG-2026-03-15（proposed）の両方を出せて初めて
        # 「検討記録はあるが正式決定ではない」が言える
        "expect": ["DEC-004", "MTG-2026-03-15"],
    },
    {
        "id": "Q2",
        "title": "superseded・版管理（非同期処理基盤）",
        "prompt": "非同期処理は Step Functions で組む予定です。この認識で合ってますか？",
        # 旧版と新版の両方。片方だけなら「置換された」が言えていない
        "expect": ["DEC-003a", "DEC-003b"],
    },
    {
        "id": "Q3",
        "title": "暗黙知の想起（外部連携）",
        "prompt": "PartnerSync連携、まずは即時リトライのシンプル実装でいきます。注意点あれば教えてください。",
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

    # 本文ではなく `message` の dict がそのまま返っていないか。
    #
    # **L1d ではこれを見逃して 12/12 PASS を出していた。** doc_id は dict の中にも
    # 現れるので、下の含有判定だけでは通ってしまう。Slack に繋いで初めて
    # 「文章ではなく dict が貼られる」と分かった（§10-9）。物差しの側を直しておく。
    if answer.lstrip().startswith("{") and "'content'" in answer:
        problems.append("本文ではなく message の dict がそのまま返っている")

    # 出力契約が崩れていないか。差し替えのたびに「答えの中身」は見てきたが、
    # **「答えの形」を見ていなかった**
    # **引くべき doc がある問だけ**根拠の形を要求する。
    # 予備問（監視ツール）は「記録が無い」と答えるのが正解なので、
    # 引用も doc_id も出ない ―― そこに根拠を求めると、無いものを挙げさせる判定になる
    if question["expect"]:
        if not EVIDENCE.search(answer):
            problems.append("根拠が doc_id（日付, status）の形で出ていない")
        if not QUOTE.search(answer):
            problems.append("原文の引用が無い")

    # これは全問に要る。断定して終わらせず、判断を人に返しているか
    if not ASK.search(closing_line(answer)):
        problems.append("最後に人へ返していない（締めが断定になっている）")

    for doc_id in question["expect"]:
        if doc_id not in answer:
            problems.append(f"{doc_id} を挙げていない")

    if question.get("require_absence") and not ABSENCE.search(answer):
        problems.append("「正本に該当の記録が無い」と明言していない")

    return not problems, problems


def invoke_runtime(runtime_arn: str, prompt: str, region: str) -> str:
    """AgentCore Runtime を叩いて本文だけ取り出す（L1d）。

    ローカルの Agent と入れ替えても判定ロジックは同じものを使う。**同じ物差しで
    測れないと「デプロイして答えが変わったか」が分からない**ため。
    """
    import json
    import uuid

    import boto3

    client = boto3.client("bedrock-agentcore", region_name=region)
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        # runtimeSessionId は 33 文字以上でないと ValidationException（§10-1）。
        # 問ごとに別セッションにして、前の問の文脈を持ち越さない
        runtimeSessionId=str(uuid.uuid4()),
        payload=json.dumps({"prompt": prompt}).encode("utf-8"),
    )
    body = json.loads(response["response"].read().decode("utf-8"))
    if "error" in body:
        raise RuntimeError(body["error"])
    return str(body.get("result", ""))


def resolve_runtime_arn(region: str) -> str:
    import os

    if arn := os.environ.get("KAI_RUNTIME_ARN"):
        return arn
    import boto3

    cfn = boto3.client("cloudformation", region_name=region)
    stacks = cfn.describe_stacks(StackName="AgentCore-kaiContextAgent-personal")["Stacks"]
    for output in stacks[0].get("Outputs", []):
        if "RuntimeArn" in output["OutputKey"]:
            return output["OutputValue"]
    raise SystemExit("Runtime の ARN が見つからない。KAI_RUNTIME_ARN を設定する")


def run_once(agent, question: dict, show: bool) -> bool:
    # 呼び出しそのものが落ちても**台本を止めない**。
    # Bedrock の `ConverseStream` が `InternalServerException` を返すことがあり、
    # 1 問の失敗で残りが測れなくなると「どのくらいの頻度で起きるか」が分からない。
    try:
        if isinstance(agent, tuple):  # ("runtime", arn, region)
            answer = invoke_runtime(agent[1], question["prompt"], agent[2])
        else:
            answer = answer_text(agent(question["prompt"]))
    except Exception as exc:
        print(f"  [FAIL] {question['id']} {question['title']}")
        print(f"        ! 呼び出しが失敗: {type(exc).__name__}: {str(exc)[:120]}")
        return False

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
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="ローカルではなく AgentCore Runtime を叩く（L1d の完了条件）",
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-northeast-1"))
    args = parser.parse_args()

    runtime_arn = resolve_runtime_arn(args.region) if args.runtime else None
    if runtime_arn:
        print(f"AgentCore Runtime: {runtime_arn.rsplit('/', 1)[-1]}\n")

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
            agent = (
                ("runtime", runtime_arn, args.region)
                if runtime_arn
                else build_agent(stream=False)
            )
            total += 1
            if not run_once(agent, question, args.show):
                failures += 1

    print(f"\n{total - failures}/{total} PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
