#!/usr/bin/env bash
# 启动 Agent 服务 (conda 环境 botagent)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="$ROOT/services/agent_service"
CONDA_ENV="${AGENT_CONDA_ENV:-botagent}"
LOG_DIR="$ROOT/logs"

export AGENT_HOST="${AGENT_HOST:-0.0.0.0}"
export AGENT_PORT="${AGENT_PORT:-8800}"

# 调试开关 (都默认关):
#   LLM_DEBUG=1    -> 日志里打印发给 LLM 的完整 prompt 和模型原始返回
#   AGENT_DEBUG=1  -> 日志里打印「输入 / 分流 trace / 回复 / action」
export LLM_DEBUG="${LLM_DEBUG:-0}"
export AGENT_DEBUG="${AGENT_DEBUG:-0}"

mkdir -p "$LOG_DIR"
cd "$SERVICE_DIR"

echo "[agent] 启动 Agent 服务 (env=$CONDA_ENV, port=$AGENT_PORT) ..."
conda run --no-capture-output -n "$CONDA_ENV" \
    uvicorn app:app --host "$AGENT_HOST" --port "$AGENT_PORT" 2>&1 | tee -a "$LOG_DIR/agent.log"
