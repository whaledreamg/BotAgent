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

# 健康检查 HTTP 服务的监听地址。
#
# 默认 0.0.0.0 —— 跟 Agent / Arm / Vision / TTS 对齐, 否则远程机器上的
# 监控台访问 http://<本机IP>:8801/health 会直接连接被拒 (显示「断开」)。
#
# ⚠️ 0.0.0.0 会把 /health、/state、/v1/submit 都暴露到局域网。
#    /v1/submit 是无鉴权的语音注入入口 —— 不在可信网络里的话,
#    把 SERVER_HOST 改成具体内网 IP, 或用防火墙挡住 8801。
export SERVER_HOST="${SERVER_HOST:-0.0.0.0}"
export SERVER_PORT="${SERVER_PORT:-8801}"

mkdir -p "$LOG_DIR"
cd "$SERVICE_DIR"

echo "[asr] 启动 ASR 服务 (env=$CONDA_ENV, host=$SERVER_HOST, port=$SERVER_PORT) ..."
conda run --no-capture-output -n "$CONDA_ENV" \
    python server.py "$@" 2>&1 | tee -a "$LOG_DIR/asr.log"
