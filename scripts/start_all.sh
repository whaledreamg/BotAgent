#!/usr/bin/env bash
# 一键启动全部三个服务 (TTS -> Agent -> ASR), 并做健康检查
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/logs"
TTS_PORT="${TTS_PORT:-8000}"
AGENT_PORT="${AGENT_PORT:-8800}"
ASR_PORT="${ASR_PORT:-8801}"

mkdir -p "$LOG_DIR"
cd "$ROOT"

wait_health() {
    local name="$1" url="$2" tries="${3:-60}"
    for _ in $(seq 1 "$tries"); do
        if curl -sf --max-time 3 "$url" >/dev/null 2>&1; then
            echo "[ok] $name 就绪: $url"
            return 0
        fi
        sleep 2
    done
    echo "[warn] $name 健康检查超时: $url" >&2
    return 1
}

# 端口被占用时先报出来 —— 否则旧进程会顶掉新进程,
# 健康检查却依然通过, 导致测的是旧配置。
check_port_free() {
    local name="$1" port="$2"
    if ss -ltnH 2>/dev/null | grep -q ":${port} "; then
        echo "[error] $name 端口 $port 已被占用, 请先执行 scripts/stop_all.sh" >&2
        ss -ltnpH 2>/dev/null | grep ":${port} " >&2 || true
        return 1
    fi
    return 0
}

echo "=== 1/3 启动 TTS ==="
check_port_free tts "$TTS_PORT" || exit 1
nohup "$ROOT/scripts/start_tts.sh" >"$LOG_DIR/tts.out" 2>&1 &
echo "$!" > "$LOG_DIR/tts.pid"
wait_health "tts" "http://127.0.0.1:${TTS_PORT}/health" 90 || true

echo "=== 2/3 启动 Agent ==="
check_port_free agent "$AGENT_PORT" || exit 1
nohup "$ROOT/scripts/start_agent.sh" >"$LOG_DIR/agent.out" 2>&1 &
echo "$!" > "$LOG_DIR/agent.pid"
wait_health "agent" "http://127.0.0.1:${AGENT_PORT}/health" 30 || true

echo "=== 3/3 启动 ASR ==="
check_port_free asr "$ASR_PORT" || exit 1
nohup "$ROOT/scripts/start_asr.sh" "$@" >"$LOG_DIR/asr.out" 2>&1 &
echo "$!" > "$LOG_DIR/asr.pid"
wait_health "asr" "http://127.0.0.1:${ASR_PORT}/health" 30 || true

echo
echo "全部服务已启动, 日志目录: $LOG_DIR"
echo "  TTS   http://127.0.0.1:${TTS_PORT}/health"
echo "  Agent http://127.0.0.1:${AGENT_PORT}/health"
echo "  ASR   http://127.0.0.1:${ASR_PORT}/health"
