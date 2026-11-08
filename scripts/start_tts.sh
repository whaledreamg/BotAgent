#!/usr/bin/env bash
# 启动 TTS 服务 (ChatTTS, conda 环境 chattts)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="$ROOT/services/tts_service"
CONDA_ENV="${TTS_CONDA_ENV:-chattts}"
LOG_DIR="$ROOT/logs"

export TTS_HOST="${TTS_HOST:-0.0.0.0}"
export TTS_PORT="${TTS_PORT:-8000}"
# 播放统一由 ASR 侧负责, 服务端不再本地播报, 避免回声与串扰
export TTS_PLAY="${TTS_PLAY:-0}"

mkdir -p "$LOG_DIR"
cd "$SERVICE_DIR"

echo "[tts] 启动 ChatTTS 服务 (env=$CONDA_ENV, port=$TTS_PORT) ..."
conda run --no-capture-output -n "$CONDA_ENV" \
    python examples/ws_server/main.py 2>&1 | tee -a "$LOG_DIR/tts.log"
