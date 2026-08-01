"""正本の置き場（S3）と、その索引（Managed KB）。

architecture_v1.md §5.1 の prefix 分離をここで物理的に表現する。

    s3://<bucket>/
    ├── knowledge_base/   ← KB のデータソースはこの prefix だけ
    └── raw/              ← 提供された Excel / PPT / PDF。KB には取り込まない

L1a では S3 バケットだけ。Managed KB は L1b でこのスタックに足す。
"""

from aws_cdk import Aws, CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_s3 as s3
from constructs import Construct


class KnowledgeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

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

        CfnOutput(
            self,
            "KnowledgeBucketName",
            value=self.bucket.bucket_name,
            description="正本を置く S3 バケット。KAI_KNOWLEDGE_BUCKET に設定する",
        )
