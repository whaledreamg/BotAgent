"""chat 分支: 通用中文语音对话。

产出 speak=True 的文本, 由 ASR 侧送去 TTS 合成并播放。
"""

from __future__ import annotations

import time
from typing import List

from event_bus import bus
from handlers.base import AgentRequest, BranchResult, Handler
from llm import LLMError


class ChatHandler(Handler):
    """闲聊 / 问答分支。"""

    speak = True

    async def classify(self, req: AgentRequest) -> float:
        """chat 是兜底分支, 任何输入都接受。"""
        return 1.0

    async def handle(self, req: AgentRequest) -> BranchResult:
        messages: List[dict] = []
        system = getattr(self.config, "system_prompt", "")
        if system:
            messages.append({"role": "system", "content": system})
        messages.extend(req.history or [])
        messages.append({"role": "user", "content": req.text})

        # ---- 思考开关 ----
        #
        # 由 configs/agent.yaml 的 `llm.thinking.enabled` 控制。
        #
        # 之前 chat 分支**完全无法思考** —— thinking 参数只在 tool_call
        # 路径上传, 而闲聊走的是 llm.chat()。现在两者打通了。
        #
        # 思考会多花几百毫秒和几百 token, 所以默认关闭;
        # 需要「模型先想清楚再答」的场景再打开。
        thinking_cfg = getattr(getattr(self.config, "llm", None), "thinking", None)
        thinking = thinking_cfg.to_payload() if thinking_cfg is not None else None

        started = time.time()
        try:
            result = await self.llm.chat_with_thinking(
                messages, role="chat", thinking=thinking
            )
            reply = result.content
        except LLMError as exc:
            return BranchResult(
                reply="抱歉，我这边服务暂时不可用，请稍后再试。",
                speak=True,
                trace=[self._trace("llm", (time.time() - started) * 1000, f"error: {exc}")],
            )

        elapsed_ms = int((time.time() - started) * 1000)

        # ---- 监控埋点: chat 的思考过程 ----
        #
        # 把模型「怎么想的」推给监控端。跟 tool_calling 那个 phase 分开,
        # 便于前端区分「闲聊时的思考」和「选工具时的思考」。
        if result.thinking.strip():
            bus.publish(
                "thinking",
                {
                    "phase": "chat",
                    "text": req.text,
                    # reasoning = 模型的思考过程 (reasoning_content)
                    "reasoning": result.thinking.strip(),
                    # content = 同一个内容, 换个名字 —— 跟 tool_calling phase
                    # 的字段名对齐, 前端读哪个都行
                    "content": result.thinking.strip(),
                    "provider": result.provider,
                    "model": result.model,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "reasoning_tokens": result.reasoning_tokens,
                    "elapsed_ms": elapsed_ms,
                },
                session_id=req.session_id,
                turn_id=req.turn_id,
            )

        reply = (reply or "").strip()
        if not reply:
            reply = "我没听清，可以再说一遍吗？"
        return BranchResult(
            reply=reply,
            speak=True,
            action=None,
            trace=[
                self._trace(
                    "llm",
                    elapsed_ms,
                    f"{len(reply)} chars, think={result.reasoning_tokens}",
                )
            ],
        )
