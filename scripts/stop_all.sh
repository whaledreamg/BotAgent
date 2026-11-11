#!/usr/bin/env bash
# 停止所有服务
#
# 两种方式都要做:
# 1. pid 文件里的进程 (正常启动路径)
# 2. 占用端口的进程 (兜底: 手动启动或 pid 文件丢失时残留的孤儿进程)
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/logs"

TTS_PORT="${TTS_PORT:-8000}"
AGENT_PORT="${AGENT_PORT:-8800}"
ASR_PORT="${ASR_PORT:-8801}"
ARM_PORT="${ARM_PORT:-8802}"
VISION_PORT="${VISION_PORT:-8803}"

stop_one() {
    local name="$1" pidfile="$LOG_DIR/$1.pid"
    if [[ ! -f "$pidfile" ]]; then
        echo "[skip] $name 没有 pid 文件"
        return
    fi
    local pid
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
        echo "[stop] $name (pid=$pid)"
        # 连同子进程一起停
        pkill -TERM -P "$pid" 2>/dev/null || true
    fi
    kill -TERM "$pid" 2>/dev/null || true
    sleep 1
    kill -KILL "$pid" 2>/dev/null || true
    rm -f "$pidfile"
}

# 按端口清理残留进程。conda run 会再 fork 一层,
# 子进程的 pid 与 pid 文件里的不一致, 所以必须按端口兜底。
stop_port() {
    local name="$1" port="$2"
    local pids
    pids="$(ss -ltnpH 2>/dev/null | grep ":${port} " | grep -oP 'pid=\K[0-9]+' | sort -u)"
    if [[ -z "$pids" ]]; then
        return
    fi
    for pid in $pids; do
        echo "[stop] $name 端口 $port 上的残留进程 (pid=$pid)"
        kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 1
    for pid in $pids; do
        kill -KILL "$pid" 2>/dev/null || true
    done
}

stop_one tts
stop_one agent
stop_one asr
stop_one arm
stop_one vision

stop_port tts "$TTS_PORT"
stop_port agent "$AGENT_PORT"
stop_port asr "$ASR_PORT"
stop_port arm "$ARM_PORT"
stop_port vision "$VISION_PORT"

sleep 1
if ss -ltnpH 2>/dev/null | grep -qE ":(${TTS_PORT}|${AGENT_PORT}|${ASR_PORT}|${ARM_PORT}|${VISION_PORT}) "; then
    echo "[warn] 仍有端口被占用:" >&2
    ss -ltnpH 2>/dev/null | grep -E ":(${TTS_PORT}|${AGENT_PORT}|${ASR_PORT}|${ARM_PORT}|${VISION_PORT}) " >&2
else
    echo "[ok] 端口 ${TTS_PORT}/${AGENT_PORT}/${ASR_PORT}/${ARM_PORT}/${VISION_PORT} 均已释放"
fi

echo "[done] 已停止全部服务"
