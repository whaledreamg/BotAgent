"""意图分流。

策略 (由 configs/agent.yaml 的 router.mode 控制):
- rules  : 只用关键词/正则规则, 零延迟、可离线
- llm    : 只用一次轻量 LLM 调用做分类
- hybrid : 规则优先, 未命中再走 LLM 兜底 (默认)

语音场景对延迟敏感, 因此 hybrid 模式下:
- LLM 兜底带超时 (router.llm_timeout, 默认 3s), 超时直接回落 default_route
- 规则支持 exclude 关键词, 用于把"说天气"这类闲聊从 tool 分支里排除掉
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from handlers.base import AgentRequest
from llm import LLMError
from registry import Registry

logger = logging.getLogger("router")


class Router:
    """把请求路由到具体分支。"""

    def __init__(self, cfg: Config, registry: Registry, llm: Any) -> None:
        self.cfg = cfg
        self.registry = registry
        self.llm = llm
        self._compiled: List[Tuple[re.Pattern, str, str]] = []
        self._excludes: List[Tuple[re.Pattern, str]] = []
        self._compile_rules()

    # ------------------------------------------------------------ 规则

    def _compile_rules(self) -> None:
        self._compiled = []
        self._excludes = []
        for rule in self.cfg.router.rules:
            intent = rule.get("intent")
            if not intent:
                continue
            for pattern in rule.get("patterns") or []:
                try:
                    self._compiled.append((re.compile(pattern), intent, str(pattern)))
                except re.error as exc:
                    logger.warning("忽略非法正则 %r: %s", pattern, exc)
            for kw in rule.get("keywords") or []:
                if kw:
                    self._compiled.append(
                        (re.compile(re.escape(str(kw))), intent, str(kw))
                    )
            # 排除词: 命中则该规则不生效 (用于区分闲聊与动作)
            for kw in rule.get("exclude") or []:
                if kw:
                    self._excludes.append((re.compile(re.escape(str(kw))), str(kw)))

    def match_rules(self, text: str) -> Optional[Tuple[str, str]]:
        """返回 (intent, 命中内容); 没命中返回 None。"""
        for pattern, intent, raw in self._compiled:
            if not pattern.search(text):
                continue
            # 命中排除词则跳过这条规则
            if any(ex.search(text) for ex, _ in self._excludes):
                continue
            return intent, raw
        return None

    # ------------------------------------------------------------ LLM

    async def classify_llm(self, req: AgentRequest) -> Optional[str]:
        """用 LLM 做意图分类, 失败或超时返回 None。"""
        if self.llm is None:
            return None
        prompt = self.cfg.router_prompt
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": req.text},
        ]
        timeout = max(0.5, self.cfg.router.llm_timeout)
        try:
            # 用流式: 分类结果只有几十个字符, 流式能拿到第一个 token 就开始解析,
            # 避免等待整个响应体。首字延迟是这里的主要成本。
            data = await asyncio.wait_for(
                self.llm.json_completion_stream(
                    messages,
                    model=self.cfg.router.llm_fallback_model or None,
                    temperature=0.0,
                    max_tokens=64,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("LLM 意图分类超时 (%.1fs), 回落默认分支", timeout)
            return None
        except LLMError as exc:
            logger.warning("LLM 意图分类失败: %s", exc)
            return None
        if not isinstance(data, dict):
            return None
        intent = data.get("intent") or data.get("route")
        if isinstance(intent, list):
            intent = intent[0] if intent else None
        if not intent:
            return None
        intent = str(intent).strip()
        if intent not in self.registry:
            logger.info("LLM 给出的意图 [%s] 无对应分支, 忽略", intent)
            return None
        return intent

    # ------------------------------------------------------------ 分流

    async def route(self, req: AgentRequest) -> Tuple[str, List[Dict[str, Any]]]:
        """返回 (intent, trace)。"""
        trace: List[Dict[str, Any]] = []
        mode = self.cfg.router.mode

        if mode in ("rules", "hybrid"):
            started = time.time()
            hit = self.match_rules(req.text)
            trace.append(
                {
                    "step": "rules",
                    "ms": round((time.time() - started) * 1000, 1),
                    "detail": f"{hit[0]}:{hit[1]}" if hit else "no-match",
                }
            )
            if hit and hit[0] in self.registry:
                # 规则是纯关键词匹配, 零延迟。trust_rules 打开时直接采信,
                # 不再花时间让 LLM 二次确认 —— 这一步原本要 1~3 秒。
                if self.cfg.router.trust_rules:
                    trace.append(
                        {"step": "trust", "ms": 0.0, "detail": f"skip-llm:{hit[0]}"}
                    )
                    return hit[0], trace
                confirmed = await self._confirm(req, hit[0], trace)
                if confirmed:
                    return hit[0], trace
            if mode == "rules":
                return self._default(trace)

        if mode in ("llm", "hybrid"):
            started = time.time()
            intent = await self.classify_llm(req)
            trace.append(
                {
                    "step": "llm",
                    "ms": round((time.time() - started) * 1000, 1),
                    "detail": intent or "failed",
                }
            )
            if intent:
                confirmed = await self._confirm(req, intent, trace)
                if confirmed:
                    return intent, trace

        return self._default(trace)

    async def _confirm(
        self, req: AgentRequest, intent: str, trace: List[Dict[str, Any]]
    ) -> bool:
        """让目标分支自己确认能否处理这个请求。

        LLM 的意图分类并不稳定 (同一句话可能时好时坏), 因此把决定权
        再交给分支自己: 分支返回 0 表示不认可, router 回落到默认分支。
        """
        handler = self.registry.get(intent)
        if handler is None:
            return False
        try:
            score = await handler.classify(req)
        except Exception as exc:  # pragma: no cover - 分支自检异常不致命
            logger.warning("分支 [%s] 自检异常: %s", intent, exc)
            return True
        trace.append(
            {"step": "confirm", "ms": 0.0, "detail": f"{intent}:{score:.2f}"}
        )
        return score > 0

    def _default(self, trace: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
        default = self.cfg.router.default_route
        if default not in self.registry:
            # 兜底: 任意一个已启用的分支
            names = self.registry.names()
            default = names[0] if names else ""
        trace.append({"step": "default", "ms": 0.0, "detail": default})
        return default, trace
