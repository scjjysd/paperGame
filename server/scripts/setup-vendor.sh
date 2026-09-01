#!/usr/bin/env bash
# 拉取 AnimatedDrawings 源码到 vendor/，供 docker-compose 构建 TorchServe 镜像。
# 幂等：已存在则跳过；若已存在但非 git 仓库则报错提示人工处理。
set -euo pipefail

SERVER_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR_DIR="${SERVER_DIR}/vendor/AnimatedDrawings"
REPO_URL="https://github.com/facebookresearch/AnimatedDrawings.git"
PINNED_COMMIT=""  # 如需锁定版本填入 commit 哈希（项目已归档，行为稳定，可锁可不锁）

if [ -d "${VENDOR_DIR}" ]; then
  if [ -d "${VENDOR_DIR}/.git" ]; then
    echo "vendor 已存在，跳过克隆：${VENDOR_DIR}"
  else
    echo "错误：${VENDOR_DIR} 存在但不是 git 仓库，请人工处理后重试" >&2
    exit 1
  fi
else
  echo "克隆 AnimatedDrawings -> ${VENDOR_DIR}"
  git clone --depth 1 "${REPO_URL}" "${VENDOR_DIR}"
fi

if [ -n "${PINNED_COMMIT}" ]; then
  git -C "${VENDOR_DIR}" fetch --depth 1 origin "${PINNED_COMMIT}"
  git -C "${VENDOR_DIR}" checkout "${PINNED_COMMIT}"
fi

echo "完成。下一步：cd ${SERVER_DIR} && docker compose up -d --build"
