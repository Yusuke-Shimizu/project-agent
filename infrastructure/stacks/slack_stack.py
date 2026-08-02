"""Slack の入口（architecture_v1.md §4.1）。

    Slack ──▶ API Gateway (HTTP API) ──▶ Lambda: ingress ──非同期──▶ Lambda: worker
                                          （3秒以内に200）              （InvokeAgentRuntime）

Runtime の ARN は AgentCore CLI 側の CDK が持っているので、context で受ける:

    cdk deploy KaiSlackStack -c runtimeArn=arn:aws:bedrock-agentcore:...:runtime/...

Bot Token と Signing Secret は **Secrets Manager の箱だけ作る**。値は入れない
（このリポジトリは public で、認証情報は誰も IaC に書くべきでない）。
デプロイ後に本人が put-secret-value する。
"""

import json

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct


class SlackStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, runtime_arn: str, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # 秘匿情報の箱（値は入れない）
        # ------------------------------------------------------------------
        secret = secretsmanager.Secret(
            self,
            "SlackSecret",
            secret_name="kai/slack",
            description="Slack bot token and signing secret (fill in manually)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                # テンプレートで鍵の形だけ作る。値は REPLACE_ME のままなので、
                # デプロイ後に aws secretsmanager put-secret-value で入れ替える
                secret_string_template=json.dumps(
                    {"bot_token": "REPLACE_ME", "signing_secret": "REPLACE_ME"}
                ),
                # テンプレートを使うには生成キーが1つ要る。中身は使わない
                generate_string_key="unused",
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        common = dict(
            runtime=lambda_.Runtime.PYTHON_3_13,
            code=lambda_.Code.from_asset("code/slack"),
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # ------------------------------------------------------------------
        # worker：時間をかけてよい方
        # ------------------------------------------------------------------
        worker = lambda_.Function(
            self,
            "SlackWorker",
            handler="worker.handler",
            # エージェントは1問 20 秒ほど。余裕を持たせる（§4.6）
            timeout=Duration.seconds(300),
            memory_size=512,
            environment={
                "AGENT_RUNTIME_ARN": runtime_arn,
                "SLACK_SECRET_ID": secret.secret_name,
            },
            **common,
        )
        secret.grant_read(worker)
        worker.add_to_role_policy(
            iam.PolicyStatement(
                sid="InvokeAgentRuntime",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                # エンドポイント（/runtime-endpoint/DEFAULT）も対象に含める
                resources=[runtime_arn, f"{runtime_arn}/*"],
            )
        )

        # ------------------------------------------------------------------
        # ingress：3秒以内に必ず 200 を返す方
        # ------------------------------------------------------------------
        ingress = lambda_.Function(
            self,
            "SlackIngress",
            handler="ingress.handler",
            # 受けて投げるだけ。長くする理由が無く、短い方が事故に気づける
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={
                "WORKER_FUNCTION_NAME": worker.function_name,
                "SLACK_SECRET_ID": secret.secret_name,
            },
            **common,
        )
        secret.grant_read(ingress)
        worker.grant_invoke(ingress)

        # ------------------------------------------------------------------
        # API Gateway（HTTP API）
        # ------------------------------------------------------------------
        api = apigw.HttpApi(self, "SlackApi", description="Slack Events API endpoint")
        api.add_routes(
            path="/slack/events",
            methods=[apigw.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("Ingress", ingress),
        )

        CfnOutput(
            self,
            "SlackEventsUrl",
            value=f"{api.api_endpoint}/slack/events",
            description="Slack App の Event Subscriptions に登録する URL",
        )
        CfnOutput(
            self,
            "SlackSecretName",
            value=secret.secret_name,
            description="bot_token と signing_secret を入れる Secrets Manager の名前",
        )
