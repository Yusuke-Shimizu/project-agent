"""正本（`knowledge_base/`）の規約を検証する。

`seed_knowledge.py --dry-run` も front matter を検証するが、**あちらは自分のリポジトリの
`knowledge_base/` に固定**されている（`parents[2] / "knowledge_base"`）。正本を別リポジトリに
分けたので、**任意のディレクトリを渡せる入口**が要る。それがこのスクリプト。

規約の実体は `core.parse_document` をそのまま使う ―― **同じ規則を2か所に置かない**。
ここが足すのは、1 ファイルだけを見ていては分からない検査（doc_id の一意性、
`supersedes` の対称性、ファイル名との一致、`topic` の allowlist 照合）。

書き戻し PR ツールのゲートとしても使う。エージェントが出す PR も人が出す PR も
**同じこれを通る**（別経路を作らない）。

単体実行:
    uv run python code/scripts/validate_knowledge.py
    uv run python code/scripts/validate_knowledge.py \\
        --root ../kai-knowledge/knowledge_base --topics ../kai-knowledge/docs/topics.yml
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools import core  # noqa: E402

#: `doc_type` に使える値
DOC_TYPES = ("decision", "knowledge", "meeting")

#: `status` に使える値
STATUSES = ("active", "superseded", "proposed", "rejected")

#: `doc_type` ごとに使える `status`。
#:
#: **`proposed` は meeting だけ。** 「検討したが正式 decision に昇格していない」という
#: 文書のライフサイクルの語なので、knowledge や decision には付かない。起案は
#: `active` で出してマージが昇格、という決めごと（writeback_pr_tool.md §5）の裏返し。
#: **エージェントが knowledge に `proposed` を書いてきたのを実測で見た**ので機械で見る
STATUS_BY_TYPE = {
    "decision": ("active", "superseded", "rejected"),
    "knowledge": ("active", "superseded", "rejected"),
    "meeting": ("proposed",),
}

#: ディレクトリ名と `doc_type` の対応。**置き場所と型が食い違っていたら弾く**
DIR_TO_TYPE = {"decisions": "decision", "knowledge": "knowledge", "meetings": "meeting"}

#: `supersedes` / `superseded_by` / `decided_by` を持てる型
#: （`knowledge_base/README.md` の「decision のみ」）
VERSIONED_KEYS = ("supersedes", "superseded_by", "decided_by")


def load_topics(path: pathlib.Path | None) -> set[str] | None:
    """`topics.yml` を読む。渡されなければ `topic` の照合はしない。

    allowlist を**正本リポジトリ側に置く**のは、案件ごとに topic が違うから。
    増やすときは `topics.yml` も同じ PR で更新する ＝ 人の判断が挟まる。
    """
    if path is None:
        return None
    if not path.exists():
        raise SystemExit(f"topics のファイルが無い: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    topics = data.get("topics")
    if not isinstance(topics, list) or not topics:
        raise SystemExit(f"topics: の一覧が読めない: {path}")
    return {str(t) for t in topics}


def validate(root: pathlib.Path, topics: set[str] | None = None) -> list[str]:
    """`root` 配下の正本を検証し、**見つかった問題を全部**返す。

    最初の1件で止めないのは、PR のレビューで「直して push、また別の指摘」を
    繰り返させないため。
    """
    if not root.is_dir():
        raise SystemExit(f"root がディレクトリではない: {root}")

    errors: list[str] = []
    docs: dict[str, core.Document] = {}
    # doc_id が重複していると docs に入らないので、対称性の検査から漏れる。
    # 「重複」と「対称性が壊れている」を別々に報告するために場所を分けて数える
    seen: dict[str, pathlib.Path] = {}

    for path in sorted(root.rglob("*.md")):
        if path.name == "README.md":
            continue

        where = path.relative_to(root)
        text = path.read_text(encoding="utf-8")

        # ① front matter が読めるか（規則は core と共通）
        try:
            doc = core.parse_document(text, str(where))
        except ValueError as exc:
            errors.append(f"{where}: {exc}")
            continue

        meta, _ = core.split_front_matter(text, str(where))

        # ② 値が語彙の中にあるか
        if doc.doc_type not in DOC_TYPES:
            errors.append(f"{where}: doc_type が {DOC_TYPES} のどれでもない: {doc.doc_type!r}")
        if doc.status not in STATUSES:
            errors.append(f"{where}: status が {STATUSES} のどれでもない: {doc.status!r}")
        # **形だけ見ると `2026-03-XX` を通してしまう**（10 文字・ハイフン 2 個）。
        # エージェントが今日の日付を知らずにプレースホルダを書いてきたのを実測で見た
        try:
            datetime.date.fromisoformat(doc.date)
        except ValueError:
            errors.append(f"{where}: date が実在する ISO 日付（YYYY-MM-DD）ではない: {doc.date!r}")
        if not doc.body:
            errors.append(f"{where}: 本文が空")

        # ③ ファイル名と doc_id が一致しているか。
        #    ずれると get_document の ID 参照（§6「doc_id で辿る」）が空振りする
        if path.stem != doc.doc_id:
            errors.append(f"{where}: ファイル名と doc_id が違う（doc_id={doc.doc_id}）")

        # ④ 置き場所と型が合っているか
        expected = DIR_TO_TYPE.get(where.parts[0] if len(where.parts) > 1 else "")
        if expected is None:
            errors.append(f"{where}: {tuple(DIR_TO_TYPE)} のいずれかの直下に置く")
        elif doc.doc_type != expected:
            errors.append(f"{where}: {where.parts[0]}/ には doc_type={expected} を置く（{doc.doc_type}）")

        # ⑤ 型ごとに使える status。meeting は必ず proposed、それ以外に proposed は無い
        allowed = STATUS_BY_TYPE.get(doc.doc_type)
        if allowed and doc.status not in allowed:
            if doc.doc_type == "meeting":
                errors.append(f"{where}: meeting は status: proposed（検討の記録であって決定ではない）")
            else:
                errors.append(
                    f"{where}: {doc.doc_type} に status: {doc.status} は使えない"
                    f"（{allowed} のいずれか。proposed は meeting だけ）"
                )

        # ⑥ 版の連鎖は decision だけが持つ
        if doc.doc_type != "decision":
            present = [k for k in VERSIONED_KEYS if core._opt(meta.get(k))]
            if present:
                errors.append(f"{where}: {', '.join(present)} は decision だけが持てる")

        # ⑦ status と版の整合。片方だけ書かれていると「旧→新」を出せない
        if doc.superseded_by and doc.status != "superseded":
            errors.append(f"{where}: superseded_by があるなら status: superseded")
        if doc.status == "superseded" and not doc.superseded_by:
            errors.append(f"{where}: status: superseded なら superseded_by が要る")

        # ⑧ topic の allowlist 照合
        if topics is not None:
            topic = core._opt(meta.get("topic"))
            if topic is None:
                errors.append(f"{where}: topic が無い")
            elif topic not in topics:
                errors.append(f"{where}: topic が allowlist に無い: {topic!r}（増やすなら topics.yml も同じ PR で）")

        # ⑨ doc_id の一意性
        if doc.doc_id in seen:
            errors.append(f"{where}: doc_id が {seen[doc.doc_id]} と重複している（{doc.doc_id}）")
        else:
            seen[doc.doc_id] = where
            docs[doc.doc_id] = doc

    errors.extend(_check_symmetry(docs))
    return errors


def _check_symmetry(docs: dict[str, core.Document]) -> list[str]:
    """`supersedes` / `superseded_by` が双方向に張られているかを見る。

    **片側だけ更新されると来歴が切れる**（§5.3）。人でもやる間違いなので機械で見る。
    出力契約ルール4（旧版と新版の両方を日付付きで示す）がここに乗っている。
    """
    errors = []
    for doc in docs.values():
        if doc.supersedes:
            old = docs.get(doc.supersedes)
            if old is None:
                errors.append(f"{doc.doc_id}: supersedes の相手が無い（{doc.supersedes}）")
            elif old.superseded_by != doc.doc_id:
                errors.append(
                    f"{doc.doc_id}: supersedes: {doc.supersedes} に対して "
                    f"{doc.supersedes} の superseded_by が {old.superseded_by!r}（逆参照が張られていない）"
                )
        if doc.superseded_by:
            new = docs.get(doc.superseded_by)
            if new is None:
                errors.append(f"{doc.doc_id}: superseded_by の相手が無い（{doc.superseded_by}）")
            elif new.supersedes != doc.doc_id:
                errors.append(
                    f"{doc.doc_id}: superseded_by: {doc.superseded_by} に対して "
                    f"{doc.superseded_by} の supersedes が {new.supersedes!r}（逆参照が張られていない）"
                )
    return errors


def main() -> None:
    default_root = pathlib.Path(__file__).resolve().parents[2] / "knowledge_base"

    parser = argparse.ArgumentParser(description="正本の規約を検証する")
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=default_root,
        help="検証する knowledge_base ディレクトリ（既定はこのリポジトリのもの）",
    )
    parser.add_argument(
        "--topics",
        type=pathlib.Path,
        default=None,
        help="topic の allowlist（docs/topics.yml）。省略すると topic を見ない",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    errors = validate(root, load_topics(args.topics))
    count = sum(1 for p in root.rglob("*.md") if p.name != "README.md")

    if errors:
        print(f"{root} の {count} 件に {len(errors)} 件の問題がある:", file=sys.stderr)
        for message in errors:
            print(f"  - {message}", file=sys.stderr)
        raise SystemExit(1)

    # 0 件で PASS するが、それが分かるように件数を出す（--root の指定ミスに気づけるように）
    print(f"{root} の {count} 件は規約どおり")


if __name__ == "__main__":
    main()
