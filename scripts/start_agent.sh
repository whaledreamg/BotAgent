#!/usr/bin/env bash
# 启动 Agent 服务 (conda 环境 botagent)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="$ROOT/services/agent_service"
CONDA_ENV="${AGENT_CONDA_ENV:-botagent}"
LOG_DIR="$ROOT/logs"

export AGENT_HOST="${AGENT_HOST:-0.0.0.0}"
export AGENT_PORT="${AGENT_PORT:-8800}"

mkdir -p "$LOG_DIR"
cd "$SERVICE_DIR"

echo "[agent] 启动 Agent 服务 (env=$CONDA_ENV, port=$AGENT_PORT) ..."
conda run --no-capture-output -n "$CONDA_ENV" \
    uvicorn app:app --host "$AGENT_HOST" --port "$AGENT_PORT" 2>&1 | tee -a "$LOG_DIR/agent.log"
