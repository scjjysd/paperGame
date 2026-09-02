#!/usr/bin/env bash
# 全栈端到端冒烟：上传 -> 轮询 -> ready -> 下载精灵表验证 PNG。
# 前置：docker compose up -d --build 已完成且 4 容器健康。
set -euo pipefail
cd "$(dirname "$0")/.."

API="${API_BASE:-http://localhost:8000}"
SAMPLE="${1:-../testdata/characters/s01.png}"   # s01 与官方示例 garlic.png 内容相同（MD5 一致）
TIMEOUT_SEC=180

echo "== 健康检查 =="
curl -sf "$API/healthz" > /dev/null
curl -sf http://localhost:8080/ping > /dev/null

echo "== 上传 $SAMPLE =="
JOB_ID=$(curl -sf -F "file=@$SAMPLE" "$API/v1/characters" | python3 -c 'import sys,json; print(json.load(sys.stdin)["jobId"])')
echo "jobId=$JOB_ID"

echo "== 轮询（最长 ${TIMEOUT_SEC}s）=="
START=$(date +%s)
while true; do
  BODY=$(curl -sf "$API/v1/characters/$JOB_ID")
  STATUS=$(echo "$BODY" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')
  ELAPSED=$(( $(date +%s) - START ))
  echo "  [${ELAPSED}s] status=$STATUS"
  case "$STATUS" in
    ready) break ;;
    needs_correction|failed) echo "FAIL: 终态异常 $BODY"; exit 1 ;;
  esac
  [ "$ELAPSED" -gt "$TIMEOUT_SEC" ] && { echo "FAIL: 超时"; exit 1; }
  sleep 5
done

echo "== 下载精灵表并验证 PNG =="
for M in run jump; do
  URL=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['animations']['$M']['spriteSheetUrl'])")
  curl -sf "$API$URL" -o "/tmp/smoke_$M.png"
  python3 -c "
from PIL import Image
im = Image.open('/tmp/smoke_$M.png')
assert im.mode == 'RGBA', im.mode
print('$M sheet:', im.size, im.mode)
"
done

echo "== 幂等验证：同图重复提交 =="
JOB_ID2=$(curl -sf -F "file=@$SAMPLE" "$API/v1/characters" | python3 -c 'import sys,json; print(json.load(sys.stdin)["jobId"])')
[ "$JOB_ID" = "$JOB_ID2" ] || { echo "FAIL: 幂等被破坏"; exit 1; }

echo "SMOKE_E2E_PASS jobId=$JOB_ID elapsed=${ELAPSED}s"
