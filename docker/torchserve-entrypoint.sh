#!/bin/sh
set -eu

base_config="${TORCHSERVE_BASE_CONFIG:-/home/torchserve/config.properties}"
runtime_config="${TORCHSERVE_RUNTIME_CONFIG:-/tmp/torchserve-config.properties}"
torchserve_bin="${TORCHSERVE_BIN:-/opt/conda/bin/torchserve}"
workers="${TORCHSERVE_WORKERS_PER_MODEL:-}"

cp "$base_config" "$runtime_config"

if [ -n "$workers" ]; then
    case "$workers" in
        *[!0-9]*)
            echo "TORCHSERVE_WORKERS_PER_MODEL 必须是正整数，当前值：$workers" >&2
            exit 2
            ;;
    esac
    if [ "$workers" -le 0 ]; then
        echo "TORCHSERVE_WORKERS_PER_MODEL 必须大于 0，当前值：$workers" >&2
        exit 2
    fi
    printf 'default_workers_per_model=%s\n' "$workers" >> "$runtime_config"
    echo "TorchServe 每模型 worker 数：$workers"
else
    echo "TorchServe 每模型 worker 数：未配置，使用原生默认"
fi

exec "$torchserve_bin" --start --foreground --disable-token-auth --ts-config "$runtime_config"
