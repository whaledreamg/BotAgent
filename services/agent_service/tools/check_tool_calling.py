#!/usr/bin/env python3
"""tool calling 接入自检。

覆盖:
1. tool schema 生成 (function 名 / enum 完整性 / required)
2. 参数校验 (非法动作名 / 缺参数 / 类型错 都被拦下)
3. 路由决策 (用假 LLM 替身验证三种路径)
4. live 模式: 真实调用 Agnes, 验证 tool_calls 解析

用法::

    python tools/check_tool_ccalling.py           # 离线自检
    python tools/check_tool_calling.py --live    # 额外跑真实 API 调用
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from action_tools import (  # noqa: E402
    ActionCatalog,
    ActionError,
    ActionSpec,
    build_tool_specs,
    to_openai_tools,
    validate_args,
)
from config import load_config  # noqa: E402
from handlers.base import AgentRequest  # noqa: E402
from llm import LLMClient, ToolCall, ToolCallResult  # noqa: E402
from registry import Registry  # noqa: E402
from router import Router  # noqa: E402

PASSED = 0
FAILED = 0


def check(desc: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {desc}")
    else:
        FAILED += 1
        print(f"  [FAIL] {desc}  {detail}")


# ------------------------------------------------------------ 1. schema


def test_schema(cfg: Any) -> None:
    print("\n[1] tool schema 生成")
    specs = build_tool_specs(cfg)
    names = [s.name for s in specs]
    check("生成了 tool 定义", len(specs) > 0, str(names))
    check("包含 play_arm_action", "play_arm_action" in names, str(names))
    check("包含 control_light", "control_light" in names, str(names))
    check(
        "包含 query_weather / query_time / query_date",
        all(n in names for n in ("query_weather", "query_time", "query_date")),
        str(names),
    )

    # 机械臂的 enum 必须覆盖 actions.yaml 里全部动作
    arm = next((s for s in specs if s.name == "play_arm_action"), None)
    check("机械臂 tool 存在", arm is not None)
    if arm is not None:
        enum = arm.parameters["properties"]["action"].get("enum") or []
        expected = [
            str(i.get("action")) for i in (cfg.arm_actions.actions or []) if i.get("action")
        ]
        check(
            "enum 覆盖全部动作名",
            set(enum) == set(expected),
            f"enum={enum} expected={expected}",
        )
        check("action 是必填", "action" in (arm.parameters.get("required") or []))
        check("kind=arm", arm.kind == "arm", arm.kind)
        check("动作要朗读", arm.speak is True)

    # OpenAI 格式
    tools = to_openai_tools(specs)
    check("to_openai 数量一致", len(tools) == len(specs))
    ok_shape = all(
        t.get("type") == "function"
        and isinstance(t.get("function", {}).get("name"), str)
        and isinstance(t.get("function", {}).get("parameters"), dict)
        for t in tools
    )
    check("OpenAI 工具格式正确", ok_shape, json.dumps(tools[0], ensure_ascii=False))

    # 每个 device/query tool 都要有 action_map
    for s in specs:
        if s.kind in ("device", "query"):
            check(f"{s.name} 有 action_map", bool(s.action_map))


# ------------------------------------------------------------ 1b. yaml 驱动


def test_yaml_driven(cfg: Any) -> None:
    print("\n[1b] tool 定义来自 yaml")
    check("tool_specs 已加载", cfg.tool_specs.loaded, "configs/tools.yaml 没读到")
    check("yaml 里定义了工具", len(cfg.tool_specs.tools) > 0, str(cfg.tool_specs.names))

    # yaml 里的每个工具都应该在最终的 spec 列表里
    specs = build_tool_specs(cfg)
    names = [s.name for s in specs]
    for raw in cfg.tool_specs.tools:
        check(f"yaml 的 {raw.name} 生成了 spec", raw.name in names, str(names))

    # 意图映射: 不再是硬编码表, 而是跟着 spec 走
    by_name = {s.name: s for s in specs}
    check(
        "play_arm_action 走 device 分支",
        by_name["play_arm_action"].intent == "device",
        by_name["play_arm_action"].intent,
    )
    for n in ("control_light", "control_aircon", "play_music", "query_weather"):
        check(
            f"{n} 走 tool 分支",
            by_name[n].intent == "tool",
            by_name[n].intent,
        )

    # YAML 1.1 的坑: 裸 on/off 会被解析成布尔值
    light = by_name["control_light"]
    check(
        "action_map 的键是小写字符串",
        set(light.action_map.keys()) == {"on", "off"},
        str(light.action_map),
    )
    check(
        "on 映射到 light_on",
        light.action_map.get("on") == "light_on",
        str(light.action_map),
    )

    # 没有 action_map 的定义必须被跳过 (而不是让服务起不来)
    from action_tools import ActionError, build_spec_from_config
    from config import ToolSpecConfig

    bad = ToolSpecConfig(name="broken_tool", kind="device", label="坏的")
    try:
        build_spec_from_config(bad)
        check("缺 action_map 被拒绝", False, "没有抛错")
    except ActionError as exc:
        check("缺 action_map 被拒绝", "action_map" in str(exc), str(exc))

    # 缺 name 也要被拒绝
    try:
        build_spec_from_config(ToolSpecConfig(kind="device"))
        check("缺 name 被拒绝", False, "没有抛错")
    except ActionError as exc:
        check("缺 name 被拒绝", "name" in str(exc), str(exc))

    # speak 没写时留空 -> 交给执行时按内部动作判定
    check(
        "speak 未写时为 None (交给执行时判定)",
        by_name["control_light"].speak is None,
        repr(by_name["control_light"].speak),
    )


# ------------------------------------------------------------ 2. 参数校验


def test_validation(cfg: Any) -> None:
    print("\n[2] 参数校验")
    specs = build_tool_specs(cfg)
    arm = next(s for s in specs if s.name == "play_arm_action")
    light = next(s for s in specs if s.name == "control_light")
    weather = next(s for s in specs if s.name == "query_weather")

    # 合法
    try:
        out = validate_args(arm, {"action": "挥手"})
        check("合法动作名通过", out.get("action") == "挥手", str(out))
    except ActionError as exc:
        check("合法动作名通过", False, str(exc))

    # 幻觉出的动作名必须被拦下
    try:
        validate_args(arm, {"action": "不存在的动作"})
        check("非法动作名被拦下", False, "没有抛错")
    except ActionError as exc:
        check("非法动作名被拦下", "不合法" in str(exc), str(exc))

    # 缺必填
    try:
        validate_args(arm, {})
        check("缺必填参数被拦下", False, "没有抛错")
    except ActionError as exc:
        check("缺必填参数被拦下", "缺少必填参数" in str(exc), str(exc))

    # enum 不匹配
    try:
        validate_args(light, {"state": "半开"})
        check("非法 state 被拦下", False, "没有抛错")
    except ActionError as exc:
        check("非法 state 被拦下", "不合法" in str(exc), str(exc))

    # 类型转换
    try:
        out = validate_args(arm, {"action": "挥手", "speed": "1.5"})
        check("speed 字符串转数字", out.get("speed") == 1.5, str(out))
    except ActionError as exc:
        check("speed 字符串转数字", False, str(exc))

    # 多余参数丢弃
    try:
        out = validate_args(weather, {"city": "北京", "bogus": "x"})
        check("未声明参数被丢弃", "bogus" not in out, str(out))
    except ActionError as exc:
        check("未声明参数被丢弃", False, str(exc))


# ------------------------------------------------------------ 3. 路由


class FakeLLM:
    """假 LLM —— 按预设脚本返回 tool_call 或纯文本。"""

    def __init__(self, script: Optional[ToolCallResult] = None) -> None:
        self.script = script
        self.calls = 0

    async def tool_call(self, messages, tools, **kwargs) -> ToolCallResult:
        self.calls += 1
        if self.script is None:
            raise RuntimeError("FakeLLM 没有配置脚本")
        return self.script

    async def chat(self, messages, **kwargs) -> str:
        self.calls += 1
        return "假回复"

    async def json_completion_stream(self, messages, **kwargs):
        self.calls += 1
        return None


def _make_router(cfg: Any, llm: Any) -> Router:
    reg = Registry()
    reg.load(cfg, llm)
    return Router(cfg, reg, llm)


def test_routing(cfg: Any) -> None:
    print("\n[3] 路由决策")

    # ---- 3a. 规则命中 -> 不该调 LLM ----
    llm = FakeLLM()
    router = _make_router(cfg, llm)
    decision = asyncio.run(router.decide(AgentRequest(text="挥个手")))
    check("规则命中走 device", decision.intent == "device", decision.intent)
    check("规则命中标记 by=rule", decision.by == "rule", decision.by)
    check("规则命中没有调 LLM", llm.calls == 0, f"LLM 调用了 {llm.calls} 次")
    check("规则命中没选工具", decision.tool is None)

    # ---- 3b. tool_calls 命中 ----
    # 注意: 用例文本必须**不命中任何规则**, 否则会走规则快速通道。
    # 「打个招呼」是挥手动作的说法之一, 所以这里换成规则里没有的说法。
    llm = FakeLLM(
        ToolCallResult(
            content="",
            calls=[
                ToolCall(
                    id="call_1",
                    name="play_arm_action",
                    arguments={"action": "挥手"},
                )
            ],
            finish_reason="tool_calls",
        )
    )
    router = _make_router(cfg, llm)
    text = "机械臂来个表演"
    check(
        "用例文本不命中规则",
        router.match_rules(text) is None,
        str(router.match_rules(text)),
    )
    decision = asyncio.run(router.decide(AgentRequest(text=text)))
    check("tool_calls 选中工具", decision.tool is not None)
    check(
        "tool 是 play_arm_action",
        decision.tool is not None and decision.tool.name == "play_arm_action",
        str(decision.tool),
    )
    check("tool_calls 映射到 device", decision.intent == "device", decision.intent)
    check("by=tools", decision.by == "tools", decision.by)
    check("参数带过来了", decision.tool_args.get("action") == "挥手", str(decision.tool_args))
    check("tool calling 只调了 1 次 LLM", llm.calls == 1, f"LLM 调用了 {llm.calls} 次")

    # ---- 3c. 纯 chat -> content 直接作为回复 ----
    llm = FakeLLM(
        ToolCallResult(
            content="今天天气不错",
            calls=[],
            finish_reason="stop",
        )
    )
    router = _make_router(cfg, llm)
    decision = asyncio.run(router.decide(AgentRequest(text="今天天气怎么样")))
    check("纯 chat 意图是 chat", decision.intent == "chat", decision.intent)
    check("纯 chat 拿到 content", decision.content == "今天天气不错", decision.content)
    check("纯 chat 没选工具", decision.tool is None)
    check("纯 chat 只调了 1 次 LLM", llm.calls == 1, f"LLM 调用了 {llm.calls} 次")

    # ---- 3d. 模型选了不存在的工具 -> 回落 ----
    llm = FakeLLM(
        ToolCallResult(
            content="",
            calls=[ToolCall(id="c", name="不存在的工具", arguments={})],
            finish_reason="tool_calls",
        )
    )
    router = _make_router(cfg, llm)
    decision = asyncio.run(router.decide(AgentRequest(text="随便说点什么")))
    check("未知工具被忽略", decision.tool is None)
    check("未知工具回落到 chat", decision.intent == "chat", decision.intent)

    # ---- 3e. tool calling 关闭时不受影响 ----
    cfg2 = load_config()
    cfg2.router.tool_calling.enabled = False
    llm = FakeLLM()
    router = _make_router(cfg2, llm)
    decision = asyncio.run(router.decide(AgentRequest(text="挥个手")))
    check("关闭后规则仍然生效", decision.intent == "device", decision.intent)
    check("关闭后不调 LLM", llm.calls == 0)

    # ---- 3f. 假确认 -> 用 required 重试 ----
    class RetryLLM(FakeLLM):
        """第一次返回假确认文本, 第二次 (required) 返回真工具调用。"""

        def __init__(self) -> None:
            super().__init__()
            self.choices = [
                ToolCallResult(
                    content="好的，帮你关掉空调。",
                    calls=[],
                    finish_reason="stop",
                ),
                ToolCallResult(
                    content="",
                    calls=[
                        ToolCall(
                            id="c2",
                            name="control_aircon",
                            arguments={"state": "off"},
                        )
                    ],
                    finish_reason="tool_calls",
                ),
            ]

        async def tool_call(self, messages, tools, **kwargs):
            self.calls += 1
            idx = min(self.calls - 1, len(self.choices) - 1)
            return self.choices[idx]

    llm = RetryLLM()
    router = _make_router(cfg, llm)
    decision = asyncio.run(router.decide(AgentRequest(text="把空调关掉")))
    check("假确认触发重试", llm.calls == 2, f"LLM 调用了 {llm.calls} 次")
    check("重试后选中了工具", decision.tool is not None)
    check(
        "重试后工具正确",
        decision.tool is not None and decision.tool.name == "control_aircon",
        str(decision.tool),
    )
    check("重试后参数正确", decision.tool_args.get("state") == "off", str(decision.tool_args))
    check(
        "trace 里有 fake-confirm 标记",
        any(t["detail"] == "fake-confirm" for t in decision.trace),
        str([t["detail"] for t in decision.trace]),
    )

    # ---- 3g. 假确认且重试也没成 -> 不能假装做完了 ----
    class DeadRetryLLM(FakeLLM):
        async def tool_call(self, messages, tools, **kwargs):
            self.calls += 1
            return ToolCallResult(
                content="好的，帮你关掉空调。",
                calls=[],
                finish_reason="stop",
            )

    llm = DeadRetryLLM()
    router = _make_router(cfg, llm)
    decision = asyncio.run(router.decide(AgentRequest(text="把空调关掉")))
    check("重试失败不返回假确认", "帮你关掉" not in decision.content, decision.content)
    check("重试失败说明做不到", "没能执行" in decision.content, decision.content)

    # ---- 3h. 假确认检测器本身 ----
    check(
        "识别出假确认措辞",
        router._looks_like_fake_confirm("好的，帮你关掉空调。"),
    )
    check(
        "普通闲聊不算假确认",
        not router._looks_like_fake_confirm("你好，有什么可以帮你的吗？"),
    )
    check(
        "普通回答不算假确认",
        not router._looks_like_fake_confirm("北京今天晴，气温 24 度"),
    )

    # ---- 3i. 并行多工具调用 ----
    llm = FakeLLM(
        ToolCallResult(
            content="",
            calls=[
                ToolCall(id="c1", name="play_arm_action", arguments={"action": "挥手"}),
                ToolCall(id="c2", name="control_light", arguments={"state": "on"}),
            ],
            finish_reason="tool_calls",
        )
    )
    router = _make_router(cfg, llm)
    decision = asyncio.run(router.decide(AgentRequest(text="随便来点活")))
    check("并行调用全部保留", len(decision.tool_calls) == 2, str(decision.tool_calls))
    check("第一个是主工具", decision.tool is not None
          and decision.tool.name == "play_arm_action", str(decision.tool))

    # ---- 3j. 规则命中但分支匹配不上具体动作 -> 升级到 tool calling ----
    # 「帮我把灯打开」: 规则里的关键词「打开」命中了, 但 TOOLS 表的正则
    # 要求「打开」后面紧跟「灯」, 所以分支匹配不上具体工具。
    # 这种情况下应该升级到 tool calling, 而不是回一句「暂不支持」。
    llm = FakeLLM(
        ToolCallResult(
            content="",
            calls=[
                ToolCall(id="c1", name="control_light", arguments={"state": "on"})
            ],
            finish_reason="tool_calls",
        )
    )
    router = _make_router(cfg, llm)
    text = "帮我把灯打开"
    check(
        "规则命中 tool 分支",
        (router.match_rules(text) or ("", ""))[0] == "tool",
        str(router.match_rules(text)),
    )
    decision = asyncio.run(router.decide(AgentRequest(text=text)))
    check("升级到 tool calling", decision.by == "tools", decision.by)
    check("选中了 control_light", decision.tool is not None
          and decision.tool.name == "control_light", str(decision.tool))
    check("参数是 on", decision.tool_args.get("state") == "on", str(decision.tool_args))

    # ---- 3k. 规则命中且分支认得 -> 仍然零 LLM ----
    llm = FakeLLM()
    router = _make_router(cfg, llm)
    decision = asyncio.run(router.decide(AgentRequest(text="打开空调")))
    check("能匹配的规则不调 LLM", llm.calls == 0, f"LLM 调用了 {llm.calls} 次")
    check("能匹配的规则走 tool", decision.intent == "tool", decision.intent)
    check("能匹配的规则 by=rule", decision.by == "rule", decision.by)

    # ---- 3l. tool calling 关闭时, 规则命中行为不变 ----
    cfg3 = load_config()
    cfg3.router.tool_calling.enabled = False
    llm = FakeLLM()
    router = _make_router(cfg3, llm)
    decision = asyncio.run(router.decide(AgentRequest(text="帮我把灯打开")))
    check("关闭后规则命中仍然放行", decision.intent == "tool", decision.intent)
    check("关闭后不调 LLM", llm.calls == 0)


# ------------------------------------------------------------ 4. 执行分派


def test_execution(cfg: Any) -> None:
    print("\n[4] 动作执行分派 (不连真机)")
    specs = build_tool_specs(cfg)

    class FakeDevice:
        def __init__(self) -> None:
            self.seen: List[str] = []

        async def execute(self, name, args=None, source="rule"):
            from handlers.base import BranchResult

            self.seen.append(name)
            return BranchResult(
                reply=f"好的，{name}",
                speak=True,
                action={"name": name, "status": "ok", "detail": "", "speak": True},
                trace=[],
            )

    class FakeTool:
        def __init__(self) -> None:
            self.seen: List[str] = []

        def tool_label(self, name):
            return name

        def is_output_tool(self, name):
            return name in ("weather", "time", "date")

        async def execute(self, name, args):
            self.seen.append(name)
            return "ok", f"{name} 完成"

    dev = FakeDevice()
    tool = FakeTool()
    catalog = ActionCatalog(specs, device_handler=dev, tool_handler=tool)

    arm = next(s for s in specs if s.name == "play_arm_action")
    outcome = asyncio.run(catalog.execute(arm, {"action": "挥手"}))
    check("机械臂走 device_handler", dev.seen == ["挥手"], str(dev.seen))
    check("机械臂结果 ok", outcome.ok is True)
    check("机械臂要朗读", outcome.speak is True)

    light = next(s for s in specs if s.name == "control_light")
    outcome = asyncio.run(catalog.execute(light, {"state": "off"}))
    check("control_light(off) -> light_off", tool.seen == ["light_off"], str(tool.seen))
    check("设备动作不朗读", outcome.speak is False)

    weather = next(s for s in specs if s.name == "query_weather")
    outcome = asyncio.run(catalog.execute(weather, {"city": "北京"}))
    check("query_weather -> weather", "weather" in tool.seen, str(tool.seen))
    check("查询型要朗读", outcome.speak is True)

    # 参数非法必须被拦下, 且**不能**到达执行器
    before = len(dev.seen)
    try:
        asyncio.run(catalog.execute(arm, {"action": "不存在的动作"}))
        check("非法参数被拦下", False, "没有抛错")
    except ActionError:
        check("非法参数被拦下", True)
    check("非法参数没到执行器", len(dev.seen) == before, str(dev.seen))


# ------------------------------------------------------------ 5. live


def test_live(cfg: Any) -> None:
    print("\n[5] live: 真实调用 Agnes")
    try:
        llm = LLMClient(cfg.llm)
    except Exception as exc:
        check("创建 LLM 客户端", False, str(exc))
        return

    if not cfg.llm.api_key:
        check("有 API key", False, "LLM_API_KEY 未配置")
        return

    specs = build_tool_specs(cfg)
    tools = to_openai_tools(specs)
    messages = [{"role": "user", "content": "让机械臂挥个手吧"}]

    try:
        result = asyncio.run(
            llm.tool_call(
                messages,
                tools,
                tool_choice="auto",
                model="agnes-2.5-flash",
                temperature=0.0,
                max_tokens=256,
            )
        )
    except Exception as exc:
        check("调用 Agnes", False, str(exc))
        return

    check("调用成功", True)
    check("返回了 tool_calls", result.has_calls, f"finish={result.finish_reason}")
    if result.has_calls:
        call = result.calls[0]
        check("工具名正确", call.name == "play_arm_action", call.name)
        check(
            "动作名在 enum 内",
            call.arguments.get("action") in
            (next(s for s in specs if s.name == "play_arm_action")
             .parameters["properties"]["action"]["enum"]),
            str(call.arguments),
        )


# ------------------------------------------------------------ main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="额外跑真实 API 调用")
    args = parser.parse_args()

    cfg = load_config()
    print(f"模型: {cfg.llm.model} | router.mode={cfg.router.mode} | "
          f"tool_calling={cfg.router.tool_calling.enabled}")

    test_schema(cfg)
    test_yaml_driven(cfg)
    test_validation(cfg)
    test_routing(cfg)
    test_execution(cfg)
    if args.live:
        test_live(cfg)

    print(f"\n通过 {PASSED}, 失败 {FAILED}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
