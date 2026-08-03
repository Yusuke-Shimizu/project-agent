"""Gateway の MCP を直接叩いて確かめる（L3a の判定）。

**エージェントを通さない。** L1b で「KB を作るだけ作って、Retrieve だけを直接
確かめた」のと同じ形（§9）。ここで `tools/list` と `tools/call` が通っていれば、
L3b で答えが変わったときに「MCP クライアントの繋ぎ込みが原因」と切り分けられる。

    uv run python code/scripts/check_gateway.py
    uv run python code/scripts/check_gateway.py --show

インバウンド認証は `AWS_IAM` なので、Cognito のトークンは要らない。**普段の
AWS 認証情報でそのまま SigV4 署名して叩く。**
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

#: Gateway が公開するツール名は `<ターゲット名>___<ツール名>`（§4.3）
DELIMITER = "___"

#: 台本4問と同じクエリで引く。ここで期待 doc が返らないなら L3b に進んでも無駄
CASES = [
    ("マルチテナント 物理分離 テナント", {"DEC-004", "MTG-2026-03-15"}),
    ("非同期処理 Step Functions", {"DEC-003a", "DEC-003b"}),
    ("PartnerSync連携 即時リトライ", {"KNW-002"}),
]


class SigV4(httpx.Auth):
    """MCP の HTTP リクエストに SigV4 署名を足す。

    `streamablehttp_client` は `auth` に httpx.Auth を取るので、ここを差すだけで
    済む。**Cognito を挟まないぶん、更新も失効も無い。**
    """

    #: 署名対象のサービス名
    SERVICE = "bedrock-agentcore"

    def __init__(self, region: str) -> None:
        session = boto3.Session()
        self._credentials = session.get_credentials()
        self._region = region

    def auth_flow(self, request: httpx.Request):
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={
                # SigV4 の署名対象に入れるヘッダだけを渡す。httpx が後から足す
                # ヘッダ（Content-Length など）を含めると署名が合わなくなる
                k: v
                for k, v in request.headers.items()
                if k.lower() in ("content-type", "accept")
            },
        )
        SigV4Auth(self._credentials, self.SERVICE, self._region).add_auth(aws_request)
        request.headers.update(dict(aws_request.headers))
        yield request


def resolve_gateway_url(region: str) -> str:
    """スタックの出力から MCP のエンドポイントを引く。

    URL にはアカウント由来の ID が入るので、`resolve_bucket()` と同じ考え方で
    設定ファイルに書かず、CloudFormation から引く（§10-13）。
    """
    if url := os.environ.get("KAI_GATEWAY_URL"):
        return url
    client = boto3.client("cloudformation", region_name=region)
    outputs = client.describe_stacks(StackName="KaiToolsStack")["Stacks"][0]["Outputs"]
    for output in outputs:
        if output["OutputKey"] == "GatewayUrl":
            return output["OutputValue"]
    raise RuntimeError("KaiToolsStack に GatewayUrl の出力が無い")


def _doc_ids(payload) -> set[str]:
    """ツールの戻り値から doc_id を拾う。"""
    if isinstance(payload, dict):
        payload = [payload]
    return {d.get("doc_id") for d in payload if isinstance(d, dict)} - {None}


def _result_json(result) -> object:
    """MCP のツール結果から中身を取り出す。

    構造化された結果が無ければテキストを JSON として読む。**dict をそのまま
    文字列にして返していないか**もここで効く（§10-24）。
    """
    if getattr(result, "structuredContent", None):
        content = result.structuredContent
        # MCP は配列を返すとき {"result": [...]} で包む
        return content.get("result", content) if isinstance(content, dict) else content
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    return None


async def run(show: bool) -> int:
    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    url = resolve_gateway_url(region)
    print(f"Gateway: {url}\n")

    failures = 0
    async with streamablehttp_client(url, auth=SigV4(region)) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            listed = await session.list_tools()
            names = [t.name for t in listed.tools]
            print(f"tools/list: {names}")

            # 前置きを剥がした名前で照合する。前置きはターゲット名なので、
            # ターゲットを張り替えると変わる ―― ツールの I/F の一部ではない
            bare = {n.split(DELIMITER)[-1]: n for n in names}
            for expected in ("search_project_knowledge", "get_document"):
                if expected not in bare:
                    print(f"  [FAIL] {expected} が公開されていない")
                    failures += 1
            if failures:
                return failures
            print()

            for query, expected in CASES:
                result = await session.call_tool(
                    bare["search_project_knowledge"], {"query": query}
                )
                hits = _result_json(result)
                found = _doc_ids(hits)
                ok = expected <= found
                print(f"  [{'PASS' if ok else 'FAIL'}] {query}")
                print(f"        期待: {sorted(expected)} / 実際: {sorted(found)}")
                if show:
                    print(json.dumps(hits, ensure_ascii=False, indent=2)[:1200])
                failures += not ok

            # get_document は検索順位に依存しない経路。ここが通らないと
            # supersedes の追跡（台本の問2）が成立しない
            result = await session.call_tool(bare["get_document"], {"doc_id": "DEC-003a"})
            doc = _result_json(result)
            ok = isinstance(doc, dict) and doc.get("superseded_by") == "DEC-003b"
            print(f"  [{'PASS' if ok else 'FAIL'}] get_document(DEC-003a) → superseded_by")
            failures += not ok

    total = len(CASES) + 1
    print(f"\n{total - failures}/{total} PASS")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Gateway の MCP を直接叩いて確かめる")
    parser.add_argument("--show", action="store_true", help="戻り値をそのまま出す")
    args = parser.parse_args()
    sys.exit(1 if asyncio.run(run(args.show)) else 0)


if __name__ == "__main__":
    main()
