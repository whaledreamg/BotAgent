#!/usr/bin/env python3
"""Agent 服务离线自检。

用假的 LLM 替身跑通"分流 -> 分支处理 -> speak 决策"整条逻辑, 不联网。
重点验证:
1. 规则命中 -> tool 分支, speak=false
2. 规则未命中 -> LLM 兜底分类
3. chat 分支 -> speak=true
4. 注册表支持新增分支
5. 会话历史按轮数裁剪

用法:
    conda run -n botagent python tools/selftest.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config
from handlers.base import AgentRequest, BranchResult, Handler
from registry import Registry
from router import Router

FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        FAILED = 1


class FakeLLM:
    """假 LLM: json_completion 返回预设意图, chat 返回固定文本。"""

    def __init__(self, intent: str = "chat", reply: str = "这是模型的回答。") -> None:
        self.intent = intent
        self.reply = reply
        self.calls = 0

    async def json_completion(self, messages, model=None, temperature=0.0, max_tokens=128):
        self.calls += 1
        return {"intent": self.intent}

    async def json_completion_stream(
        self, messages, model=None, temperature=0.0, max_tokens=128
    ):
        self.calls += 1
        return {"intent": self.intent}

    async def chat(self, messages, model=None, temperature=None, max_tokens=None):
        self.calls += 1
        return self.reply


class CustomHandler(Handler):
    """用于验证注册表机制的临时分支。"""

    speak = False

    async def handle(self, req: AgentRequest) -> BranchResult:
        return BranchResult(
            reply="custom ok",
            speak=False,
            action={"name": "custom", "args": {}, "status": "ok"},
        )


async def main() -> int:
    cfg = load_config()
    llm = FakeLLM()

    registry = Registry()
    registry.load(cfg, llm)
    print(f"已装载分支: {registry.names()}")
    check("chat 分支已装载", "chat" in registry)
    check("tool 分支已装载", "tool" in registry)
    # device 分支由 configs/agent.yaml 的 handlers.device.enabled 控制
    # (启用后机械臂才能接语音指令), 这里按配置断言而不是写死
    device_expected = cfg.handlers.get("device").enabled if "device" in cfg.handlers else False
    check(
        "device 分支装载状态与配置一致",
        ("device" in registry) == device_expected,
        f"(enabled={device_expected})",
    )

    router = Router(cfg, registry, llm)

    print("\n1) 规则命中: 动作指令 -> tool, speak=false")
    intent, trace = await router.route(AgentRequest(text="打开空调"))
    check("分流到 tool", intent == "tool", f"(实际 {intent})")
    handler = registry.get(intent)
    result = await handler.handle(AgentRequest(text="打开空调"))
    check("tool 分支 speak=false", result.speak is False)
    check("tool 分支返回 action", result.action is not None)
    check("动作匹配到 aircon_on", result.action.get("name") == "aircon_on",
          f"(实际 {result.action.get('name')})")
    print(f"     action={result.action}")

    print("\n2) 疑问句走 chat, 不被当成动作指令")
    intent, trace = await router.route(AgentRequest(text="你好，今天天气怎么样"))
    check("疑问句分流到 chat", intent == "chat", f"(实际 {intent})")
    tool_handler = registry.get("tool")
    tool_res = await tool_handler.handle(AgentRequest(text="你好，今天天气怎么样"))
    check("疑问句不产生动作", tool_res.action.get("status") == "unsupported",
          f"(实际 {tool_res.action})")

    print("\n3) 规则命中机器人动作时, 回落到已启用的分支")
    intent, trace = await router.route(AgentRequest(text="向前走"))
    check("分流到已启用的分支", intent in registry, f"(实际 {intent})")
    print(f"     intent={intent} trace={trace}")

    print("\n4) LLM 兜底超时 -> 回落默认分支")
    class SlowLLM(FakeLLM):
        async def json_completion_stream(
            self, messages, model=None, temperature=0.0, max_tokens=128
        ):
            import asyncio

            await asyncio.sleep(30)
            return {"intent": "chat"}

    slow = SlowLLM()
    slow_router = Router(cfg, registry, slow)
    started = asyncio.get_event_loop().time()
    intent, trace = await slow_router.route(AgentRequest(text="随便说点什么"))
    elapsed = asyncio.get_event_loop().time() - started
    check("超时后回落 chat", intent == "chat", f"(实际 {intent})")
    check(f"超时受控 (<6s, 实际 {elapsed:.1f}s)", elapsed < 6.0)

    print("\n5) LLM 兜底正常返回 -> 采用 LLM 结果")
    llm.intent = "chat"
    intent, trace = await router.route(AgentRequest(text="讲个笑话吧"))
    check("LLM 兜底分流到 chat", intent == "chat", f"(实际 {intent})")
    handler = registry.get(intent)
    result = await handler.handle(AgentRequest(text="讲个笑话吧"))
    check("chat 分支 speak=true", result.speak is True)
    check("chat 分支有回复文本", bool(result.reply))
    print(f"     reply={result.reply}")

    print("\n6) LLM 误判时, 分支自检可以否决")
    llm.intent = "tool"      # LLM 把闲聊误判成 tool
    intent, trace = await router.route(AgentRequest(text="今天天气怎么样"))
    check("被分支自检否决后回落 chat", intent == "chat", f"(实际 {intent})")
    confirm = [t for t in trace if t["step"] == "confirm"]
    check("trace 里有 confirm 记录", bool(confirm), f"(trace={trace})")
    print(f"     trace={trace}")

    print("\n7) 注册表支持新增分支")
    cfg.handlers["custom"] = type(cfg.handlers["chat"])(
        name="custom",
        module="tools.selftest",
        cls="CustomHandler",
        enabled=True,
        speak=False,
    )
    registry.load(cfg, llm)
    check("custom 分支已装载", "custom" in registry)
    custom = registry.get("custom")
    res = await custom.handle(AgentRequest(text="x"))
    check("custom 分支可执行", res.reply == "custom ok")

    print("\n8) 会话历史")
    from session import SessionStore

    store = SessionStore(max_turns=2, ttl_seconds=60)
    store.append("s1", "你好", "在的")
    store.append("s1", "你是谁", "我是助手")
    history = store.history("s1")
    check("历史保留最近 2 轮", len(history) == 4, f"(实际 {len(history)})")
    check("空会话返回空列表", store.history("s2") == [])

    print("\n9) 输出型工具的结果要朗读 (speak=true)")
    tool_handler = registry.get("tool")
    check("tool 分支已装载", tool_handler is not None)
    if tool_handler is not None:
        for text, expect_tool in (
            ("查询北京天气", "weather"),
            ("现在几点", "time"),
            ("今天几号", "date"),
        ):
            res = await tool_handler.handle(AgentRequest(text=text))
            name = (res.action or {}).get("name")
            check(f"{text!r} 匹配到 {expect_tool}", name == expect_tool,
                  f"(实际 {name})")
            check(f"{text!r} speak=true (结果要播报)", res.speak is True)
            check(f"{text!r} 回复里带具体内容", bool(res.reply) and
                  res.reply != f"已执行 {expect_tool}",
                  f"(实际 {res.reply!r})")
            print(f"     {text} -> speak={res.speak} reply={res.reply!r}")

    print("\n10) 执行型工具不朗读 (speak=false)")
    if tool_handler is not None:
        res = await tool_handler.handle(AgentRequest(text="打开空调"))
        check("开灯/开空调仍不朗读", res.speak is False)
        check("开空调返回 action", (res.action or {}).get("name") == "aircon_on")
        print(f"     打开空调 -> speak={res.speak} reply={res.reply!r}")

    print("\n11) 闲聊问句不会被当成输出型工具")
    if tool_handler is not None:
        res = await tool_handler.handle(AgentRequest(text="今天天气怎么样"))
        check("疑问句不产生动作", (res.action or {}).get("status") == "unsupported")
        check("不支持的动作会告知用户 (speak=true)", res.speak is True,
              f"(实际 {res.speak})")

    print("\n12) 输出型工具判定可被配置覆盖")
    if tool_handler is not None:
        # 构造一个只把 weather 视为输出型的配置
        import copy

        cfg2 = copy.deepcopy(cfg)
        cfg2.handlers["tool"].options["speak_tools"] = ["weather"]
        reg2 = Registry()
        reg2.load(cfg2, llm)
        h2 = reg2.get("tool")
        check("配置生效: weather 是输出型",
              h2.is_output_tool("weather") is True)
        check("配置生效: time 不是输出型",
              h2.is_output_tool("time") is False)
        res = await h2.handle(AgentRequest(text="现在几点"))
        check("被排除的工具不朗读", res.speak is False,
              f"(实际 {res.speak})")

    print()
    if FAILED == 0:
        print("全部通过")
    else:
        print("存在失败项")
    return FAILED


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
