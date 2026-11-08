"""chat 分支: 通用中文语音对话。

产出 speak=True 的文本, 由 ASR 侧送去 TTS 合成并播放。
"""

from __future__ import annotations

import time
from typing import List

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

        started = time.time()
        try:
            reply = await self.llm.chat(messages)
        except LLMError as exc:
            return BranchResult(
                reply="抱歉，我这边服务暂时不可用，请稍后再试。",
                speak=True,
                trace=[self._trace("llm", (time.time() - started) * 1000, f"error: {exc}")],
            )
        reply = (reply or "").strip()
        if not reply:
            reply = "我没听清，可以再说一遍吗？"
        return BranchResult(
            reply=reply,
            speak=True,
            action=None,
            trace=[self._trace("llm", (time.time() - started) * 1000, f"{len(reply)} chars")],
        )
