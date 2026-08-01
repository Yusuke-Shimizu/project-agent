#!/usr/bin/env bash
#
# デモ用リソースの後片付け（architecture_v1.md §11）。
#
# 支配的なコストは Runtime の起動時間と Bedrock のトークンで、KB と S3 は微々たるもの。
# それでも「止め忘れ」と「作り直し」の両方に効くので、KB を作る前からここに置いておく。
#
# 消す対象は**環境変数で明示されたものだけ**。名前の推測やワイルドカードでの一括削除は
# しない。未設定のものは黙って飛ばす。
#
#   KAI_RUNTIME_ID        AgentCore Runtime の ID（L1d）
#   KAI_KB_ID             Bedrock Knowledge Base の ID（L1b）
#   KAI_KNOWLEDGE_BUCKET  正本を置いた S3 バケット名（L1a）
#   AWS_REGION            既定 ap-northeast-1
#
# 使い方:
#   ./code/scripts/teardown.sh            # 消す対象を出して確認を取る
#   ./code/scripts/teardown.sh --dry-run  # 出すだけ
#   ./code/scripts/teardown.sh -y         # 確認なし
#
# CloudWatch Transaction Search はアカウント・リージョン単位の一回きりの設定なので
# ここでは触らない（消すと次回また有効化して10分待つことになる）。止めたい場合は
#   aws xray update-trace-segment-destination --destination XRay
#
set -euo pipefail

REGION="${AWS_REGION:-ap-northeast-1}"
DRY_RUN=false
ASSUME_YES=false

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    -y|--yes)  ASSUME_YES=true ;;
    -h|--help) sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "不明な引数: $arg" >&2; exit 2 ;;
  esac
done

# 存在が確認できたものだけを積む
targets=()

if [[ -n "${KAI_RUNTIME_ID:-}" ]]; then
  if aws bedrock-agentcore-control get-agent-runtime \
       --agent-runtime-id "$KAI_RUNTIME_ID" --region "$REGION" >/dev/null 2>&1; then
    targets+=("runtime:$KAI_RUNTIME_ID")
  else
    echo "skip: Runtime $KAI_RUNTIME_ID は見つからない（削除済み？）"
  fi
fi

if [[ -n "${KAI_KB_ID:-}" ]]; then
  if aws bedrock-agent get-knowledge-base \
       --knowledge-base-id "$KAI_KB_ID" --region "$REGION" >/dev/null 2>&1; then
    targets+=("kb:$KAI_KB_ID")
  else
    echo "skip: Knowledge Base $KAI_KB_ID は見つからない（削除済み？）"
  fi
fi

if [[ -n "${KAI_KNOWLEDGE_BUCKET:-}" ]]; then
  if aws s3api head-bucket --bucket "$KAI_KNOWLEDGE_BUCKET" >/dev/null 2>&1; then
    n=$(aws s3 ls "s3://$KAI_KNOWLEDGE_BUCKET" --recursive --summarize 2>/dev/null \
        | awk '/Total Objects:/ {print $3}')
    targets+=("bucket:$KAI_KNOWLEDGE_BUCKET (${n:-?} オブジェクト)")
  else
    echo "skip: バケット $KAI_KNOWLEDGE_BUCKET は見つからない（削除済み？）"
  fi
fi

if [[ ${#targets[@]} -eq 0 ]]; then
  echo "消すものは無い。"
  echo "（環境変数 KAI_RUNTIME_ID / KAI_KB_ID / KAI_KNOWLEDGE_BUCKET が未設定なら、それが理由）"
  exit 0
fi

echo
echo "以下を削除する（リージョン: $REGION）:"
printf '  - %s\n' "${targets[@]}"
echo
echo "S3 の中身は git の knowledge_base/ が正本なので、消しても seed_knowledge.py で戻せる。"

if [[ "$DRY_RUN" == true ]]; then
  echo "(--dry-run のため何もしない)"
  exit 0
fi

if [[ "$ASSUME_YES" != true ]]; then
  read -r -p "本当に削除する？ [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "中止した。"; exit 1; }
fi

# 依存の逆順に消す: Runtime → KB → S3
for target in "${targets[@]}"; do
  case "$target" in
    runtime:*)
      id="${target#runtime:}"
      echo "削除中: Runtime $id"
      aws bedrock-agentcore-control delete-agent-runtime \
        --agent-runtime-id "$id" --region "$REGION" >/dev/null
      ;;
    kb:*)
      id="${target#kb:}"
      echo "削除中: Knowledge Base $id"
      aws bedrock-agent delete-knowledge-base \
        --knowledge-base-id "$id" --region "$REGION" >/dev/null
      ;;
    bucket:*)
      name="${target#bucket:}"; name="${name%% *}"
      echo "削除中: s3://$name"
      aws s3 rm "s3://$name" --recursive >/dev/null
      aws s3api delete-bucket --bucket "$name" --region "$REGION"
      ;;
  esac
done

echo
echo "完了。CloudWatch のログ（/aws/bedrock-agentcore/... と aws/spans）は残っている。"
echo "保持期間で自然に消えるが、すぐ消したいなら aws logs delete-log-group で。"
