#!/usr/bin/env python3
"""验证「思考 (thinking)」链路是否真的通。

检查三件事:
1. 配置能正确解析出 thinking 参数
2. `_apply_thinking` 只对支持的 provider 发参数
3. `ToolCallResult` / `ChatResult` 带 thinking 字段 (之前**没有**,
   导致模型的思考内容被直接丢弃 —— orchestration.py 里读 result.thinking
   的是死代码)

**不联网**, 纯本地检查。需要联网验证模型真的思考, 用:

    # 1) 打开 configs/agent.yaml 的 llm.thinking.enabled
    # 2) 重启 agent 服务
    # 3) 订阅事件流看 thinking 事件
    python3 -c "
    import asyncio, json, urllib.request, websockets
    async def main():
        async with websockets.connect('ws://127.0.0.1:8800/v1/events/') as ws:
            ...
    "

用法::

    cd services/agent_service
    conda run -n botagent python check_thinking.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "agent_service"))

from config import load_config  # noqa: E402
from llm import (  # noqa: E402
    THINKING_CAPABLE_PROVIDERS,
    ChatResult,
    ToolCallResult,
    _apply_thinking,
)

FAILED = 0
PASSED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED, PASSED
    if ok:
        PASSED += 1
        print(f"  [PASS] {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f"  ({detail})" if detail else ""))


def test_config() -> None:
    print("\n[1] 配置解析")
    cfg = load_config()
    chat_th = cfg.llm.thinking
    tc_th = cfg.router.tool_calling.thinking

    check("llm.thinking 存在", chat_th is not None)
    check(
        "llm.thinking.to_payload() 关闭时返回 None",
        chat_th.to_payload() is None or chat_th.enabled,
        f"enabled={chat_th.enabled}",
    )
    check(
        "tool_calling.thinking 存在",
        tc_th is not None,
        f"enabled={tc_th.enabled}",
    )
    if tc_th.enabled:
        payload = tc_th.to_payload()
        check(
            "tool_calling payload 格式正确",
            payload == {"type": "enabled", "budget_tokens": tc_th.budget_tokens},
            str(payload),
        )


def test_apply_thinking() -> None:
    print("\n[2] _apply_thinking 行为")
    enabled = {"type": "enabled", "budget_tokens": 4000}

    p: dict = {}
    _apply_thinking(p, None, "agnes")
    check("thinking=None -> 不加参数", p == {}, str(p))

    p = {}
    _apply_thinking(p, enabled, "agnes")
    check(
        "支持的 provider 会加参数",
        p.get("thinking") == enabled,
        str(p),
    )

    # 不支持的 provider 不能加 —— 有些服务端会直接 400
    for prov in ("openai", "qwen", "ollama", "kimi"):
        p = {}
        _apply_thinking(p, enabled, prov)
        check(
            f"不支持的 provider ({prov}) 不加参数",
            p == {},
            str(p),
        )

    # 开关关掉也不能加
    p = {}
    _apply_thinking(p, {"type": "disabled"}, "agnes")
    check("type != enabled -> 不加参数", p == {}, str(p))


def test_result_fields() -> None:
    print("\n[3] 结果对象带 thinking 字段")
    tr = ToolCallResult(content="你好")
    check("ToolCallResult.thinking 存在", hasattr(tr, "thinking"))
    check("ToolCallResult.thinking 默认空", tr.thinking == "")
    check("ToolCallResult.has_thinking 为 False", not tr.has_thinking)

    tr2 = ToolCallResult(content="你好", thinking="我想想...", reasoning_tokens=42)
    check("ToolCallResult.has_thinking 为 True", tr2.has_thinking)
    check("reasoning_tokens 正确", tr2.reasoning_tokens == 42)

    cr = ChatResult(content="你好")
    check("ChatResult.thinking 存在", hasattr(cr, "thinking"))
    check("ChatResult.has_thinking 为 False", not cr.has_thinking)
    cr2 = ChatResult(content="你好", thinking="思考中")
    check("ChatResult.has_thinking 为 True", cr2.has_thinking)


def test_capable_providers() -> None:
    print("\n[4] 支持思考的 provider")
    check(
        "agnes 在列表里 (本项目默认 provider)",
        "agnes" in THINKING_CAPABLE_PROVIDERS,
        str(sorted(THINKING_CAPABLE_PROVIDERS)),
    )
    check(
        "deepseek 在列表里",
        "deepseek" in THINKING_CAPABLE_PROVIDERS,
    )


def main() -> int:
    print("=" * 60)
    print("Thinking 链路自检")
    print("=" * 60)

    test_config()
    test_apply_thinking()
    test_result_fields()
    test_capable_providers()

    print("\n" + "=" * 60)
    if FAILED:
        print(f"失败 {FAILED} 项, 通过 {PASSED} 项")
        return 1
    print(f"全部通过 ({PASSED} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
