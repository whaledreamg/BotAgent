#!/usr/bin/env python3
"""测量 LLM 首次响应延迟, 用于评估路由策略。

用法:
    conda run -n botagent python tools/bench_llm.py
"""

from __future__ import annotations

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
    cfg = load_config()
    llm = LLMClient(cfg.llm)
    print(f"模型: {cfg.llm.model}  base_url: {cfg.llm.base_url}")

    print("\n分类 JSON (max_tokens=128):")
    messages = [
        {"role": "system", "content": "只输出 JSON: {\"intent\":\"chat|tool|device\"}"},
        {"role": "user", "content": "你好，今天天气怎么样"},
    ]
    await bench("classify", lambda: llm.json_completion(messages, max_tokens=128))

    print("\n普通对话 (max_tokens=128):")
    messages2 = [
        {"role": "system", "content": "用一句话回答"},
        {"role": "user", "content": "你好"},
    ]
    await bench("chat", lambda: llm.chat(messages2, max_tokens=128))

    await llm.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
