"""AgentCore Gateway に MCP で繋ぐ側（architecture_v1.md §4.3、L3）。

`local_tools.py`（プロセス内 `@tool`）と対になる **もう一つの呼ばれ方**。ツールの
実体は Gateway の向こうの Lambda にあり、そこでも同じ `core.py` が動いている。

**インバウンド認証は `AWS_IAM`。** §4.3 は Cognito の JWT（M2M）を前提にしていたが、
`AuthorizerType` に `AWS_IAM` があるので Cognito ごと不要になった。トークンの取得も
更新も失効も無く、**呼べるのは `bedrock-agentcore:InvokeGateway` を持つ主体だけ**。
「判断は人に残す」を IAM で表現した §5.2 と同じ考え方で、接続も IAM で表現する。
"""

from __future__ import annotations

import os

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

#: Gateway が公開するツール名は `<ターゲット名>___<ツール名>`
DELIMITER = "___"


class SigV4(httpx.Auth):
    """MCP の HTTP リクエストに SigV4 署名を足す。

    `streamablehttp_client` が `auth` に httpx.Auth を取るので、差し込むのはここだけ。
    """

    #: 署名対象のサービス名
    SERVICE = "bedrock-agentcore"

    #: 署名に含めるヘッダ。httpx が後から足すもの（Content-Length など）を
    #: 含めると署名が合わなくなるので、こちらから明示する
    SIGNED_HEADERS = ("content-type", "accept")

    def __init__(self, region: str) -> None:
        self._credentials = boto3.Session().get_credentials()
        self._region = region

    def auth_flow(self, request: httpx.Request):
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={
                k: v
                for k, v in request.headers.items()
                if k.lower() in self.SIGNED_HEADERS
            },
        )
        SigV4Auth(self._credentials, self.SERVICE, self._region).add_auth(aws_request)
        request.headers.update(dict(aws_request.headers))
        yield request


def resolve_gateway_url(region: str | None = None) -> str:
    """MCP のエンドポイントを引く。

    URL にはアカウント由来の ID が入るので、`core.resolve_bucket()` と同じ考え方で
    設定ファイルに書かず CloudFormation の出力から引く（§10-13）。
    """
    if url := os.environ.get("KAI_GATEWAY_URL"):
        return url

    region = region or os.environ.get("AWS_REGION", "ap-northeast-1")
    client = boto3.client("cloudformation", region_name=region)
    outputs = client.describe_stacks(StackName="KaiToolsStack")["Stacks"][0]["Outputs"]
    for output in outputs:
        if output["OutputKey"] == "GatewayUrl":
            return output["OutputValue"]
    raise RuntimeError("KaiToolsStack に GatewayUrl の出力が無い")


def build_mcp_client():
    """Gateway に繋いだ Strands の MCP クライアントを返す（未接続）。

    呼び出し側で `start()` してから `list_tools_sync()` する。
    """
    from mcp.client.streamable_http import streamablehttp_client
    from strands.tools.mcp import MCPClient

    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    url = resolve_gateway_url(region)
    return MCPClient(lambda: streamablehttp_client(url, auth=SigV4(region)))
