#!/usr/bin/env python3
"""对比实验: 「所有请求都带 tools」 vs 「规则优先 + tool calling 兜底」。

要回答的问题:
1. 模型自己能不能分清「要执行动作」和「纯闲聊」?
2. 统一走 tool calling 之后, 会不会出现「该调不调」(说了要做但没做)?
3. 延迟代价多大?

**结论 (2026-11 实测)**: 一律带 tools 的准确率确实更高 (18/19 vs 12/19),
但延迟是规则路径的 6 倍, 且完全依赖 LLM —— LLM 一限流 (429) 全线失败。
所以保持「规则优先, 复合指令也走规则」的做法。

用法::
    python tools/exp_unified_tools.py                # 对比两种策略
    python tools/exp_unified_tools.py --only-current # 只跑当前实现 (省 API 额度)
    python tools/exp_unified_tools.py --full         # 再跑一遍完整路由明细
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from action_tools import build_tool_specs, to_openai_tools  # noqa: E402
from config import load_config  # noqa: E402
from handlers.base import AgentRequest  # noqa: E402
from llm import LLMClient  # noqa: E402
from registry import Registry  # noqa: E402
from router import Router  # noqa: E402

#: 测试句子。kind 说明**期望**的结果:
#:   arm    -> 应该调 play_arm_action
#:   tool   -> 应该调某个设备/查询工具
#:   chat   -> 不该调任何工具, 应该用文字回答
CASES: List[Dict[str, Any]] = [
    # ---- 机械臂 (复合指令最容易出问题) ----
    {"text": "挥个手", "kind": "arm"},
    {"text": "挥挥手", "kind": "arm"},
    {"text": "先准备点然后再挥挥手", "kind": "arm"},
    {"text": "回到准备点后挥一挥手", "kind": "arm"},
    {"text": "机器人先帮我会准备点然后再挥挥手", "kind": "arm"},
    {"text": "伸左手然后伸右手", "kind": "arm"},
    {"text": "帮我把机械臂归零", "kind": "arm"},
    # ---- 设备 ----
    {"text": "打开客厅的灯", "kind": "tool"},
    {"text": "把空调关掉", "kind": "tool"},
    {"text": "帮我关灯", "kind": "tool"},
    # ---- 查询 ----
    {"text": "现在几点了", "kind": "tool"},
    {"text": "今天天气怎么样", "kind": "tool"},
    {"text": "今天是几号", "kind": "tool"},
    # ---- 闲聊 (不该调工具) ----
    {"text": "你好", "kind": "chat"},
    {"text": "讲个笑话", "kind": "chat"},
    {"text": "你会什么", "kind": "chat"},
    {"text": "你是谁", "kind": "chat"},
    {"text": "介绍一下你自己", "kind": "chat"},
    {"text": "为什么天是蓝色的", "kind": "chat"},
]

RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


def judge(kind: str, calls: List[Any]) -> tuple[bool, str]:
    """判定一次 tool calling 的结果是否符合预期。"""
    if not calls:
        if kind == "chat":
            return True, "未调工具(正确)"
        return False, "该调工具却没调"

    names = [c.name for c in calls]
    if kind == "arm":
        ok = any(n == "play_arm_action" for n in names)
    elif kind == "tool":
        ok = any(n != "play_arm_action" for n in names)
    else:  # chat
        ok = False

    detail = ",".join(names)
    return ok, detail


async def probe_tools(
    llm: LLMClient,
    tools: List[Dict[str, Any]],
    system_prompt: str,
    text: str,
    model: str,
) -> Dict[str, Any]:
    """跑一次带 tools 的原生 function calling。

    429 (免费额度限流) 会重试 —— 一次实验被限流打断的话, 后面的样本
    全部变成「未知」, 白跑。
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": text})

    t0 = time.time()
    last_err = ""
    for attempt in range(4):
        try:
            result = await llm.tool_call(
                messages,
                tools,
                tool_choice="auto",
                model=model,
                temperature=0.0,
                max_tokens=256,
            )
        except Exception as exc:
            last_err = str(exc)
            if "429" in last_err or "rate" in last_err.lower():
                wait = 5.0 * (attempt + 1)
                print(f"       (限流, 等 {wait:.0f}s 后重试)")
                time.sleep(wait)
                continue
            break
        else:
            return {
                "calls": result.calls,
                "content": result.content,
                "finish": result.finish_reason,
                "ms": int((time.time() - t0) * 1000),
                "tokens": (result.prompt_tokens, result.completion_tokens),
            }

    return {"error": last_err, "ms": int((time.time() - t0) * 1000)}


async def run_probe(cfg: Any, llm: LLMClient, args: argparse.Namespace) -> Dict[str, Any]:
    """跑一遍「所有请求都带 tools」的实验。"""
    specs = build_tool_specs(cfg)
    tools = to_openai_tools(specs)

    tc = getattr(cfg.router, "tool_calling", None)
    system_prompt = str(getattr(tc, "system_prompt", "") or "")
    model = str(getattr(tc, "model", "") or "") or None

    print(f"\n工具数: {len(tools)}  模型: {model}")
    print(f"system_prompt: {system_prompt[:70]!r}...\n")

    results: List[Dict[str, Any]] = []
    for case in CASES:
        text = case["text"]
        kind = case["kind"]

        r = await probe_tools(llm, tools, system_prompt, text, model)
        if "error" in r:
            print(f"  {RED}[ERR]{RESET} 「{text}」 {r['error'][:70]}")
            results.append({**case, **r, "ok": False})
            continue

        ok, detail = judge(kind, r["calls"])
        tag = f"{GREEN}OK  {RESET}" if ok else f"{RED}MISS{RESET}"
        extra = ""
        if not ok and not r["calls"]:
            extra = f"  content={r['content'][:40]!r}"
        print(
            f"  {tag} 「{text}」{'':　<{max(1, 22 - len(text))}}"
            f"{r['ms']:4d}ms  {detail}{extra}"
        )
        results.append({**case, **r, "ok": ok, "detail": detail})

    return {"probe": results, "tools": tools, "system_prompt": system_prompt}


async def run_full(cfg: Any, llm: LLMClient, args: argparse.Namespace) -> None:
    """对比: 现在的路由 vs 「一律带 tools」。"""
    reg = Registry()
    reg.load(cfg)
    router = Router(cfg, reg, llm)

    print("\n" + "=" * 78)
    print("完整路由对比 (现在的实现: 规则优先 + tool calling 兜底)")
    print("=" * 78)
    for case in CASES:
        t0 = time.time()
        try:
            decision = await router.decide(AgentRequest(text=case["text"]))
            ms = int((time.time() - t0) * 1000)
            by = decision.by
            matched = decision.matched or ""
            tool = decision.tool.name if decision.tool else ""
        except Exception as exc:
            print(f"  「{case['text']}」 失败: {exc}")
            continue
        print(
            f"  「{case['text']}」{'':　<{max(1, 22 - len(case['text']))}}"
            f"{ms:4d}ms  by={by:6s} intent={decision.intent:6s} "
            f"matched={matched:20s} tool={tool}"
        )


async def run_current_only(cfg: Any, llm: LLMClient) -> List[Dict[str, Any]]:
    """只跑现在的路由, 判定是否符合预期。

    这一项不调 LLM 的分支 (规则命中就是 0ms), 所以能看出「规则省了多少延迟」。
    """
    reg = Registry()
    reg.load(cfg)
    router = Router(cfg, reg, llm)

    print("\n" + "=" * 78)
    print("当前实现: 规则优先 + tool calling 兜底")
    print("=" * 78)
    out: List[Dict[str, Any]] = []
    for case in CASES:
        t0 = time.time()
        try:
            decision = await router.decide(AgentRequest(text=case["text"]))
        except Exception as exc:
            print(f"  「{case['text']}」 失败: {exc}")
            continue
        ms = int((time.time() - t0) * 1000)
        by = decision.by
        intent = decision.intent
        tool = decision.tool.name if decision.tool else ""

        # 判定: 期望 arm -> 最终 intent 要是 device 且调了 play_arm_action
        if case["kind"] == "arm":
            ok = intent == "device"
            detail = f"intent={intent} tool={tool or '-'}"
        elif case["kind"] == "tool":
            ok = intent in ("tool", "device")
            detail = f"intent={intent} tool={tool or '-'}"
        else:
            ok = intent == "chat"
            detail = f"intent={intent}"

        tag = f"{GREEN}OK  {RESET}" if ok else f"{RED}MISS{RESET}"
        print(
            f"  {tag} 「{case['text']}」{'':　<{max(1, 22 - len(case['text']))}}"
            f"{ms:4d}ms  by={by:6s} {detail}"
        )
        out.append({**case, "ok": ok, "ms": ms, "detail": detail, "by": by})

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="统一 tools 的对比实验")
    ap.add_argument("--full", action="store_true", help="再跑完整路由对比")
    ap.add_argument(
        "--only-current", action="store_true", help="只跑当前实现 (省 API 额度)"
    )
    args = ap.parse_args()

    print("=" * 78)
    print("实验: 所有请求都带 tools, 让模型自己区分 chat / 动作")
    print("=" * 78)

    cfg = load_config()
    if not cfg.llm.api_key:
        print("LLM_API_KEY 未配置")
        return 1

    llm = LLMClient(cfg.llm)

    # ---- 当前实现 ----
    current = asyncio.run(run_current_only(cfg, llm))
    cur_ok = sum(1 for r in current if r.get("ok"))
    cur_avg = sum(r.get("ms", 0) for r in current) // max(1, len(current))

    if args.only_current:
        print(f"\n当前实现: {cur_ok}/{len(current)} 符合预期, 平均 {cur_avg}ms")
        return 0

    # ---- 一律带 tools ----
    out = asyncio.run(run_probe(cfg, llm, args))
    results = out["probe"]

    total = len(results)
    ok = sum(1 for r in results if r.get("ok"))

    print("\n" + "=" * 78)
    print("结果对比")
    print("=" * 78)
    print(f"  当前实现 (规则优先)   {cur_ok}/{len(current)}  平均 {cur_avg}ms")
    print(f"  一律带 tools          {ok}/{total}  平均 "
          f"{sum(r.get('ms', 0) for r in results) // max(1, total)}ms")

    print("\n分类统计 (一律带 tools):")
    for kind in ("arm", "tool", "chat"):
        group = [r for r in results if r["kind"] == kind]
        if not group:
            continue
        good = sum(1 for r in group if r.get("ok"))
        avg = sum(r.get("ms", 0) for r in group) // max(1, len(group))
        color = GREEN if good == len(group) else YELLOW
        print(f"  {color}{kind:5s}{RESET} {good}/{len(group)}  平均 {avg}ms")

    if ok < total:
        print("\n不符合预期的句子:")
        for r in results:
            if not r.get("ok"):
                print(
                    f"  {RED}x{RESET} 「{r['text']}」 期望={r['kind']} "
                    f"实际={r.get('detail', '')}"
                    + (
                        f" content={r.get('content', '')[:50]!r}"
                        if r.get("content")
                        else ""
                    )
                )

    if args.full:
        asyncio.run(run_full(cfg, llm, args))

    return 0


if __name__ == "__main__":
    sys.exit(main())
