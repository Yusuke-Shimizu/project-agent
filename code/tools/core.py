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
from dataclasses import dataclass
from functools import lru_cache

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
    """正本 1 ファイル。`chunkingStrategy: NONE` なので 1 ファイル = 1 チャンク。"""

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


def parse_document(text: str, uri: str) -> Document:
    """front matter 付き markdown を Document にする。

    front matter が無い、または doc_id を欠くファイルは正本として扱えないので弾く。
    """
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError(f"front matter が無い: {uri}")

    meta = yaml.safe_load(m.group(1)) or {}
    missing = [k for k in ("doc_id", "doc_type", "title", "date", "status") if not meta.get(k)]
    if missing:
        raise ValueError(f"front matter に {', '.join(missing)} が無い: {uri}")

    return Document(
        doc_id=str(meta["doc_id"]),
        doc_type=str(meta["doc_type"]),
        title=str(meta["title"]),
        date=str(meta["date"]),
        status=str(meta["status"]),
        supersedes=_opt(meta.get("supersedes")),
        superseded_by=_opt(meta.get("superseded_by")),
        body=m.group(2).strip(),
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


def build_source() -> DocumentSource:
    """環境変数から読み口を決める。既定はローカル。

    KAI_KNOWLEDGE_SOURCE = local | s3
    KAI_KNOWLEDGE_ROOT   = local のときの knowledge_base/ のパス
    KAI_KNOWLEDGE_BUCKET = s3 のときのバケット名
    KAI_KNOWLEDGE_PREFIX = s3 のときの prefix（既定 knowledge_base/）
    """
    kind = os.environ.get("KAI_KNOWLEDGE_SOURCE", "local").lower()
    if kind == "s3":
        bucket = os.environ.get("KAI_KNOWLEDGE_BUCKET")
        if not bucket:
            raise RuntimeError(
                "KAI_KNOWLEDGE_SOURCE=s3 のときは KAI_KNOWLEDGE_BUCKET が要る"
            )
        return S3Source(bucket, os.environ.get("KAI_KNOWLEDGE_PREFIX", "knowledge_base/"))
    if kind == "local":
        root = os.environ.get("KAI_KNOWLEDGE_ROOT")
        return LocalSource(pathlib.Path(root) if root else _default_root())
    raise RuntimeError(f"KAI_KNOWLEDGE_SOURCE が不正: {kind}")


@lru_cache(maxsize=1)
def _load_all() -> tuple[Document, ...]:
    return tuple(build_source().load_all())


def reset_cache() -> None:
    """データを書き換えた直後に読み直したいとき用。"""
    _load_all.cache_clear()


# --------------------------------------------------------------------------
# 検索（L0: 全件スキャン / L1: Managed KB の Retrieve に差し替え）
# --------------------------------------------------------------------------

_ASCII = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_CJK = re.compile(r"[ぁ-んァ-ヴ一-龠]+")


def _tokens(text: str) -> set[str]:
    """日本語混じりの素朴なトークン化。

    KB のハイブリッド検索（L1）が担う「固有名詞・doc_id の引き当て」を、L0 では
    ASCII 語 + 日本語の文字バイグラムで近似する。13 ファイルしか無いので全件スキャンで足りる。
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


def search_project_knowledge(
    query: str,
    doc_type: str | None = None,
    status: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """案件の decision / knowledge / meeting を検索する。

    Args:
        query: 検索クエリ（自然文でよい）
        doc_type: decision | knowledge | meeting に絞る。省略時は絞らない
        status: active | superseded | proposed | rejected に絞る。省略時は絞らない
        limit: 返す件数。既定 5

    Returns:
        ヒットしたドキュメントのリスト。本文全文（body）を含む。
        該当が無ければ空リスト（呼び出し側に「根拠なし」と言わせるため）。
    """
    docs = _load_all()
    if doc_type:
        docs = tuple(d for d in docs if d.doc_type == doc_type)
    if status:
        docs = tuple(d for d in docs if d.status == status)

    scored = [(s, d) for d in docs if (s := _score(d, query)) > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1].doc_id))
    return [d.as_search_hit() for _, d in scored[:limit]]


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
