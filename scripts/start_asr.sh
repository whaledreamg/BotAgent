#!/usr/bin/env bash
# 启动 ASR 服务 (conda 环境 asrt)
#
# ASR 是语音链路第一环: 麦克风 -> 唤醒词 -> 识别。
# 可选参数会透传给 server.py, 例如:
#   ./scripts/start_asr.sh --wake "你好助手"
#   ./scripts/start_asr.sh --replay /path/to/test.wav
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="$ROOT/services/asr_service"
CONDA_ENV="${ASR_CONDA_ENV:-asrt}"
LOG_DIR="$ROOT/logs"

mkdir -p "$LOG_DIR"
cd "$SERVICE_DIR"

echo "[asr] 启动 ASR 服务 (env=$CONDA_ENV) ..."
conda run --no-capture-output -n "$CONDA_ENV" \
    python server.py "$@" 2>&1 | tee -a "$LOG_DIR/asr.log"
