#!/usr/bin/env bash
set -euo pipefail

root=${CONTRACT_REVIEW_DEV_ROOT:-/home/aituge/workspace/contract-review-dev}
source_file=${1:?usage: contract_window_stage5_e2e.sh INPUT_PDF}
artifact_dir="$root/test-artifacts"
base_url=${CONTRACT_STAGE5_BASE_URL:-http://127.0.0.1:19200}
stamp=$(date +%Y%m%d-%H%M%S)
request_id="stage5-window-$stamp"
business_task_id="stage5-business-$stamp"
contract_version_id="stage5-version-$stamp"

set -a
source "$root/env/.env"
set +a

test -s "$source_file"
headers=(
  -H "X-Internal-Service: continew-java"
  -H "X-Internal-Token: $CONTRACT_INTERNAL_TOKEN"
  -H "X-User-Id: stage5-user"
  -H "X-Tenant-Id: 0"
  -H "X-Request-Id: $request_id"
)
payload=$(jq -nc \
  --arg business_task_id "$business_task_id" \
  --arg contract_version_id "$contract_version_id" \
  '{business_task_id:$business_task_id,contract_version_id:$contract_version_id,perspective:"PARTY_A",our_party_name:"Alpha Demo Company",contract_type:"AUTO",review_attitude:"NEUTRAL",schema_version:"1.0"}')

created=$(curl -fsS -X POST "$base_url/v1/contract-reviews" \
  "${headers[@]}" \
  -H "Idempotency-Key: stage5-window-$stamp" \
  -F "file=@$source_file;type=application/pdf" \
  -F "request=$payload;type=application/json")
printf '%s\n' "$created" >"$artifact_dir/stage5-window-created.json"
review_id=$(jq -er '.data.review_id' <<<"$created")

status=RUNNING
for _ in $(seq 1 240); do
  response=$(curl -fsS "$base_url/v1/contract-reviews/$review_id" "${headers[@]}")
  printf '%s\n' "$response" >"$artifact_dir/stage5-window-status.json"
  status=$(jq -er '.data.status' <<<"$response")
  case "$status" in
    SUCCEEDED|FAILED|CANCELLED) break ;;
  esac
  sleep 5
done

if [[ "$status" != "SUCCEEDED" ]]; then
  jq -n \
    --arg review_id "$review_id" \
    --arg status "$status" \
    --slurpfile current "$artifact_dir/stage5-window-status.json" \
    '{review_id:$review_id,status:$status,current:$current[0].data}' \
    >"$artifact_dir/stage5-window-e2e-summary.json"
  cat "$artifact_dir/stage5-window-e2e-summary.json"
  exit 1
fi

result=$(curl -fsS "$base_url/v1/contract-reviews/$review_id/result" "${headers[@]}")
printf '%s\n' "$result" >"$artifact_dir/stage5-window-result.json"
jq -n \
  --arg review_id "$review_id" \
  --arg status "$status" \
  --slurpfile current "$artifact_dir/stage5-window-status.json" \
  --slurpfile result "$artifact_dir/stage5-window-result.json" \
  '{
    review_id:$review_id,
    status:$status,
    current_stage:$current[0].data.current_stage,
    framework_attempt_no:$current[0].data.framework_attempt_no,
    framework_task_id:$current[0].data.framework_task_id,
    framework_run_id:$current[0].data.framework_run_id,
    result_hash:$result[0].data.result_hash,
    finding_count:($result[0].data.findings|length),
    evidence_count:($result[0].data.evidences|length),
    risk_counts:$result[0].data.summary,
    categories:($result[0].data.findings|map(.category)|group_by(.)|map({category:.[0],count:length})),
    relationships_empty:($result[0].data.relationships == []),
    bounding_boxes_empty:($result[0].data.evidences|all(.bounding_boxes == []))
  }' >"$artifact_dir/stage5-window-e2e-summary.json"
cat "$artifact_dir/stage5-window-e2e-summary.json"
