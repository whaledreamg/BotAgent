#!/usr/bin/env bash
# 启动机械臂动作服务 (conda 环境 botagent)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="$ROOT/services/arm_service"
CONDA_ENV="${ARM_CONDA_ENV:-botagent}"
LOG_DIR="$ROOT/logs"

export ARM_SERVER_HOST="${ARM_SERVER_HOST:-0.0.0.0}"
export ARM_SERVER_PORT="${ARM_SERVER_PORT:-8802}"
# 控制器地址以 configs/arm.yaml 为准, 这里不再硬编码默认值。
# 只有外部显式设置 ARM_CONTROLLER_WS 时才用环境变量覆盖 (配置优先级:
# 默认值 < configs/arm.yaml < 环境变量)。
if [ -n "${ARM_CONTROLLER_WS:-}" ]; then
    export ARM_CONTROLLER_WS
    echo "[arm] 控制器地址由环境变量指定: $ARM_CONTROLLER_WS"
fi

mkdir -p "$LOG_DIR"
cd "$SERVICE_DIR"

echo "[arm] 启动机械臂动作服务 (env=$CONDA_ENV, port=$ARM_SERVER_PORT) ..."
conda run --no-capture-output -n "$CONDA_ENV" \
    python main.py 2>&1 | tee -a "$LOG_DIR/arm.log"
