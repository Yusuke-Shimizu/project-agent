"""正本の置き場（S3）と、その索引（Managed KB）。

architecture_v1.md §5.1 の prefix 分離をここで物理的に表現する。

    s3://<bucket>/
    ├── knowledge_base/   ← KB のデータソースはこの prefix だけ
    └── raw/              ← 提供された Excel / PPT / PDF。KB には取り込まない

L1a で S3 バケット、L1b で Managed KB とデータソースを足した。
**この段では索引を作るだけで、エージェントはまだ KB を見ていない**（§9）。
"""

from aws_cdk import Aws, CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct


class KnowledgeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # 正本の置き場（L1a）
        # ------------------------------------------------------------------
        self.bucket = s3.Bucket(
            self,
            "KnowledgeBucket",
            # アカウント ID をリテラルで書かないための組み立て（public リポジトリなので）
            bucket_name=f"kai-knowledge-{Aws.ACCOUNT_ID}-{Aws.REGION}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # 正本は git 側なので、消して作り直せることを優先する（§5.2）。
            # auto_delete_objects は使わない（Lambda と IAM ロールが増えるだけで、
            # 中身を空にするのは teardown.sh の仕事）。中身が残っていると
            # cdk destroy は失敗するので、先に teardown.sh を流す。
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # KB のサービスロール（L1b）
        # ------------------------------------------------------------------
        # embeddingModelType: MANAGED なので bedrock:InvokeModel は要らない。
        # サービス管理の埋め込みモデルが使われ、モデルアクセスの申請も不要。
        # 渡す権限は「この bucket の knowledge_base/ を読むこと」だけ。
        kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock:{Aws.REGION}:{Aws.ACCOUNT_ID}:knowledge-base/*"
                    },
                },
            ),
            # IAM の description は ASCII / Latin-1 しか通らないので英語で書く
            # （CfnOutput の description は日本語で問題ない）
            description="Lets the managed knowledge base read the source of record in S3",
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="S3ListBucket",
                actions=["s3:ListBucket"],
                resources=[self.bucket.bucket_arn],
                conditions={"StringEquals": {"aws:ResourceAccount": Aws.ACCOUNT_ID}},
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="S3GetObject",
                actions=["s3:GetObject"],
                resources=[f"{self.bucket.bucket_arn}/*"],
                conditions={"StringEquals": {"aws:ResourceAccount": Aws.ACCOUNT_ID}},
            )
        )

        # ------------------------------------------------------------------
        # Managed KB（L1b）
        # ------------------------------------------------------------------
        # §4.4：ハイブリッド検索が常時 ON。固有名詞と doc_id の引き当てにこれが要る。
        # embeddingModelType は KB 作成後に変更できない（§10-5）。
        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=f"kai-knowledge-{Aws.ACCOUNT_ID}",
            role_arn=kb_role.role_arn,
            description="架空案件 KAI の decision / knowledge / meeting",
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="MANAGED",
                managed_knowledge_base_configuration=(
                    bedrock.CfnKnowledgeBase.ManagedKnowledgeBaseConfigurationProperty(
                        embedding_model_type="MANAGED",
                    )
                ),
            ),
        )
        self.knowledge_base.node.add_dependency(kb_role)

        # ------------------------------------------------------------------
        # データソース（L1b）
        # ------------------------------------------------------------------
        # chunkingStrategy: NONE = 1ファイル 1チャンク。13ファイル各数KBなので分割不要で、
        # ドキュメント全体が丸ごと1件で返る＝引き当ての確実性が最大になる（§4.4）。
        # データソース作成後は変更できない（§10-5）。
        self.data_source = bedrock.CfnDataSource(
            self,
            "KnowledgeDataSource",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            name="s3-knowledge-base",
            # Managed KB のデータソースは type: "S3" ではなく
            # MANAGED_KNOWLEDGE_BASE_CONNECTOR で、S3 はその中の connectorParameters
            # として指定する。type: "S3" を渡すと
            # 「Unsupported data source type for MANAGED knowledge base type」で落ちる。
            # KB が見るのは knowledge_base/ prefix だけ。raw/ は取り込まない（§5.1）
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="MANAGED_KNOWLEDGE_BASE_CONNECTOR",
                managed_knowledge_base_connector_configuration=(
                    bedrock.CfnDataSource.ManagedKnowledgeBaseConnectorConfigurationProperty(
                        connector_parameters={
                            "type": "S3",
                            "version": "1",
                            "connectionConfiguration": {
                                "bucketName": self.bucket.bucket_name,
                                "bucketOwnerAccountId": Aws.ACCOUNT_ID,
                            },
                            "filterConfiguration": {
                                "inclusionPrefixes": ["knowledge_base/"],
                            },
                        },
                    )
                ),
            ),
            # chunkingConfiguration は指定しない。
            # embeddingModelType: MANAGED と chunkingStrategy は併用できず、
            # 指定すると「A chunking strategy cannot be specified with a managed
            # embedding model」で落ちる。§4.4 は NONE を前提に書かれているが、
            # 同じ節が「マネージド reranker のために MANAGED のまま触らない」とも
            # 言っており、両立しない。ハイブリッド検索と reranker を取り、
            # 既定チャンキング（fixed-size 300 トークン / 20% overlap）を受け入れる。
            # 正本は数百トークンなので多くは 1 チャンクに収まる。全文が要るときは
            # get_document が S3 を直読みするので、出力契約のルール1は保たれる。
            # 作り直しを繰り返す前提なので、データソースを消したらベクタも消す
            data_deletion_policy="DELETE",
        )

        # ------------------------------------------------------------------
        # AgentCore Runtime の実行ロールへの付与（L1d）
        # ------------------------------------------------------------------
        # Runtime は AgentCore CLI（別の CDK スタック）が作るので、ロールはこちらの
        # 持ち物ではない。だが **KB とバケットはこちらの持ち物**なので、それらへの
        # アクセス許可はここで定義するのが筋。ロール ARN は context で受ける:
        #
        #   cdk deploy KaiKnowledgeStack -c runtimeRoleArn=arn:aws:iam::...:role/...
        #
        # §5.2 のとおり **s3:PutObject は与えない**。エージェントは read-only で、
        # 「判断は人に残す」を権限でも表現する。
        runtime_role_arn = self.node.try_get_context("runtimeRoleArn")
        if runtime_role_arn:
            runtime_role = iam.Role.from_role_arn(
                self, "RuntimeRole", runtime_role_arn, mutable=True
            )
            runtime_role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="RetrieveFromKnowledgeBase",
                    actions=["bedrock:Retrieve"],
                    resources=[self.knowledge_base.attr_knowledge_base_arn],
                )
            )
            runtime_role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="ReadSourceOfRecord",
                    actions=["s3:GetObject", "s3:ListBucket"],
                    resources=[self.bucket.bucket_arn, f"{self.bucket.bucket_arn}/*"],
                )
            )
            runtime_role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="ResolveBucketNameFromStackOutput",
                    # バケット名にアカウント ID が入るため設定に literal で書かず、
                    # core.py の resolve_bucket() がスタックの出力から引く（§10-13）
                    actions=["cloudformation:DescribeStacks"],
                    resources=[self.stack_id],
                )
            )

        # ------------------------------------------------------------------
        CfnOutput(
            self,
            "KnowledgeBucketName",
            value=self.bucket.bucket_name,
            description="正本を置く S3 バケット。KAI_KNOWLEDGE_BUCKET に設定する",
        )
        CfnOutput(
            self,
            "KnowledgeBaseId",
            value=self.knowledge_base.attr_knowledge_base_id,
            description="Managed KB の ID。KAI_KB_ID に設定する",
        )
        CfnOutput(
            self,
            "DataSourceId",
            value=self.data_source.attr_data_source_id,
            description="S3 データソースの ID。Ingestion job の起動に使う",
        )
