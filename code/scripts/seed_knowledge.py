"""正本を S3 に送り込む（architecture_v1.md §5.2）。

経路は「人 → Git（PR レビュー）→ マージ → CI が S3 に sync → KB の Ingestion」。
その CI が .github/workflows/seed-knowledge.yml で、**この同じスクリプト**を実行する
（CI 用の別経路は作らない）。手で流すのは、CI を待たずに試したいときと、
正本を触らずに索引だけ作り直したいとき。**Git が正本、S3 は配信先**という向きは
どちらでも変えない。S3 を直接編集することはない。

やること:

1. `knowledge_base/**/*.md` を読んで front matter を検証する
2. 各ファイルの `.metadata.json`（§4.4 のメタデータフィルタ用）を作る
3. S3 に同期する。ローカルに無くなったものは S3 からも消す
4. Ingestion を起こす。**削除があった回は、索引からも消えたことを確かめる**

`raw/` prefix（提供された Excel / PPT の原本置き場）には一切触らない。

    uv run python code/scripts/seed_knowledge.py --dry-run
    uv run python code/scripts/seed_knowledge.py

バケット名は KAI_KNOWLEDGE_BUCKET、無ければ CloudFormation スタックの出力から引く。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

import boto3

from tools import core

#: KB のデータソースになる prefix。ここより下だけを同期の対象にする
PREFIX = "knowledge_base/"

#: 埋め込みにも載せるキー（§4.4）。
#: 「決定事項では」「議事録では」のような問いのスコアが上がる
EMBED = ("doc_id", "doc_type", "status")

#: フィルタにだけ使うキー。埋め込みには載せない
FILTER_ONLY = ("date", "topic", "supersedes", "superseded_by")

STACK_NAME = "KaiKnowledgeStack"


def stack_outputs(region: str) -> dict[str, str]:
    """CloudFormation スタックの出力を辞書で返す。

    バケット名や KB の ID を手でコピペしなくて済むようにするための逃げ道。
    """
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        stacks = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"]
    except cfn.exceptions.ClientError as exc:
        raise SystemExit(
            f"{STACK_NAME} が見つからない。先に cdk deploy するか、"
            f"KAI_KNOWLEDGE_BUCKET などを環境変数で渡す: {exc}"
        ) from exc
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


def run_ingestion(region: str, kb_id: str, ds_id: str) -> None:
    """Ingestion job を起こして終わるまで待つ（§5.2 の CI の後半）。

    S3 に置いただけでは KB は新しい内容を知らない。ここまでやって初めて
    「正本 → 索引」が繋がる。
    """
    agent = boto3.client("bedrock-agent", region_name=region)
    job = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
    job_id = job["ingestionJob"]["ingestionJobId"]
    print(f"Ingestion job {job_id} を起動した。完了まで待つ…")

    while True:
        current = agent.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
        )["ingestionJob"]
        status = current["status"]
        if status in ("COMPLETE", "FAILED", "STOPPED"):
            break
        time.sleep(5)

    stats = current.get("statistics", {})
    print(f"Ingestion: {status}")
    if stats:
        print(
            f"  スキャン {stats.get('numberOfDocumentsScanned', '?')} / "
            f"取り込み {stats.get('numberOfNewDocumentsIndexed', '?')} 新規, "
            f"{stats.get('numberOfModifiedDocumentsIndexed', '?')} 更新, "
            f"{stats.get('numberOfDocumentsDeleted', '?')} 削除, "
            f"{stats.get('numberOfDocumentsFailed', '?')} 失敗"
        )
    if status != "COMPLETE":
        for reason in current.get("failureReasons", []):
            print(f"  ! {reason}")
        raise SystemExit(1)


def deleted_doc_ids(stale: set[str]) -> list[str]:
    """S3 から消したキーのうち、doc として消えたものの doc_id を拾う。

    `.metadata.json` は本体と対で消えるので数えない。doc_id はファイル名と
    一致させる規則（knowledge_base/README.md）なので stem をそのまま使える。
    """
    return sorted(pathlib.Path(key).stem for key in stale if key.endswith(".md"))


def still_retrievable(region: str, kb_id: str, doc_ids: list[str]) -> list[str]:
    """消したはずの doc がまだ索引に残っていないか、doc_id で直接引いて確かめる。

    Managed KB のフィルタは `equals` / `in` しか使えない（§10-6）。曖昧なクエリで
    「それらしいものが返らないこと」を見るのではなく、doc_id の完全一致で引いて
    **0 件であること**を見る。こうしないと「たまたま上位に来なかった」と区別できない。
    """
    runtime = boto3.client("bedrock-agent-runtime", region_name=region)
    remaining = []
    for doc_id in doc_ids:
        response = runtime.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": doc_id},
            retrievalConfiguration={
                "managedSearchConfiguration": {
                    "numberOfResults": 5,
                    "filter": {"equals": {"key": "doc_id", "value": doc_id}},
                }
            },
        )
        if response.get("retrievalResults"):
            remaining.append(doc_id)
    return remaining


def verify_deletions(region: str, kb_id: str, ds_id: str, doc_ids: list[str]) -> None:
    """消した doc が索引からも消えたことを確かめ、残っていたら取り込み直す。

    **Ingestion job が「削除 0 件」で COMPLETE したのに索引には残っていた、という
    取りこぼしを一度観測している。** 同じ手順でも再現せず、条件は特定できていない
    （ジョブ間隔・環境のどちらも説明にならなかった）。AWS のドキュメントは
    「削除はその sync で反映される」としか書いておらず、この挙動の記述は無い。

    原因を当てにいくより、消えたことを毎回確かめる。放っておくと
    **正本から消えた doc がエージェントの検索で返り続ける**ので、静かに間違える。

    **回数ではなく状態で判断する。** 「削除があったら 2 回回しておく」だと、2 回目でも
    消えなかったときに気づけない（2 回で消えるという保証は、観測 1 例からは言えない）。
    ここでやるのは次の 3 つで、Ingestion は 1 回で消えていれば 1 回きり:

        確認 → 消えていれば終わり
             → 残っていれば取り込み直して、もう一度確認
             → それでも残っていれば落とす（原因不明の異常なので人に見せる）

    2 回目を試すのは修復の試みであって、回したことを成功の根拠にはしない。
    黙って回し続けない（無限リトライにしない）のも同じ理由。
    """
    if not doc_ids:
        return

    print()
    print(f"削除の反映を確認する: {', '.join(doc_ids)}")
    remaining = still_retrievable(region, kb_id, doc_ids)
    if not remaining:
        print("  索引からも消えている")
        return

    print(f"  ! まだ引ける: {', '.join(remaining)}。もう一度 Ingestion する")
    run_ingestion(region, kb_id, ds_id)
    remaining = still_retrievable(region, kb_id, doc_ids)
    if remaining:
        raise SystemExit(
            f"消したはずの {', '.join(remaining)} が 2 回の Ingestion 後も索引に残っている。"
            "正本から消えた doc がエージェントの検索で返る状態なので、手で確認すること"
        )
    print("  2 回目の Ingestion で索引からも消えた")


def build_metadata(meta: dict) -> dict:
    """front matter から `.metadata.json` を作る。

    値が空のキーは落とす。Managed KB のフィルタは `equals` / `in` しか使えないので
    （§10-6）、全部フラットな STRING で持たせる。
    """
    attributes = {}
    for key in (*EMBED, *FILTER_ONLY):
        value = meta.get(key)
        if value is None or str(value).strip() in ("", "null", "None"):
            continue
        attributes[key] = {
            "value": {"type": "STRING", "stringValue": str(value)},
            "includeForEmbedding": key in EMBED,
        }
    return {"metadataAttributes": attributes}


def collect(root: pathlib.Path) -> dict[str, bytes]:
    """S3 に置くべきキーと中身を組み立てる。front matter が壊れていればここで落ちる。"""
    payload: dict[str, bytes] = {}

    for path in sorted(root.rglob("*.md")):
        if path.name == "README.md":
            continue

        text = path.read_text(encoding="utf-8")
        # 検証は core と同じ規則で行う。ここを通らないものは S3 に送らない
        core.parse_document(text, str(path))
        meta, _ = core.split_front_matter(text, str(path))

        key = f"{PREFIX}{path.relative_to(root).as_posix()}"
        payload[key] = text.encode("utf-8")
        payload[f"{key}.metadata.json"] = json.dumps(
            build_metadata(meta), ensure_ascii=False, indent=2
        ).encode("utf-8")

    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="正本を S3 に同期し、KB に取り込む")
    parser.add_argument("--dry-run", action="store_true", help="送らずに内容を出す")
    parser.add_argument("--no-ingest", action="store_true", help="S3 同期だけで Ingestion しない")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-northeast-1"))
    args = parser.parse_args()

    root = pathlib.Path(__file__).resolve().parents[2] / "knowledge_base"
    payload = collect(root)
    docs = sum(1 for k in payload if k.endswith(".md"))
    print(f"{root} から {docs} 件（{len(payload)} オブジェクト）")

    if args.dry_run:
        for key in sorted(payload):
            print(f"  {key}")
        print("(--dry-run のため送らない)")
        return

    outputs = stack_outputs(args.region)
    bucket = os.environ.get("KAI_KNOWLEDGE_BUCKET") or outputs["KnowledgeBucketName"]
    s3 = boto3.client("s3", region_name=args.region)

    # 今 S3 にあるものを控えておき、ローカルに無いものは後で消す
    existing = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX):
        existing.update(obj["Key"] for obj in page.get("Contents", []))

    for key, body in sorted(payload.items()):
        content_type = (
            "application/json" if key.endswith(".json") else "text/markdown; charset=utf-8"
        )
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    print(f"送信: {len(payload)} オブジェクト → s3://{bucket}/{PREFIX}")

    stale = existing - payload.keys()
    if stale:
        s3.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": k} for k in sorted(stale)]}
        )
        print(f"削除（ローカルに無い）: {len(stale)} オブジェクト")
        for key in sorted(stale):
            print(f"  - {key}")

    kb_id = os.environ.get("KAI_KB_ID") or outputs.get("KnowledgeBaseId")
    ds_id = os.environ.get("KAI_DATA_SOURCE_ID") or outputs.get("DataSourceId")

    if args.no_ingest:
        print("(--no-ingest のため Ingestion しない)")
    elif kb_id and ds_id:
        print()
        run_ingestion(args.region, kb_id, ds_id)
        # 削除があった回だけ、索引からも消えたことを確かめる（取りこぼし対策）
        verify_deletions(args.region, kb_id, ds_id, deleted_doc_ids(stale))
    else:
        print("(KB がまだ無いので Ingestion しない)")

    print()
    print("読み口を S3 に向けるには:")
    print("  export KAI_KNOWLEDGE_SOURCE=s3")
    print(f"  export KAI_KNOWLEDGE_BUCKET={bucket}")
    if kb_id:
        print("検索そのものを KB に向けるには:")
        print("  export KAI_SEARCH=kb")
        print(f"  export KAI_KB_ID={kb_id}")


if __name__ == "__main__":
    sys.exit(main())
