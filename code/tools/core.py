"""ツールの実体。

architecture_v1.md §6 のツール I/F をここに置く。この 2 関数のシグネチャと戻り値の形は
L0（ローカル）から L3（Gateway 経由の MCP）まで一度も変えない。段が上がるときに
差し替わるのは:

- `search_project_knowledge` の中身（L1 で全件スキャン → Managed KB の Retrieve）
- この関数たちの**呼ばれ方**（プロセス内 @tool → Lambda + Gateway 経由 MCP）

の 2 つだけで、呼び出し側から見た契約は変わらない。

正本の読み口は `DocumentSource` に隠してある。L0 はローカルの `knowledge_base/` を読み、
S3 に seed したあとは環境変数で S3 に切り替えられる（`get_document` の S3 直読みは
L1 以降もこのまま使う。§6「doc_id で辿る」用途）。

単体実行:
    uv run python -m tools.core "マルチテナント 物理分離"
    uv run python -m tools.core --doc DEC-003a
"""

from __future__ import annotations

import os
import pathlib
import re
import threading
from dataclasses import dataclass

import yaml

# --------------------------------------------------------------------------
# ドキュメント
# --------------------------------------------------------------------------

#: front matter のうち、検索結果に載せるキー
_META_KEYS = (
    "doc_id",
    "doc_type",
    "title",
    "date",
    "status",
    "supersedes",
    "superseded_by",
)


@dataclass(frozen=True)
class Document:
    """正本 1 ファイル。**索引のチャンクではなく、ファイル 1 つがそのまま 1 件。**

    Managed KB は既定チャンキングで 1 ファイルを複数エントリに割って返すが、
    `body` は索引からではなく正本から取り直すので、ここは常に全文（§4.4）。
    """

    doc_id: str
    doc_type: str
    title: str
    date: str
    status: str
    supersedes: str | None
    superseded_by: str | None
    body: str
    uri: str

    def as_search_hit(self) -> dict:
        """§6 の `search_project_knowledge` の戻り値の形。"""
        return {
            "doc_id": self.doc_id,
            "doc_type": self.doc_type,
            "title": self.title,
            "date": self.date,
            "status": self.status,
            "supersedes": self.supersedes,
            "body": self.body,
            "s3_uri": self.uri,
        }

    def as_document(self) -> dict:
        """§6 の `get_document` の戻り値の形。"""
        return {
            "doc_id": self.doc_id,
            "doc_type": self.doc_type,
            "title": self.title,
            "date": self.date,
            "status": self.status,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "body": self.body,
            "s3_uri": self.uri,
        }


def split_front_matter(text: str, uri: str) -> tuple[dict, str]:
    """front matter 付き markdown を (メタデータ, 本文) に割る。

    `Document` に載せないキー（topic など）も辞書のまま返すので、
    `.metadata.json` を作る側（seed_knowledge.py）もこれを使う。パースの規則を
    読み口と seed で二重に持たないための共通点。
    """
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError(f"front matter が無い: {uri}")

    meta = yaml.safe_load(m.group(1)) or {}
    missing = [k for k in ("doc_id", "doc_type", "title", "date", "status") if not meta.get(k)]
    if missing:
        raise ValueError(f"front matter に {', '.join(missing)} が無い: {uri}")

    return meta, m.group(2).strip()


def parse_document(text: str, uri: str) -> Document:
    """front matter 付き markdown を Document にする。

    front matter が無い、または必須キーを欠くファイルは正本として扱えないので弾く。
    """
    meta, body = split_front_matter(text, uri)

    return Document(
        doc_id=str(meta["doc_id"]),
        doc_type=str(meta["doc_type"]),
        title=str(meta["title"]),
        date=str(meta["date"]),
        status=str(meta["status"]),
        supersedes=_opt(meta.get("supersedes")),
        superseded_by=_opt(meta.get("superseded_by")),
        body=body,
        uri=uri,
    )


def _opt(value) -> str | None:
    """front matter の `null` / 空文字を None に寄せる。"""
    if value is None:
        return None
    text = str(value).strip()
    return None if text in ("", "null", "None") else text


# --------------------------------------------------------------------------
# 正本の読み口
# --------------------------------------------------------------------------


class DocumentSource:
    """正本の置き場。L0 はローカル、seed 後は S3。"""

    def load_all(self) -> list[Document]:
        raise NotImplementedError


class LocalSource(DocumentSource):
    """リポジトリ内の `knowledge_base/` を読む（L0 の既定）。"""

    def __init__(self, root: pathlib.Path):
        self.root = root

    def load_all(self) -> list[Document]:
        docs = []
        for path in sorted(self.root.rglob("*.md")):
            if path.name == "README.md":
                continue
            docs.append(
                parse_document(path.read_text(encoding="utf-8"), f"file://{path}")
            )
        return docs


class S3Source(DocumentSource):
    """S3 の `knowledge_base/` prefix を読む。

    §5.1 のとおり KB のデータソースはこの prefix だけ。`raw/` は取り込まない。
    """

    def __init__(self, bucket: str, prefix: str = "knowledge_base/"):
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/"

    def load_all(self) -> list[Document]:
        import boto3

        s3 = boto3.client("s3")
        docs = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".md") or key.endswith("README.md"):
                    continue
                text = (
                    s3.get_object(Bucket=self.bucket, Key=key)["Body"]
                    .read()
                    .decode("utf-8")
                )
                docs.append(parse_document(text, f"s3://{self.bucket}/{key}"))
        return sorted(docs, key=lambda d: d.doc_id)


def _default_root() -> pathlib.Path:
    """リポジトリ直下の knowledge_base/。"""
    return pathlib.Path(__file__).resolve().parents[2] / "knowledge_base"


#: 正本を置く CloudFormation スタック。バケット名の解決に使う
STACK_NAME = "KaiKnowledgeStack"


def resolve_bucket() -> str:
    """バケット名を環境変数か CloudFormation の出力から決める。

    バケット名にはアカウント ID が入る。**このリポジトリは public なので設定ファイルに
    literal で書かない**。`KAI_KNOWLEDGE_BUCKET` が無ければスタックの出力から引く
    （Runtime の実行ロールに `cloudformation:DescribeStacks` が1つ増えるだけ）。
    """
    if bucket := os.environ.get("KAI_KNOWLEDGE_BUCKET"):
        return bucket

    import boto3

    cfn = boto3.client(
        "cloudformation", region_name=os.environ.get("AWS_REGION", "ap-northeast-1")
    )
    stacks = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"]
    for output in stacks[0].get("Outputs", []):
        if output["OutputKey"] == "KnowledgeBucketName":
            return output["OutputValue"]
    raise RuntimeError(
        f"{STACK_NAME} に KnowledgeBucketName の出力が無い。"
        "KAI_KNOWLEDGE_BUCKET を明示するか、先に cdk deploy する"
    )


def build_source() -> DocumentSource:
    """環境変数から読み口を決める。既定はローカル。

    KAI_KNOWLEDGE_SOURCE = local | s3
    KAI_KNOWLEDGE_ROOT   = local のときの knowledge_base/ のパス
    KAI_KNOWLEDGE_BUCKET = s3 のときのバケット名（省略時はスタックの出力から引く）
    KAI_KNOWLEDGE_PREFIX = s3 のときの prefix（既定 knowledge_base/）
    """
    kind = os.environ.get("KAI_KNOWLEDGE_SOURCE", "local").lower()
    if kind == "s3":
        return S3Source(
            resolve_bucket(), os.environ.get("KAI_KNOWLEDGE_PREFIX", "knowledge_base/")
        )
    if kind == "local":
        root = os.environ.get("KAI_KNOWLEDGE_ROOT")
        return LocalSource(pathlib.Path(root) if root else _default_root())
    raise RuntimeError(f"KAI_KNOWLEDGE_SOURCE が不正: {kind}")


#: 正本のキャッシュ。ロックで守るのは、**Strands がツールを並列に呼ぶ**ため。
#: lru_cache だと同時にミスした分だけ実体が走り、L1d のトレースでは
#: 3 並列の search が 14 ファイル × 3 = 42 回の S3.GetObject を発生させていた。
_CACHE: tuple[Document, ...] | None = None
_CACHE_LOCK = threading.Lock()


def _load_all() -> tuple[Document, ...]:
    global _CACHE
    if _CACHE is None:
        with _CACHE_LOCK:
            # ロック待ちの間に他のスレッドが埋めている可能性があるので、もう一度見る
            if _CACHE is None:
                _CACHE = tuple(build_source().load_all())
    return _CACHE


def reset_cache() -> None:
    """データを書き換えた直後に読み直したいとき用。"""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


# --------------------------------------------------------------------------
# 検索（L0: 全件スキャン / L1: Managed KB の Retrieve に差し替え）
# --------------------------------------------------------------------------

_ASCII = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_CJK = re.compile(r"[ぁ-んァ-ヴ一-龠]+")


def _tokens(text: str) -> set[str]:
    """日本語混じりの素朴なトークン化。

    KB のハイブリッド検索（L1）が担う「固有名詞・doc_id の引き当て」を、L0 では
    ASCII 語 + 日本語の文字バイグラムで近似する。14 ファイルしか無いので全件スキャンで足りる。
    """
    lower = text.lower()
    tokens = set(_ASCII.findall(lower))
    for run in _CJK.findall(lower):
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            tokens.add(run)
    return tokens


def _score(doc: Document, query: str) -> float:
    q = _tokens(query)
    if not q:
        return 0.0

    title_hits = len(q & _tokens(doc.title))
    body_hits = len(q & _tokens(doc.body))
    score = title_hits * 3.0 + body_hits

    # doc_id をそのまま聞かれたケースは検索順位に依存させたくないので強く効かせる
    if doc.doc_id.lower() in query.lower():
        score += 50.0

    return score / (1 + len(q)) if score else 0.0


def _search_local(
    query: str, doc_type: str | None, status: str | None, limit: int
) -> list[dict]:
    """L0 の全件スキャン。AWS に触らないので、テストとオフラインの開発はこちら。"""
    docs = _load_all()
    if doc_type:
        docs = tuple(d for d in docs if d.doc_type == doc_type)
    if status:
        docs = tuple(d for d in docs if d.status == status)

    scored = [(s, d) for d in docs if (s := _score(d, query)) > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1].doc_id))
    return [d.as_search_hit() for _, d in scored[:limit]]


# --------------------------------------------------------------------------
# 検索（L1c: Managed KB の Retrieve）
# --------------------------------------------------------------------------


def merge_kb_results(results: list[dict]) -> list[tuple[str, float]]:
    """Retrieve の結果を doc_id ごとに束ね、(doc_id, スコア) を降順で返す。

    Managed KB は同じドキュメントについて「本文中の抜粋」と「front matter 込みの全文」
    を**別エントリ**で返す（smart parsing の副産物。スコアも別々に付く）。束ねないと
    エージェントが同じ doc を2回見ることになり、`numberOfResults` の意味も狂う。

    これは hierarchical chunking が「子チャンクを引いて親チャンクに差し替える」のと
    同じ考え方を手でやっている。本来は chunkingStrategy で任せたいところだが、
    `embeddingModelType: MANAGED` とは併用できないので（§10-11）自前で持つ。

    スコアは**最大値**を採る。抜粋の方が高スコアなのは「クエリに近い箇所がそこにある」
    という情報なので、順位付けには活かす。
    """
    best: dict[str, float] = {}
    for result in results:
        doc_id = (result.get("metadata") or {}).get("doc_id")
        if not doc_id:
            continue
        score = float(result.get("score") or 0.0)
        doc_id = str(doc_id)
        if score > best.get(doc_id, -1.0):
            best[doc_id] = score
    return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))


def _search_kb(
    query: str, doc_type: str | None, status: str | None, limit: int
) -> list[dict]:
    """Managed KB の Retrieve で当てて、本文は正本から取り直す。

    索引は「どの doc か」を決めるためだけに使い、`body` は `_load_all()`（＝正本）
    から引く。索引が抜粋しか返さなくても全文が渡り、`status` と `supersedes` も必ず
    入る。「Git が正本、S3 は配信先、索引は索引」（§5.2）をコードでもそのまま守る。
    """
    import boto3

    kb_id = os.environ.get("KAI_KB_ID")
    if not kb_id:
        raise RuntimeError(
            "KAI_SEARCH=kb のときは KAI_KB_ID が要る。"
            "seed_knowledge.py の出力か、CloudFormation の KaiKnowledgeStack から取る"
        )

    # Managed KB は vectorSearchConfiguration を受け付けない（§10-10）
    config: dict = {"numberOfResults": max(limit * 3, 12)}

    conditions = []
    if doc_type:
        conditions.append({"equals": {"key": "doc_type", "value": doc_type}})
    if status:
        conditions.append({"equals": {"key": "status", "value": status}})
    if len(conditions) == 1:
        config["filter"] = conditions[0]
    elif conditions:
        config["filter"] = {"andAll": conditions}

    # マネージド reranker。MANAGED 埋め込みを選んだ理由がこれなので既定で使う
    if os.environ.get("KAI_RERANK", "managed").lower() != "none":
        config["rerankingModelType"] = "MANAGED"

    client = boto3.client(
        "bedrock-agent-runtime",
        region_name=os.environ.get("AWS_REGION", "ap-northeast-1"),
    )
    response = client.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={"managedSearchConfiguration": config},
    )

    by_id = {doc.doc_id: doc for doc in _load_all()}
    hits = []
    for doc_id, _score_value in merge_kb_results(response.get("retrievalResults", [])):
        doc = by_id.get(doc_id)
        if doc is None:
            # 索引にあって正本に無い＝seed 漏れか消し忘れ。黙って捨てる方が安全
            continue
        # KB 側でも絞っているが、契約は呼び出し側との約束なのでここでも担保する
        if doc_type and doc.doc_type != doc_type:
            continue
        if status and doc.status != status:
            continue
        hits.append(doc.as_search_hit())
        if len(hits) >= limit:
            break
    return hits


def search_project_knowledge(
    query: str,
    doc_type: str | None = None,
    status: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """案件の decision / knowledge / meeting を検索する。

    中身は `KAI_SEARCH` で切り替わる（既定 `local`）。**呼び出し側から見た形は
    L0 から変わらない**ので、`local_tools.py` もプロンプトも無傷のまま。

    Args:
        query: 検索クエリ（自然文でよい）
        doc_type: decision | knowledge | meeting に絞る。省略時は絞らない
        status: active | superseded | proposed | rejected に絞る。省略時は絞らない
        limit: 返す件数。既定 5

    Returns:
        ヒットしたドキュメントのリスト。本文全文（body）を含む。
        該当が無ければ空リスト（呼び出し側に「根拠なし」と言わせるため）。
    """
    kind = os.environ.get("KAI_SEARCH", "local").lower()
    if kind == "kb":
        return _search_kb(query, doc_type, status, limit)
    if kind == "local":
        return _search_local(query, doc_type, status, limit)
    raise RuntimeError(f"KAI_SEARCH が不正: {kind}")


def get_document(doc_id: str) -> dict:
    """doc_id を指定して 1 件取る。

    検索ではなく ID 参照。`supersedes` / `superseded_by` を辿るときはこちらを使う。

    Returns:
        ドキュメント 1 件。見つからなければ `{"doc_id": ..., "found": False}`。
    """
    for doc in _load_all():
        if doc.doc_id.lower() == doc_id.strip().lower():
            return doc.as_document()
    return {"doc_id": doc_id, "found": False}


# --------------------------------------------------------------------------
# 単体実行（AWS に上げる前にツールだけで台本を検証する）
# --------------------------------------------------------------------------


def _main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="ツール単体の動作確認")
    parser.add_argument("query", nargs="?", help="検索クエリ")
    parser.add_argument("--doc", help="doc_id を指定して 1 件取る")
    parser.add_argument("--doc-type", dest="doc_type")
    parser.add_argument("--status")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--full", action="store_true", help="本文も表示する")
    args = parser.parse_args()

    if args.doc:
        print(json.dumps(get_document(args.doc), ensure_ascii=False, indent=2))
        return

    if not args.query:
        parser.error("query か --doc のどちらかが要る")

    hits = search_project_knowledge(
        args.query, doc_type=args.doc_type, status=args.status, limit=args.limit
    )
    if not hits:
        print("(該当なし)")
        return
    for hit in hits:
        if not args.full:
            hit = {k: v for k, v in hit.items() if k != "body"}
        print(json.dumps(hit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
