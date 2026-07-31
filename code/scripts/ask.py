"""L0 の入口（CLI）。

architecture_v1.md §9 の軸C「入口」の手前側。L2 でここが Slack に置き換わるが、
エージェントの組み立て（`build_agent`）とツールはそのまま使い回す。

    uv run python code/scripts/ask.py "マルチテナントは物理分離で書きました"
    uv run python code/scripts/ask.py            # 対話モード
"""

from __future__ import annotations

import sys

from runtime.agent import build_agent


def main() -> None:
    agent = build_agent()  # 生成中の出力はそのまま標準出力に流れる

    if len(sys.argv) > 1:
        agent(" ".join(sys.argv[1:]))
        print()
        return

    print("案件 KAI のアシスタント（Ctrl-D で終了）")
    while True:
        try:
            prompt = input("\n> ").strip()
        except EOFError:
            print()
            return
        if prompt:
            print()
            agent(prompt)


if __name__ == "__main__":
    main()
