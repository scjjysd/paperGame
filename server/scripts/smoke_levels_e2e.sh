#!/usr/bin/env bash
# 关卡全栈端到端冒烟：上传、轮询、契约产物校验与幂等复验。
set -euo pipefail
cd "$(dirname "$0")/.."

API="${API_BASE:-http://localhost:8000}"
SAMPLE="${1:-../testdata/levels/synthetic/front.png}"
TIMEOUT_SEC="${TIMEOUT_SEC:-210}"
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/smoke-levels.XXXXXX")
trap 'rm -rf "$TMP_DIR"' EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
json_field() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

[ -f "$SAMPLE" ] || fail "样本不存在: $SAMPLE"
curl -sf "$API/healthz" >/dev/null || fail "API 健康检查失败: $API/healthz"

printf '== 上传 %s ==\n' "$SAMPLE"
BODY=$(curl -sf -F "file=@$SAMPLE" "$API/v1/levels") || fail "上传失败"
JOB_ID=$(printf '%s' "$BODY" | json_field '["jobId"]')
printf 'jobId=%s\n' "$JOB_ID"

printf '== 轮询（最长 %ss）==\n' "$TIMEOUT_SEC"
START=$(date +%s)
while true; do
  BODY=$(curl -sf "$API/v1/levels/$JOB_ID") || fail "查询任务失败"
  STATUS=$(printf '%s' "$BODY" | json_field '["status"]')
  ELAPSED=$(( $(date +%s) - START ))
  printf '  [%ss] status=%s\n' "$ELAPSED" "$STATUS"
  case "$STATUS" in
    ready) break ;;
    needs_fix|needs_review|failed) fail "终态异常 $BODY" ;;
    queued|processing) ;;
    *) fail "未知状态 $STATUS" ;;
  esac
  [ "$ELAPSED" -le "$TIMEOUT_SEC" ] || fail "轮询超时"
  sleep 2
done

printf '%s' "$BODY" >"$TMP_DIR/result.json"
python3 - "$TMP_DIR/result.json" <<'PY'
import json
import sys

body = json.load(open(sys.argv[1], encoding='utf-8'))
result = body['result']
level = result['level']
canvas = level['canvas']
background = level['background']
assert background['width'] == canvas['width']
assert background['height'] == canvas['height']
width, height = canvas['width'], canvas['height']

def point_inside(point):
    return 0 <= point['x'] < width and 0 <= point['y'] < height

assert point_inside(level['playerStart'])
for platform in level['platforms']:
    assert point_inside(platform['start'])
    assert point_inside(platform['end'])
goal = level['goalRegion']
assert 0 <= goal['x'] and 0 <= goal['y']
assert goal['x'] + goal['width'] <= width
assert goal['y'] + goal['height'] <= height
artifacts = result['artifacts']
for name in ('rectifiedImageUrl', 'levelJsonUrl', 'analysisJsonUrl'):
    assert artifacts[name], name
print('契约坐标与背景尺寸有效')
PY

printf '== 下载并验证三份契约产物 ==\n'
for SPEC in 'rectifiedImageUrl rectified.png' 'levelJsonUrl level.json' 'analysisJsonUrl analysis.json'; do
  set -- $SPEC
  URL=$(printf '%s' "$BODY" | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['artifacts']['$1'])")
  curl -sf "$API$URL" -o "$TMP_DIR/$2" || fail "下载 $1 失败"
done
printf '== 校验下载产物契约 ==\n'
python3 - "$TMP_DIR" <<'PY'
import json
import sys
from pathlib import Path
from PIL import Image

root = Path(sys.argv[1])
level = json.loads((root / 'level.json').read_text(encoding='utf-8'))
analysis = json.loads((root / 'analysis.json').read_text(encoding='utf-8'))
canvas = level['canvas']
background = level['background']
assert background['width'] == canvas['width']
assert background['height'] == canvas['height']
width, height = canvas['width'], canvas['height']

def point_inside(point):
    return 0 <= point['x'] < width and 0 <= point['y'] < height

assert point_inside(level['playerStart'])
for platform in level['platforms']:
    assert point_inside(platform['start'])
    assert point_inside(platform['end'])
goal = level['goalRegion']
assert 0 <= goal['x'] and 0 <= goal['y']
assert goal['x'] + goal['width'] <= width
assert goal['y'] + goal['height'] <= height
with Image.open(root / 'rectified.png') as image:
    image.load()
    assert image.size == (width, height), (image.size, (width, height))
assert analysis['playability'] in ('playable', 'unreachable')
assert isinstance(analysis['profile'], dict) and analysis['profile'].get('profileVersion')
assert isinstance(analysis['path'], list)
assert isinstance(analysis['warnings'], list)
print('下载关卡 JSON 坐标、背景和分析契约有效')
PY

printf '== 幂等验证：同图重复提交 ==\n'
BODY2=$(curl -sf -F "file=@$SAMPLE" "$API/v1/levels") || fail "重复上传失败"
JOB_ID2=$(printf '%s' "$BODY2" | json_field '["jobId"]')
[ "$JOB_ID" = "$JOB_ID2" ] || fail "幂等被破坏: $JOB_ID != $JOB_ID2"

printf 'SMOKE_LEVELS_E2E_PASS jobId=%s elapsed=%ss\n' "$JOB_ID" "$ELAPSED"
