#!/usr/bin/env python
"""一键查看 Agent 收到什么、怎么分流、回了什么。

用法:
    python tools/debug_dispatch.py "今天天气怎么样"
    python tools/debug_dispatch.py "打开客厅的灯" --session t1

只打 **Agent 这一层** (不含 ASR / TTS / 机械臂)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import DispatchRequest, Service  # noqa: E402
from handlers.base import AgentRequest  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description="查看 Agent 分流细节")
    ap.add_argument("text", help="用户话语")
    ap.add_argument("--session", default="debug", help="session_id")
    ap.add_argument("--history", action="store_true", help="带上会话历史")
    args = ap.parse_args()

    svc = Service()

    areq = AgentRequest(
        text=args.text,
        session_id=args.session,
        history=svc._history(args.session, args.history),
    )

    decision = await svc.router.decide(areq)
    print("=" * 68)
    print(f"输入       : {args.text}")
    print(f"分流结果   : intent={decision.intent}  by={decision.by}")
    print(f"匹配到     : {getattr(decision, 'matched', None)}")
    print(f"工具调用   : {getattr(decision, 'tool_calls', None)}")
    print("-" * 68)
    print("trace:")
    for step in decision.trace:
        print(f"  {step.get('step'):<14} {step.get('ms', 0):>8}ms  {step.get('detail')}")
    print("=" * 68)

    resp = await svc.dispatch(
        DispatchRequest(
            text=args.text, session_id=args.session, use_history=args.history
        )
    )
    print("最终响应:")
    print(json.dumps(resp.model_dump(), ensure_ascii=False, indent=2))
    await svc.llm.aclose()
    return 0


if __name__ == "__main__":
    # 打开 LLM 全文日志, 这样脚本输出里也能看到 prompt 和模型返回
    os.environ.setdefault("LLM_DEBUG", "1")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(asyncio.run(main()))
