#!/usr/bin/env python3
"""LLM provider 连通性与能力探测。

回答三个问题:
1. 各家 provider 能不能连上? 延迟多少?
2. 哪家支持原生 function calling?
3. 三个角色 (chat / classify / tool) 最终绑到了谁?

用法:
    conda run -n botagent python tools/check_providers.py
    conda run -n botagent python tools/check_providers.py --provider deepseek
    conda run -n botagent python tools/check_providers.py --role tool
    conda run -n botagent python tools/check_providers.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config
from llm import LLMClient, LLMError

logging.basicConfig(level=logging.WARNING, format="%(message)s")

#: 探测用的最小工具 —— 只用来测「模型认不认 tools」, 不涉及任何真实动作
_PROBE_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "获取当前时间",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}

_PROBE_MESSAGES = [{"role": "user", "content": "现在几点?"}]


async def probe_chat(client: LLMClient, provider: str) -> Dict[str, Any]:
    """测一次普通对话。"""
    started = time.time()
    try:
        text = await client.chat(
            [{"role": "user", "content": "用一句话说说你好"}],
            role="chat",
            model=None,
        )
        return {
            "ok": True,
            "ms": int((time.time() - started) * 1000),
            "detail": (text or "")[:60],
        }
    except LLMError as exc:
        return {
            "ok": False,
            "ms": int((time.time() - started) * 1000),
            "detail": str(exc)[:120],
        }


async def probe_tools(client: LLMClient, provider: str) -> Dict[str, Any]:
    """测一次原生 function calling。"""
    started = time.time()
    try:
        result = await client.tool_call(
            _PROBE_MESSAGES,
            [_PROBE_TOOL],
            tool_choice="auto",
            role="tool",
        )
        names = [c.name for c in result.calls]
        return {
            "ok": True,
            "ms": int((time.time() - started) * 1000),
            "detail": (f"调用了 {names}" if names else "没调工具 (返回文本)"),
            "called": bool(names),
        }
    except LLMError as exc:
        return {
            "ok": False,
            "ms": int((time.time() - started) * 1000),
            "detail": str(exc)[:120],
        }


async def probe_provider(
    client: LLMClient, provider: str, want_tools: bool = True
) -> Dict[str, Any]:
    """探一家 provider。"""
    pcfg = client._providers().get(provider)
    if pcfg is None:
        return {"ok": False, "ms": 0, "detail": "provider 不存在"}

    out: Dict[str, Any] = {"provider": provider, "model": pcfg.model, "base_url": pcfg.base_url}

    if not pcfg.api_key and not pcfg.base_url.startswith("http://127.0.0.1"):
        # 没配 key 的远程 provider 直接跳过 —— 探测它只会拿到 401, 没意义
        out["skipped"] = "未配置 key"
        out["ok"] = False
        return out

    out["chat"] = await probe_chat(client, provider)
    if want_tools and pcfg.supports_tools:
        out["tools"] = await probe_tools(client, provider)
    return out


def print_roles(client: LLMClient) -> None:
    """打印三个角色最终解析出的候选链。"""
    print("\n=== 角色绑定 ===")
    for role in ("chat", "classify", "tool"):
        targets = client.resolve(role)
        if not targets:
            print(f"  {role:9s} -> (无)")
            continue
        chain = " -> ".join(f"{t.provider}/{t.model}" for t in targets)
        print(f"  {role:9s} -> {chain}")


def print_providers(client: LLMClient, results: List[Dict[str, Any]]) -> None:
    """打印探测结果表。"""
    print("\n=== provider 探测 ===")
    for r in results:
        if r.get("skipped"):
            print(f"  [跳过] {r['provider']:10s} {r.get('detail')}")
            continue
        chat = r.get("chat") or {}
        tools = r.get("tools")
        mark = "OK " if chat.get("ok") else "ERR"
        print(f"  [{mark}] {r['provider']:10s} model={r.get('model')}")
        print(f"         base={r.get('base_url')}")
        if chat.get("ok"):
            print(f"         chat: {chat['ms']}ms  {chat.get('detail')!r}")
        else:
            print(f"         chat: 失败 {chat.get('detail')}")
        if tools is not None:
            if tools.get("ok"):
                flag = "支持" if tools.get("called") else "未触发"
                print(f"         tools: {tools['ms']}ms  {flag} — {tools.get('detail')}")
            else:
                print(f"         tools: 失败 {tools.get('detail')}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="LLM provider 探测")
    parser.add_argument("--provider", help="只探指定 provider")
    parser.add_argument("--role", help="只探某个角色 (chat/classify/tool)")
    parser.add_argument("--list", action="store_true", help="只列出预设, 不发请求")
    parser.add_argument("--no-tools", action="store_true", help="跳过 function calling 探测")
    args = parser.parse_args()

    cfg = load_config()
    client = LLMClient(cfg.llm)

    print("=== 当前生效配置 ===")
    print(f"  default_provider: {cfg.llm.default_provider}")
    print(f"  扁平 model/base:  {cfg.llm.model} @ {cfg.llm.base_url}")
    print(f"  可用预设:         {', '.join(cfg.llm.provider_names) or '(无)'}")

    if args.list:
        print("\n=== provider 预设表 ===")
        for name, p in cfg.llm.providers.items():
            key = "有" if p.has_key else "无"
            notes = f"  ({p.notes})" if p.notes else ""
            print(f"  {name:10s} {p.base_url}")
            print(f"             model={p.model}  key={key}  tools={p.supports_tools}{notes}")
        print_roles(client)
        return 0

    print_roles(client)

    # 决定探哪些 provider
    if args.provider:
        names = [args.provider]
    elif args.role:
        names = list({t.provider for t in client.resolve(args.role)})
    else:
        names = list(cfg.llm.providers.keys())

    results = []
    for name in names:
        results.append(await probe_provider(client, name, want_tools=not args.no_tools))

    print_providers(client, results)

    await client.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
