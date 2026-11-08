#!/usr/bin/env bash
# 完整链路验证: 三个服务一起跑
#   唤醒词 -> 真实录音问句 -> ASR -> Agent 分流 -> TTS -> 播放, 并检查 /state
#
# 用法: ./scripts/full_chain_test.sh [真实录音路径]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TTS_PORT=8000
AGENT_PORT=8800
ASR_PORT=8801
FAILED=0

ok()   { echo "  [PASS] $*"; }
fail() { echo "  [FAIL] $*"; FAILED=1; }

cleanup() {
    for pid in "${TTS_PID:-}" "${AGENT_PID:-}" "${ASR_PID:-}"; do
        [[ -n "$pid" ]] && kill "$pid" 2>/dev/null
    done
    wait 2>/dev/null
}
trap cleanup EXIT

# 清掉上一轮遗留的进程, 避免端口占用导致服务没起来
# 注意: 服务可能是通过 `conda run` 起的, 父进程名不包含关键字,
# 所以要按端口回收, 不能只靠 pkill 匹配命令行
echo "=== 清理遗留进程 ==="
pkill -f "ws_server/main.py" 2>/dev/null
pkill -f "uvicorn app:app" 2>/dev/null
pkill -f "uvicorn app" 2>/dev/null
pkill -f "server.py --replay" 2>/dev/null
pkill -f "services/asr_service/server.py" 2>/dev/null
pkill -f "services/agent_service/app.py" 2>/dev/null
free_port() {
    local port="$1"
    local pids
    pids="$(ss -ltnpH "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u)"
    if [[ -n "$pids" ]]; then
        echo "  释放端口 $port (pid: $(echo "$pids" | tr '\n' ' '))"
        kill -9 $pids 2>/dev/null
    fi
}
free_port "$TTS_PORT"
free_port "$AGENT_PORT"
free_port "$ASR_PORT"
sleep 2

echo "=== 启动 TTS ==="
cd "$ROOT/services/tts_service"
/home/gxh/miniconda3/envs/chattts/bin/python examples/ws_server/main.py > /tmp/fc_tts.log 2>&1 &
TTS_PID=$!
for i in $(seq 1 60); do
    curl -sf --max-time 2 "http://127.0.0.1:${TTS_PORT}/health" >/dev/null 2>&1 && break
    sleep 2
done
curl -sf --max-time 5 "http://127.0.0.1:${TTS_PORT}/health" | head -c 200; echo

echo "=== 启动 Agent ==="
cd "$ROOT/services/agent_service"
/home/gxh/miniconda3/envs/botagent/bin/python -m uvicorn app:app --host 127.0.0.1 --port "${AGENT_PORT}" > /tmp/fc_agent.log 2>&1 &
AGENT_PID=$!
for i in $(seq 1 30); do
    curl -sf --max-time 2 "http://127.0.0.1:${AGENT_PORT}/health" >/dev/null 2>&1 && break
    sleep 1
done
curl -sf --max-time 5 "http://127.0.0.1:${AGENT_PORT}/health" | head -c 200; echo

echo "=== 生成测试 wav ==="
REAL="${1:-/home/gxh/workspace/project/ASRT/recordings/rec_20261106_182400.wav}"
cd "$ROOT/services/tts_service"
/home/gxh/miniconda3/envs/chattts/bin/python tools/make_test_wav.py \
    --out /tmp/fc_test_16k.wav --real "$REAL" --skip-wake-audio | tail -3

echo "=== 启动 ASR (回放模式, 跳过唤醒词以验证下游链路) ==="
cd "$ROOT/services/asr_service"
/home/gxh/miniconda3/envs/asrt/bin/python server.py \
    --replay /tmp/fc_test_16k.wav --no-wake --keep-alive 60 \
    --host 127.0.0.1 --port "${ASR_PORT}" > /tmp/fc_asr.log 2>&1 &
ASR_PID=$!
# 等这一轮 (识别 + 分流 + 朗读) 跑完, 最多 150s
STATE=""
for i in $(seq 1 75); do
    STATE="$(curl -sf --max-time 2 "http://127.0.0.1:${ASR_PORT}/state" 2>/dev/null || true)"
    if echo "$STATE" | grep -q '"last_intent": *"chat"'; then
        break
    fi
    sleep 2
done
echo "=== ASR 状态 ==="
STATE="$(curl -sf --max-time 5 "http://127.0.0.1:${ASR_PORT}/state" || true)"
echo "$STATE" | head -c 1200; echo
if echo "$STATE" | grep -q '"last_text": *"[^"]'; then
    ok "ASR 已记录识别文本"
else
    fail "ASR 未记录识别文本"
fi
if echo "$STATE" | grep -q '"last_intent": *"chat"'; then
    ok "分流到 chat 分支"
else
    fail "未分流到 chat"
fi
if echo "$STATE" | grep -q '"last_speak": *true'; then
    ok "speak=true (走了 TTS 朗读)"
else
    fail "speak 不为 true"
fi
if echo "$STATE" | grep -q '"last_reply": *"[^"]'; then
    ok "已生成回复文本"
else
    fail "无回复文本"
fi

echo "=== 分支验证: 执行型工具不应进 TTS ==="
TOOL_STATE="$(curl -sf --max-time 60 -X POST "http://127.0.0.1:${AGENT_PORT}/v1/dispatch" \
    -H 'Content-Type: application/json' \
    -d '{"text":"打开空调","session_id":"e2e-tool"}' || true)"
echo "$TOOL_STATE" | head -c 400; echo
if echo "$TOOL_STATE" | grep -q '"speak": *false'; then
    ok "执行型工具 speak=false (不走 TTS)"
else
    fail "执行型工具 speak 不为 false"
fi
if echo "$TOOL_STATE" | grep -q '"intent": *"tool"'; then
    ok "工具类意图识别为 tool"
else
    fail "工具类意图识别错误"
fi

echo "=== 分支验证: 输出型工具结果要进 TTS ==="
QUERY_STATE="$(curl -sf --max-time 60 -X POST "http://127.0.0.1:${AGENT_PORT}/v1/dispatch" \
    -H 'Content-Type: application/json' \
    -d '{"text":"现在几点","session_id":"e2e-query"}' || true)"
echo "$QUERY_STATE" | head -c 400; echo
if echo "$QUERY_STATE" | grep -q '"speak": *true'; then
    ok "输出型工具 speak=true (结果经 TTS 播报)"
else
    fail "输出型工具 speak 不为 true"
fi
if echo "$QUERY_STATE" | grep -q '"intent": *"tool"'; then
    ok "输出型工具意图识别为 tool"
else
    fail "输出型工具意图识别错误"
fi

echo "=== 不落盘校验 ==="
if find "$ROOT/services" -newermt "-5 minutes" \
       \( -name "*.wav" -o -name "*.pcm" -o -name "*.mp3" \) 2>/dev/null | grep -q .; then
    fail "检测到新生成的音频文件 (不应落盘)"
    find "$ROOT/services" -newermt "-5 minutes" \
         \( -name "*.wav" -o -name "*.pcm" -o -name "*.mp3" \) 2>/dev/null | head
else
    ok "全程未产生任何音频文件"
fi

echo "=== ASR 日志 ==="
grep -aE "状态|识别|分流|朗读|错误|TTS|唤醒" /tmp/fc_asr.log | tail -20

echo
if [[ "$FAILED" == "0" ]]; then
    echo "结果: 全部通过"
else
    echo "结果: 存在失败项"
fi
exit "$FAILED"
