#!/usr/bin/env bash
# 三段式联通验证
#   1) TTS 服务检查
#   2) Agent 分流检查 (chat -> speak=true, tool -> speak=false)
#   3) ASR replay 完整链路 (麦克风 -> ASR -> Agent -> TTS -> 播放), 确认不落盘
#
# 用法: ./scripts/smoke_test.sh [测试wav路径]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TTS_PORT="${TTS_PORT:-8000}"
AGENT_PORT="${AGENT_PORT:-8800}"
ASR_PORT="${ASR_PORT:-8801}"
AGENT_URL="http://127.0.0.1:${AGENT_PORT}"

FAILED=0
ok()   { echo "  [PASS] $*"; }
fail() { echo "  [FAIL] $*"; FAILED=1; }

echo "=========================================="
echo " 1/3 TTS 服务检查"
echo "=========================================="
HEALTH="$(curl -sf --max-time 5 "http://127.0.0.1:${TTS_PORT}/health" || true)"
echo "$HEALTH"
if echo "$HEALTH" | grep -q '"model_loaded": *true'; then
    ok "TTS 模型已加载"
else
    fail "TTS 未就绪 (或 model_loaded=false)"
fi

echo
echo "=========================================="
echo " 2/3 Agent 分流检查"
echo "=========================================="
resp="$(curl -sf --max-time 60 -X POST "${AGENT_URL}/v1/dispatch" \
    -H 'Content-Type: application/json' \
    -d '{"session_id":"smoke","text":"你好，今天天气怎么样"}' || true)"
echo "  chat 输入 -> $resp"
if echo "$resp" | grep -q '"intent": *"chat"'; then
    ok "闲聊分流到 chat"
else
    fail "闲聊未分流到 chat"
fi
if echo "$resp" | grep -q '"speak": *true'; then
    ok "chat 分支 speak=true (会走 TTS)"
else
    fail "chat 分支 speak 不为 true"
fi

resp="$(curl -sf --max-time 60 -X POST "${AGENT_URL}/v1/dispatch" \
    -H 'Content-Type: application/json' \
    -d '{"session_id":"smoke","text":"打开空调"}' || true)"
echo "  tool 输入 -> $resp"
if echo "$resp" | grep -q '"intent": *"tool"'; then
    ok "动作指令分流到 tool"
else
    fail "动作指令未分流到 tool"
fi
if echo "$resp" | grep -q '"speak": *false'; then
    ok "tool 分支 speak=false (不走 TTS)"
else
    fail "tool 分支 speak 不为 false"
fi

echo
echo "=========================================="
echo " 3/3 ASR 完整链路 (replay)"
echo "=========================================="
WAV="${1:-}"
if [[ -z "$WAV" ]]; then
    echo "  [SKIP] 未提供测试 wav 路径, 跳过完整链路"
    echo "         用法: ./scripts/smoke_test.sh /path/to/16k_mono.wav"
elif [[ ! -f "$WAV" ]]; then
    fail "wav 不存在: $WAV"
else
    REC_DIR="$ROOT/services/asr_service/recordings"
    BEFORE_COUNT="$(find "$REC_DIR" -name '*.wav' 2>/dev/null | wc -l)"
    echo "  回放: $WAV"
    conda run --no-capture-output -n "${ASR_CONDA_ENV:-asrt}" \
        python "$ROOT/services/asr_service/server.py" \
        --replay "$WAV" --wake "${ASR_WAKE_WORDS:-你好助手}" \
        --no-http 2>&1 | tail -30
    AFTER_COUNT="$(find "$REC_DIR" -name '*.wav' 2>/dev/null | wc -l)"
    if [[ "$BEFORE_COUNT" == "$AFTER_COUNT" ]]; then
        ok "未产生新的录音文件 (不落盘)"
    else
        fail "产生了新的录音文件, 不符合不落盘要求"
    fi
fi

echo
echo "=========================================="
if [[ "$FAILED" == "0" ]]; then
    echo " 结果: 全部通过"
else
    echo " 结果: 存在失败项"
fi
echo "=========================================="
exit "$FAILED"
