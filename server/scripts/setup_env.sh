#!/usr/bin/env bash
# 创建宿主侧 Python 环境并以可编辑模式安装 vendor 的 AnimatedDrawings。
set -euo pipefail
SERVER_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR_DIR="${SERVER_DIR}/vendor/AnimatedDrawings"

[ -d "${VENDOR_DIR}" ] || { echo "先运行 scripts/setup-vendor.sh" >&2; exit 1; }

python3 -m venv "${SERVER_DIR}/.venv"
source "${SERVER_DIR}/.venv/bin/activate"
pip install --upgrade pip
pip install -e "${VENDOR_DIR}"
pip install -r "${SERVER_DIR}/requirements-dev.txt"
echo "完成。激活环境：source ${SERVER_DIR}/.venv/bin/activate"
