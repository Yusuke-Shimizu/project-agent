"""起案の提案（ボタン）の受け渡し形式。**worker と interactive の間の契約。**

`writeback_pr_tool.md` §2 の D（AI が提案 → 人がボタンで承認）を成立させる部品。

なぜマーカー行で受け渡すのか
----------------------------
エージェントの回答は出力契約で 3 ブロック固定なので、**そこに構造化データを混ぜたくない**。
代わりに**最終行にだけ機械が読む 1 行**を置き、worker が剥がしてボタンに変換する。
人が見る文面は 3 ブロックのまま変わらない。

    【指摘】…
    【根拠】…
    【確認してほしいこと】…
    [[PROPOSE]] kind=append doc_id=KNW-002 based_on=KNW-002,DEC-003b summary=...

なぜボタンの value に要旨を持たせるのか
--------------------------------------
**下書きはクリックされてから作る**（押されなかった提案のコストをゼロにする）。
そのときスレッドを読み直せると楽だが、それには `channels:history` スコープが要り、
Slack App の再インストールが必要になる。**要旨と根拠 doc_id をボタンに持たせておけば
スコープを増やさずに済む** ―― 本文はエージェントが正本を引き直して書ける。

Slack の `value` は 2000 文字までなので、要旨 1 行と doc_id なら十分収まる。
"""

from __future__ import annotations

import datetime
import json

#: 回答の最終行に置く印。人には見せない
MARKER = "[[PROPOSE]]"

#: ボタンの action_id
ACTION_PROPOSE = "kai_propose"
ACTION_DISMISS = "kai_dismiss"

#: `value` の上限（Slack の仕様）。超えると Slack が 400 を返す
MAX_VALUE = 2000


def parse_marker(text: str) -> tuple[str, dict | None]:
    """回答からマーカー行を剥がして `(人に見せる本文, 提案)` を返す。

    マーカーが無ければ `(元の本文, None)`。**壊れたマーカーは黙って捨てる**
    ―― 提案が出ないだけで、回答は届いたほうがよい。
    """
    lines = text.rstrip().split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].startswith(MARKER):
            continue

        raw = lines[i][len(MARKER) :].strip()
        body = "\n".join(lines[:i]).rstrip()
        parsed = _parse_fields(raw)
        return body, parsed

    return text, None


def _parse_fields(raw: str) -> dict | None:
    """`kind=append doc_id=KNW-002 based_on=A,B summary=残りぜんぶ` を辞書にする。

    `summary` は最後に置く約束にして、そこから行末までを値とする
    （要旨に空白が入るため）。
    """
    fields: dict[str, str] = {}
    rest = raw
    for key in ("kind", "doc_id", "based_on"):
        token = f"{key}="
        if token not in rest:
            continue
        after = rest.split(token, 1)[1]
        value = after.split(" ", 1)[0]
        fields[key] = value
        rest = after[len(value) :]

    if "summary=" in raw:
        fields["summary"] = raw.split("summary=", 1)[1].strip()

    kind = fields.get("kind")
    if kind not in ("new", "append"):
        return None
    if kind == "append" and not fields.get("doc_id"):
        return None
    if not fields.get("summary"):
        return None

    based_on = [d for d in (fields.get("based_on") or "").split(",") if d]
    if not based_on:
        # 根拠なしの起案は受け付けないので、提案の段でも出さない
        return None

    return {
        "kind": kind,
        "doc_id": fields.get("doc_id", ""),
        "based_on": based_on,
        "summary": fields["summary"],
    }


def button_value(proposal: dict) -> str:
    """ボタンに載せる値。2000 文字に収める。"""
    value = json.dumps(
        {
            "k": proposal["kind"],
            "d": proposal.get("doc_id", ""),
            "b": proposal["based_on"],
            "s": proposal["summary"],
        },
        ensure_ascii=False,
    )
    if len(value) <= MAX_VALUE:
        return value

    # 要旨を削って収める。根拠 doc_id は落とさない（起案の前提なので）
    room = MAX_VALUE - (len(value) - len(proposal["summary"]))
    trimmed = dict(proposal, summary=proposal["summary"][: max(room - 8, 20)])
    return button_value(trimmed) if room > 30 else json.dumps({"k": proposal["kind"]})


def blocks(text: str, proposal: dict) -> list[dict]:
    """回答 + 起案ボタンの Slack blocks。

    **「不要」も置く。** 押されない提案と明示的に断られた提案を区別できないと、
    §10 の「提案の的中率」が指標として使えない。
    """
    label = (
        f"{proposal['doc_id']} に追記する"
        if proposal["kind"] == "append"
        else "knowledge として起案する"
    )
    value = button_value(proposal)

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "これは知見として残す価値がありそうです。"
                        "*起案しても正本には入りません*（PR が立つだけで、"
                        "マージするのは人の判断）。"
                    ),
                }
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_PROPOSE,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": label},
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": ACTION_DISMISS,
                    "text": {"type": "plain_text", "text": "不要"},
                    "value": "dismiss",
                },
            ],
        },
    ]


def without_actions(blocks: list[dict] | None) -> list[dict]:
    """ボタンの行だけを外して、**回答の本文は残す**。

    以前は `chat.update` で本文ごと結果に差し替えていたが、それだと
    **押した瞬間に 3 ブロックの回答が消える**（実測で気づいた）。何を提案されて何を
    断ったのかが後から追えず、§10 の「提案の的中率」を数えるときにも困る。

    本文を残してボタンだけ外せば、二度押しは防げて履歴は残る。
    """
    return [b for b in (blocks or []) if b.get("type") != "actions"]


#: 追記の見出しと同じく JST。**Lambda は UTC なので固定オフセットで持つ**
JST = datetime.timezone(datetime.timedelta(hours=9))

#: front matter の要求。**エージェントは今日の日付を知らない**ので指示文に入れる。
#:
#: 実測（2026-08-10）では `date: 2026-03-XX` というプレースホルダと、
#: knowledge に `status: proposed`、`topic` の欠落を書いてきた。CI は止めたが、
#: **PR を立てる前に分かることは指示文で渡しておく**ほうが早い。
FRONT_MATTER_RULES = """front matter は次を満たすこと:

- `doc_id` / `doc_type` / `title` / `date` / `status` / `topic` は必須
- `date` は **{today}**（今日。推測やプレースホルダを書かない）
- `status` は **active**（マージされた時点で正本になるので、proposed にはしない）
- `topic` は既存の doc が使っている値から選ぶ（新語を作らない）
- `owner` と `review_by` も既存の doc に倣って入れる
- `supersedes` / `superseded_by` / `decided_by` は **decision だけ**が持てる"""


def front_matter_rules(now: datetime.datetime | None = None) -> str:
    today = (now or datetime.datetime.now(JST)).astimezone(JST).date().isoformat()
    return FRONT_MATTER_RULES.format(today=today)


def directive(proposal: dict, source_url: str, requested_by: str) -> str:
    """クリック後にエージェントへ渡す指示文。

    **下書きはここで初めて作られる。** エージェントは正本を引き直してから
    起案ツールを呼ぶ ―― スレッドを読み直すためのスコープが要らないのはこのため。

    **今日の日付と front matter の要求もここで渡す。** エージェントは日付を知らないし、
    規約の全文も持っていない（実測でプレースホルダを書いてきた）。
    """
    based_on = ", ".join(proposal["based_on"])
    if proposal["kind"] == "append":
        what = (
            f"既存の `{proposal['doc_id']}` の末尾に追記する形で、"
            f"propose_append を呼んでください。"
            "**body に見出しは書かないこと** ―― `## 追記 <日付>` はツールが付けます。"
        )
    else:
        what = (
            "新しい knowledge として propose_knowledge を呼んでください。"
            "doc_id は既存の最大 + 1 で採番してください。\n\n"
            + front_matter_rules()
        )

    return (
        "人が起案を承認しました。以下の要旨で正本への起案（PR）を作ってください。\n\n"
        f"- 要旨: {proposal['summary']}\n"
        f"- 根拠にする doc_id: {based_on}\n"
        f"- source_url: {source_url}\n"
        f"- requested_by: {requested_by}\n\n"
        f"{what}\n"
        "**本文は根拠の doc を実際に読んでから書いてください。**"
        "ツールが拒否したら、その理由をそのまま報告してください。"
        "起案したら PR の URL だけを短く返してください。"
    )
