"""KB の引き当てだけを確かめる（architecture_v1.md §9 の L1b の検証）。

**エージェントもツールも通さない。** Retrieve API を直接叩いて、台本4問のクエリに
期待する doc が返るかだけを見る。ここで弱ければ `.metadata.json` の
`includeForEmbedding` を直して KB を作り直す。

この段ではまだ `search_project_knowledge` を差し替えていないので、
デモ（`run_demo_script.py`）は S3 直読みのまま動き続けている。だから何度作り直しても
当日の準備は止まらない。

    uv run python code/scripts/check_retrieve.py
    uv run python code/scripts/check_retrieve.py --results 10 --show

クエリの文面は run_demo_script.py と揃えてあるが、**見ている観点が違う**ので表は
別に持つ。あちらは「エージェントが根拠に挙げたか」、こちらは「索引が返したか」。
"""

from __future__ import annotations

import argparse
import os
import sys

import boto3

STACK_NAME = "KaiKnowledgeStack"

QUERIES = [
    {
        "id": "Q1",
        "title": "矛盾検知（マルチテナント方式）",
        "query": (
            "設計書ドラフトのレビューお願いします。マルチテナント、"
            "大口テナント向けにテナントごとにDBを物理分離する方針で書きました。"
        ),
        # 現行の正本と、未昇格の検討メモの両方が返らないと矛盾を指摘できない
        "expect": ["DEC-004", "MTG-2026-03-15"],
    },
    {
        "id": "Q2",
        "title": "superseded（非同期処理基盤）",
        "query": "非同期処理は Step Functions で組む予定です。",
        # 新版が返れば、旧版は get_document で supersedes を辿れる。
        # ただし検索でも両方返るのが理想
        "expect": ["DEC-003b"],
        "want": ["DEC-003a"],
    },
    {
        "id": "Q3",
        "title": "暗黙知（外部連携）",
        "query": "PartnerSync連携、まずは即時リトライのシンプル実装でいきます。",
        # 固有名詞の引き当て。ハイブリッド検索のキーワード側が効くところ
        "expect": ["KNW-002"],
    },
    {
        "id": "Q4",
        "title": "根拠なし（監視ツール）",
        "query": "監視は Datadog を入れる案、どう思う？",
        # 監視ツールの decision は存在しない。何が返ってもよいが、
        # 存在しない doc をでっち上げないことだけ確認する
        "expect": [],
    },
]


def resolve_kb_id(region: str) -> str:
    if kb_id := os.environ.get("KAI_KB_ID"):
        return kb_id
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        stacks = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"]
    except cfn.exceptions.ClientError as exc:
        raise SystemExit(f"{STACK_NAME} が見つからない: {exc}") from exc
    for output in stacks[0].get("Outputs", []):
        if output["OutputKey"] == "KnowledgeBaseId":
            return output["OutputValue"]
    raise SystemExit(f"{STACK_NAME} に KnowledgeBaseId の出力が無い")


def doc_id_of(result: dict) -> str:
    """Retrieve の結果から doc_id を取り出す。

    `.metadata.json` で付けた属性がここに乗ってくる。乗っていなければ
    メタデータの設計かファイルの置き場所が間違っている。
    """
    metadata = result.get("metadata", {})
    if doc_id := metadata.get("doc_id"):
        return str(doc_id)
    # メタデータが乗っていない場合は S3 のキーから推測して、原因を分かるようにする
    uri = result.get("location", {}).get("s3Location", {}).get("uri", "?")
    return f"(metadata なし: {uri.rsplit('/', 1)[-1]})"


def main() -> None:
    parser = argparse.ArgumentParser(description="KB の引き当てを確かめる")
    parser.add_argument("--results", type=int, default=5, help="1クエリあたりの取得件数")
    parser.add_argument("--show", action="store_true", help="本文の先頭も表示する")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-northeast-1"))
    args = parser.parse_args()

    kb_id = resolve_kb_id(args.region)
    runtime = boto3.client("bedrock-agent-runtime", region_name=args.region)
    print(f"Knowledge Base: {kb_id}\n")

    failures = 0
    for q in QUERIES:
        response = runtime.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": q["query"]},
            # Managed KB は vectorSearchConfiguration を受け付けない。
            # 渡すと「vectorSearchConfiguration is not supported for managed
            # knowledge bases. Use managedSearchConfiguration instead」で落ちる。
            # L1c で search を差し替えるときも同じ形になる。
            retrievalConfiguration={
                "managedSearchConfiguration": {"numberOfResults": args.results}
            },
        )
        results = response.get("retrievalResults", [])
        returned = [doc_id_of(r) for r in results]

        missing = [d for d in q["expect"] if d not in returned]
        ok = not missing
        failures += 0 if ok else 1

        print(f"  [{'PASS' if ok else 'FAIL'}] {q['id']} {q['title']}")
        print(f"        返却({len(returned)}件): {', '.join(returned) if returned else '(なし)'}")
        for doc_id in missing:
            print(f"        ! {doc_id} が返っていない")
        for doc_id in q.get("want", []):
            if doc_id not in returned:
                print(f"        - {doc_id} は返っていない（get_document で辿れるので致命ではない）")
        if args.show:
            for r in results:
                text = r.get("content", {}).get("text", "").replace("\n", " ")
                print(f"        · {doc_id_of(r)} score={r.get('score', 0):.3f} {text[:70]}…")
        print()

    print(f"{len(QUERIES) - failures}/{len(QUERIES)} PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
