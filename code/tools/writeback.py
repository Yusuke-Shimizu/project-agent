"""書き戻し（起案）の実体。`writeback_pr_tool.md` §4 のツール I/F をここに置く。

**エージェントが書けるのは提案まで。正本を変えるのは人のマージだけ。**
このモジュールは PR を立てるところで止まり、マージする API は一切呼ばない。

読みの 2 ツール（`core.py`）と対になる、書きの 2 ツール:

- `propose_knowledge` … 新しい doc を起こす。front matter 込みの全文を受け取る
- `propose_append`    … 既存 doc の末尾に足す。**既存本文を受け取らない**

`propose_append` が既存本文を受け取らないのは検証の都合ではなく、**書き換えの経路を
I/F 上に作らないため**。現物は Lambda が GitHub から読み、末尾に足すだけなので、
既存の記述を書き換える引数が存在しない。渡せないものは壊せない。

構造で閉じているもの:

1. `path` の allowlist（§7）―― `knowledge_base/knowledge/KNW-*.md` だけ。
   入力は Slack のメッセージなので、ここはプロンプトインジェクションの経路になる
2. `based_on` が空なら拒否 ―― 根拠なしの起案を塞ぐ（出力契約ルール1・2 の書き込み側）
3. front matter が壊れていれば PR を立てる前に落とす（`core` と同じ規則で見る）
4. open な起案 PR が多すぎるときは起案しない（§2 のバックプレッシャー）
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from tools import core

GITHUB_API = "https://api.github.com"

#: 起案してよいディレクトリ。
#:
#: **v1 は `knowledge` だけ。** `decision` は「新規・高影響・トレードオフ」なので人が
#: 起こす（§0）。`meeting` は検討の記録なのでエージェントが書くものではない。
#: `superseded` の対称更新で `decisions` を触る段になったらここを広げる
ALLOWED_DIRS = ("knowledge",)

#: `path` の allowlist。**lint ではなく入口で弾く**（§7）
ALLOWED_PATH = re.compile(
    r"^knowledge_base/(?:" + "|".join(ALLOWED_DIRS) + r")/[A-Z]{3}-[0-9a-z-]+\.md$"
)

#: PR に付けるラベル。マージ率の集計と、バックプレッシャーの数え上げに使う
LABEL = os.environ.get("KAI_PROPOSAL_LABEL", "proposed-by-agent")

#: open な起案 PR の上限。詰まっているときは新しく起案しない（§2）
MAX_OPEN_PROPOSALS = int(os.environ.get("KAI_MAX_OPEN_PROPOSALS", "5"))

#: 起案先。`<owner>/<name>`
REPO = os.environ.get("KAI_WRITEBACK_REPO", "Yusuke-Shimizu/kai-knowledge")

#: PR の宛先ブランチ
BASE_BRANCH = os.environ.get("KAI_WRITEBACK_BASE", "main")

#: GitHub App の資格情報が入っている Secrets Manager のシークレット
APP_SECRET_ID = os.environ.get("KAI_GITHUB_APP_SECRET_ID", "kai/github-app")


class ProposalRejected(Exception):
    """起案を受け付けなかった。**理由をエージェントに返す**（握りつぶさない）。

    Gateway の `ExceptionLevel: DEBUG` と対で、エージェントから「空の結果」と
    区別できるようにする（§4.3）。
    """


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------


def _http(method: str, url: str, headers: dict, body: dict | None) -> dict:
    """GitHub API を1回叩く。Slack の worker と同じく `urllib` で書く（依存を足さない）。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ProposalRejected(f"GitHub API {method} {url} が {exc.code}: {detail}") from exc
    return json.loads(raw) if raw else {}


def _app_jwt(app_id: str, private_key_pem: str) -> str:
    """App の JWT を作る（RS256・有効 9 分）。

    `jwt` はこの関数の中でだけ使うので遅延 import する。テストは
    `Client(token=...)` でトークンを直接渡せるため、ここを通らない。
    """
    import jwt  # noqa: PLC0415

    now = int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 9 * 60, "iss": app_id},
        private_key_pem,
        algorithm="RS256",
    )


def _installation_token(app_id: str, private_key_pem: str, repo: str) -> str:
    """インストールトークンを取る。**1 時間で失効する短命トークン**。

    `contents:write` と `pull_requests:write` だけを持つ App を、この
    リポジトリにインストールしてある前提（`docs/github_app.md`）。
    """
    headers = {
        "Authorization": f"Bearer {_app_jwt(app_id, private_key_pem)}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    installation = _http("GET", f"{GITHUB_API}/repos/{repo}/installation", headers, None)
    created = _http(
        "POST",
        f"{GITHUB_API}/app/installations/{installation['id']}/access_tokens",
        headers,
        {"repositories": [repo.split("/")[1]]},
    )
    return str(created["token"])


def _credentials() -> tuple[str, str]:
    """Secrets Manager から App ID と秘密鍵を読む。

    **Runtime のロールはこのシークレットを読めない。**読めるのは起案 Lambda の
    実行ロールだけで、それが「エージェント自身は書けない」の物理的な裏返しになる
    （§6。`s3:PutObject` を与えないのと同じ表現）。
    """
    import boto3  # noqa: PLC0415

    secrets = boto3.client("secretsmanager")
    value = json.loads(secrets.get_secret_value(SecretId=APP_SECRET_ID)["SecretString"])
    app_id, key = value.get("app_id"), value.get("private_key")
    if not app_id or not key or "REPLACE_ME" in str(app_id):
        raise ProposalRejected(
            f"{APP_SECRET_ID} に App ID と秘密鍵が入っていない（値は本人が入れる）"
        )
    return str(app_id), str(key)


@dataclasses.dataclass
class Client:
    """1 リポジトリ分の GitHub 操作。**マージする API は持たない。**"""

    repo: str = REPO
    base: str = BASE_BRANCH
    token: str | None = None
    #: HTTP を差し替える口（テストはここに偽の transport を渡す）
    transport: Callable[[str, str, dict, dict | None], object] = _http

    def __post_init__(self) -> None:
        if self.token is None:
            self.token = _installation_token(*_credentials(), self.repo)

    def _call(self, method: str, path: str, body: dict | None = None):
        return self.transport(
            method,
            f"{GITHUB_API}/repos/{self.repo}{path}",
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            body,
        )

    # --- 読み ---

    def open_proposals(self) -> list[dict]:
        return list(self._call("GET", "/pulls?state=open&per_page=100") or [])

    def find_pull(self, branch: str) -> dict | None:
        owner = self.repo.split("/")[0]
        found = self._call("GET", f"/pulls?state=open&head={owner}:{branch}")
        return found[0] if found else None

    def get_file(self, path: str) -> tuple[str, str]:
        """`(本文, sha)` を返す。無ければ `ProposalRejected`。"""
        got = self._call("GET", f"/contents/{path}?ref={self.base}")
        try:
            text = base64.b64decode(got["content"]).decode("utf-8")
        except (KeyError, binascii.Error, UnicodeDecodeError) as exc:
            raise ProposalRejected(f"{path} を読めない: {exc}") from exc
        return text, str(got["sha"])

    # --- 書き（PR まで） ---

    def create_branch(self, branch: str) -> None:
        head = self._call("GET", f"/git/ref/heads/{self.base}")
        self._call(
            "POST",
            "/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": head["object"]["sha"]},
        )

    def put_file(self, branch: str, path: str, text: str, message: str, sha: str | None) -> None:
        body = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        self._call("PUT", f"/contents/{path}", body)

    def create_pull(self, branch: str, title: str, body: str) -> dict:
        pull = self._call(
            "POST", "/pulls", {"title": title, "head": branch, "base": self.base, "body": body}
        )
        # ラベルが付かなくても PR は立っている。集計が欠けるだけなので落とさない
        try:
            self._call("POST", f"/issues/{pull['number']}/labels", {"labels": [LABEL]})
        except ProposalRejected as exc:
            print(f"ラベルを付けられなかった（PR は立っている）: {exc}")
        return pull


# --------------------------------------------------------------------------
# 起案
# --------------------------------------------------------------------------


def _today() -> str:
    return datetime.date.today().isoformat()


def _branch_name(kind: str, key: str, text: str) -> str:
    """内容から決める。**同じ内容の PR を連発しない**ため（§4）。"""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"writeback/{kind}-{key.lower()}-{digest}"


def _check_common(based_on: list[str], source_url: str, client: Client) -> None:
    if not based_on:
        raise ProposalRejected(
            "based_on が空。根拠にした doc_id を挙げていない起案は受け付けない"
        )
    if not source_url:
        raise ProposalRejected("source_url が無い。どのやりとりから出た起案か辿れない")

    open_count = sum(1 for p in client.open_proposals() if _is_proposal(p))
    if open_count >= MAX_OPEN_PROPOSALS:
        raise ProposalRejected(
            f"未処理の起案が {open_count} 件あるので新しく起案しない"
            f"（上限 {MAX_OPEN_PROPOSALS}）。先に既存の PR を片付けてほしい"
        )


def _is_proposal(pull: dict) -> bool:
    return any(label.get("name") == LABEL for label in pull.get("labels") or [])


def _pr_body(based_on: list[str], source_url: str, requested_by: str, note: str) -> str:
    """**レビューに必要なものを構造で載せる。**マージ率の集計もここから取れる。"""
    return "\n".join(
        [
            note,
            "",
            "## 根拠にした doc",
            *[f"- {doc_id}" for doc_id in based_on],
            "",
            f"## 出どころ\n{source_url}",
            "",
            f"Requested-by: {requested_by}",
            "",
            "---",
            "**起案なので、正本になるのはマージされた時点。**"
            "根拠の doc を実際に開いて、書かれている内容と合っているか確かめてほしい。",
        ]
    )


def propose_knowledge(
    files: list[dict],
    summary: str,
    based_on: list[str],
    source_url: str,
    requested_by: str,
    client: Client | None = None,
) -> dict:
    """新しい doc を起案する。`files` は `[{"path": ..., "content": ...}]`。

    front matter はエージェントが書く（§5）。**人の PR と同じ規約・同じ CI を通す**ので、
    ここでは PR を立てる前に落とせるものだけ見る。
    """
    client = client or Client()
    if not files:
        raise ProposalRejected("files が空")
    if len(files) > 1:
        # 1 PR = 1 doc（§4）。レビューできない PR は負の複利
        raise ProposalRejected(f"1 回の起案で出せるのは 1 ファイルまで（{len(files)} 件）")

    path = str(files[0].get("path") or "")
    content = str(files[0].get("content") or "")

    if not ALLOWED_PATH.match(path):
        raise ProposalRejected(
            f"起案できる path ではない: {path!r}"
            f"（{'/'.join(ALLOWED_DIRS)} の下だけ。decision は人が起こす）"
        )

    # front matter が壊れていれば PR にする前に落とす。規則は core と共通
    try:
        doc = core.parse_document(content, path)
    except ValueError as exc:
        raise ProposalRejected(f"front matter が規約に合わない: {exc}") from exc

    stem = path.rsplit("/", 1)[-1].removesuffix(".md")
    if doc.doc_id != stem:
        raise ProposalRejected(f"ファイル名と doc_id が違う: {stem} / {doc.doc_id}")

    _check_common(based_on, source_url, client)

    branch = _branch_name("new", doc.doc_id, content)
    existing = client.find_pull(branch)
    if existing:
        # 同じ内容を二度出しても PR は増えない（§4 の「連発しない」）
        return {"pr_url": existing["html_url"], "branch": branch, "created": False}

    client.create_branch(branch)
    client.put_file(branch, path, content, f"docs: {summary}", sha=None)
    pull = client.create_pull(
        branch,
        f"docs: {summary}",
        _pr_body(based_on, source_url, requested_by, f"`{doc.doc_id}` を新しく起案する。"),
    )
    return {"pr_url": pull["html_url"], "branch": branch, "doc_id": doc.doc_id, "created": True}


def propose_append(
    doc_id: str,
    body: str,
    based_on: list[str],
    source_url: str,
    requested_by: str,
    client: Client | None = None,
) -> dict:
    """既存 doc の**末尾に**足す。

    **既存本文を引数に取らない。**現物は GitHub から読み、末尾に見出しを付けて足すだけ
    なので、既存の記述を書き換える経路が I/F 上に存在しない（§4）。front matter も
    1 バイトも変えない ―― 触る場所が末尾しかないので、構造的にそうなる。
    """
    client = client or Client()
    if not body.strip():
        raise ProposalRejected("追記する本文が空")

    path = f"knowledge_base/knowledge/{doc_id}.md"
    if not ALLOWED_PATH.match(path):
        raise ProposalRejected(f"追記できる doc_id ではない: {doc_id!r}")

    _check_common(based_on, source_url, client)

    current, sha = client.get_file(path)
    # 読めることを確かめる。壊れている doc に足して二重に壊さない
    core.parse_document(current, path)

    # 本文しか見ないエージェントが「いつの追記か」を根拠にできるよう日付を残す（§4）
    addition = f"\n\n## 追記 {_today()}\n\n{body.strip()}\n"
    updated = current.rstrip("\n") + addition

    branch = _branch_name("append", doc_id, addition)
    existing = client.find_pull(branch)
    if existing:
        return {"pr_url": existing["html_url"], "branch": branch, "created": False}

    client.create_branch(branch)
    client.put_file(branch, path, updated, f"docs: {doc_id} に追記する", sha=sha)
    pull = client.create_pull(
        branch,
        f"docs: {doc_id} に追記する",
        _pr_body(
            based_on,
            source_url,
            requested_by,
            f"`{doc_id}` の**末尾に**追記する。既存の記述と front matter は変えていない。",
        ),
    )
    return {"pr_url": pull["html_url"], "branch": branch, "doc_id": doc_id, "created": True}
