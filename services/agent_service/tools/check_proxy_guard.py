#!/usr/bin/env python3
"""验证: 环境里有 socks:// 代理变量时, Agent 不应再崩成 500。

背景: httpx 不认识 socks:// 代理协议, 构造 AsyncClient 时会直接抛
ValueError("Unknown scheme for proxy URL"), 冒泡到 FastAPI 就是 HTTP 500。
修复后 LLMClient 会先摘掉这类环境变量, 并把意外异常转成 LLMError。

用法:
    conda run -n botagent python tools/check_proxy_guard.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 必须在 import config 之前设置, 模拟用户环境里带着 socks 代理的情况
os.environ["ALL_PROXY"] = "socks://127.0.0.1:7897"
os.environ["SOCKS_PROXY"] = "socks://127.0.0.1:7897"

from config import load_config          # noqa: E402
from llm import LLMClient, LLMError      # noqa: E402


async def main() -> int:
    cfg = load_config()
    client = LLMClient(cfg.llm)

    print("1) 构造 AsyncClient (此前在这里直接抛 ValueError)")
    try:
        await client._get_client()
        print("   OK: 没有抛异常")
    except Exception as exc:
        print(f"   FAIL: {type(exc).__name__}: {exc}")
        await client.aclose()
        return 1

    print("2) 代理变量已被摘掉")
    for key in ("ALL_PROXY", "SOCKS_PROXY"):
        left = os.environ.get(key)
        if left:
            print(f"   FAIL: {key} 仍然存在 = {left}")
            await client.aclose()
            return 1
        print(f"   OK: {key} 已清除")

    print("3) 发起真实请求, 异常必须是 LLMError 而不是 ValueError")
    try:
        await client.chat([{"role": "user", "content": "你好"}])
        print("   OK: 请求成功")
    except LLMError as exc:
        print(f"   OK: 转成了 LLMError -> {str(exc)[:120]}")
    except Exception as exc:
        print(f"   FAIL: 未被转换: {type(exc).__name__}: {exc}")
        await client.aclose()
        return 1

    await client.aclose()
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
