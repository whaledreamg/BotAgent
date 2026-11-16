#!/usr/bin/env python3
"""测量 LLM 首次响应延迟, 用于评估路由策略。

**按角色**分别打点 —— chat / classify / tool 三个角色可能绑到不同的
provider, 混在一起测没有意义。

用法:
    conda run -n botagent python tools/bench_llm.py
    conda run -n botagent python tools/bench_llm.py --times 5
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config
from llm import LLMClient


async def bench(name: str, coro_factory, times: int = 3) -> None:
    for i in range(times):
        started = time.time()
        try:
            out = await coro_factory()
        except Exception as exc:
            print(f"  {name} #{i + 1}: 失败 {exc}")
            continue
        print(f"  {name} #{i + 1}: {(time.time() - started) * 1000:.0f}ms  {str(out)[:60]}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 延迟基准测试")
    parser.add_argument("--times", type=int, default=3, help="每个用例跑几次")
    args = parser.parse_args()

    cfg = load_config()
    llm = LLMClient(cfg.llm)
    print(f"默认 provider: {cfg.llm.default_provider}  model: {cfg.llm.model}")
    print(f"base_url: {cfg.llm.base_url}")

    print("\n角色绑定:")
    for role in ("chat", "classify", "tool"):
        targets = llm.resolve(role)
        chain = " -> ".join(f"{t.provider}/{t.model}" for t in targets) or "(无)"
        print(f"  {role:9s} {chain}")

    print("\n分类 JSON (role=classify, max_tokens=128):")
    messages = [
        {"role": "system", "content": "只输出 JSON: {\"intent\":\"chat|tool|device\"}"},
        {"role": "user", "content": "你好，今天天气怎么样"},
    ]
    await bench(
        "classify",
        lambda: llm.json_completion(messages, max_tokens=128, role="classify"),
        args.times,
    )

    print("\n普通对话 (role=chat, max_tokens=128):")
    messages2 = [
        {"role": "system", "content": "用一句话回答"},
        {"role": "user", "content": "你好"},
    ]
    await bench(
        "chat",
        lambda: llm.chat(messages2, max_tokens=128, role="chat"),
        args.times,
    )

    await llm.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
