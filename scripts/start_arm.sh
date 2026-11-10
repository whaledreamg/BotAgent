#!/usr/bin/env bash
# 启动机械臂动作服务 (conda 环境 botagent)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="$ROOT/services/arm_service"
CONDA_ENV="${ARM_CONDA_ENV:-botagent}"
LOG_DIR="$ROOT/logs"

export ARM_SERVER_HOST="${ARM_SERVER_HOST:-0.0.0.0}"
export ARM_SERVER_PORT="${ARM_SERVER_PORT:-8802}"
export ARM_CONTROLLER_WS="${ARM_CONTROLLER_WS:-ws://192.168.100.16:9000}"

mkdir -p "$LOG_DIR"
cd "$SERVICE_DIR"

echo "[arm] 启动机械臂动作服务 (env=$CONDA_ENV, port=$ARM_SERVER_PORT) ..."
conda run --no-capture-output -n "$CONDA_ENV" \
    python main.py 2>&1 | tee -a "$LOG_DIR/arm.log"
