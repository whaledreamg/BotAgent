#!/usr/bin/env python3
"""describe_scene 工具: 匹配规则与三种输出的检查。

运行: cd services/agent_service && python test_describe_scene.py
"""

import asyncio
import sys

from config import load_config
from handlers.tool_handler import ToolHandler
from vision_client import VisionError

handler = ToolHandler(config=load_config(), llm=None)
failures = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


print("匹配:")
for text in ["你看到了什么", "你前面有什么", "桌上有什么东西", "看看有什么", "你看见啥了"]:
    name, _, _ = handler._match(text)
    check(name == "describe_scene", f"{text!r} -> {name}")

for text, want in [("杯子在哪", "find_object"), ("有几个瓶子", "find_object"),
                   ("现在几点", "time")]:
    name, _, _ = handler._match(text)
    check(name == want, f"{text!r} -> {name} (期望 {want})")

name, _, _ = handler._match("左手在哪")
check(name is None, f"'左手在哪' 被机械臂判断挡掉 -> {name}")


class FakeVision:
    def __init__(self, objects=None, error=False):
        self.objects, self.error = objects or [], error

    async def detect(self, **_):
        if self.error:
            raise VisionError("boom")
        return {"objects": self.objects}


def run(fake):
    handler._vision_client = fake
    return asyncio.run(handler.execute("describe_scene", {}))


def obj(zh):
    return {"label": "x", "label_zh": zh}


print("输出:")
st, msg = run(FakeVision([obj("杯子"), obj("杯子"), obj("人")]))
check(st == "ok" and msg == "我看到 2 个杯子、1 个人。", f"有物体: {msg}")

st, msg = run(FakeVision([]))
check(st == "ok" and "没有看到" in msg, f"无物体: {msg}")

st, msg = run(FakeVision(error=True))
check(st == "failed" and "视觉服务" in msg, f"VisionError: {msg}")

st, msg = run(FakeVision([{"label": "cup"}]))
check("1 个cup" in msg, f"无 label_zh 回落原类名: {msg}")

st, msg = run(FakeVision([obj(n) for n in "甲乙丙丁戊己庚"]))
check("还有其他几样东西" in msg and msg.count("个") == 5, f"超过 5 类截断: {msg}")

print("\n全部通过" if not failures else f"\n{len(failures)} 项失败")
sys.exit(1 if failures else 0)
