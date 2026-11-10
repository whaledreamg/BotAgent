"""意图分流。

策略 (由 configs/agent.yaml 的 router.mode 控制):
- rules  : 只用关键词/正则规则, 零延迟、可离线
- llm    : 只用一次轻量 LLM 调用做分类
- hybrid : 规则优先, 未命中再走 LLM 兜底 (默认)

**tool calling 升级路径** (`router.tool_calling`):
规则没命中时, 不再走「LLM 分类意图 -> 分支内再调一次 LLM 选动作」的两段式,
而是直接用 Agnes 的**原生 function calling** 一次问出「要调哪个工具、
参数是什么」。一次往返同时完成分类与选动作。

收益:
1. 规则命中仍是**零延迟** —— 语音体感的快速通道完全保留;
2. 规则未命中时只花一次 LLM 往返 (原来要两次);
3. 纯闲聊时模型的 content 直接就是回复, 不用再调一次 chat。

语音场景对延迟敏感, 因此 tool calling 兜底带超时
(router.tool_calling.timeout), 超时直接回落 default_route。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from action_tools import (
    INTENT_BY_KIND,
    ActionCatalog,
    ActionSpec,
    build_tool_specs,
)
from config import Config
from handlers.base import AgentRequest
from llm import LLMError, ToolCall
from registry import Registry

logger = logging.getLogger("router")


@dataclass
class Decision:
    """一次分流的完整结论。"""

    #: 选中的分支 (chat / tool / device)
    intent: str = "chat"
    #: 规则命中时的命中内容
    matched: str = ""
    #: 命中的规则还是 LLM
    by: str = "rule"
    #: tool calling 选中的工具 (None = 没选工具)
    tool: Optional[ActionSpec] = None
    #: tool calling 选中的参数
    tool_args: Dict[str, Any] = field(default_factory=dict)
    #: tool calling 选中的**全部**工具调用 (模型可能一次给多个)
    tool_calls: List[ToolCall] = field(default_factory=list)
    #: 模型生成的文本 (纯 chat 场景直接用作回复, 省掉第二次 LLM 调用)
    content: str = ""
    #: 分流过程的 trace
    trace: List[Dict[str, Any]] = field(default_factory=list)


class Router:
    """把请求路由到具体分支。"""

    def __init__(self, cfg: Config, registry: Registry, llm: Any) -> None:
        self.cfg = cfg
        self.registry = registry
        self.llm = llm
        self._compiled: List[Tuple[re.Pattern, str, str, List[re.Pattern]]] = []
        self._compile_rules()
        # tool calling 的工具目录 —— 机械臂动作 + 设备工具
        self.catalog = ActionCatalog(
            build_tool_specs(cfg),
            device_handler=registry.get("device"),
            tool_handler=registry.get("tool"),
        )
        logger.info(
            "router 就绪: tools=%s tool_calling=%s",
            [s.name for s in self.catalog.specs],
            "on" if self._tool_calling_enabled() else "off",
        )

    def _tool_calling_enabled(self) -> bool:
        tc = getattr(self.cfg.router, "tool_calling", None)
        return bool(tc is not None and getattr(tc, "enabled", False))

    def _tool_calling_cfg(self) -> Any:
        tc = getattr(self.cfg.router, "tool_calling", None)
        if tc is None:
            return None
        return tc

    # ------------------------------------------------------------ 规则

    def _compile_rules(self) -> None:
        self._compiled = []
        for rule in self.cfg.router.rules:
            intent = rule.get("intent")
            if not intent:
                continue
            # 排除词必须**按规则隔离**:
            # 之前把所有规则的 exclude 汇总成一个全局列表, 导致 tool 规则的
            # 「介绍」会误伤 device 规则 —— 两条规则互相干扰。
            excludes: List[re.Pattern] = []
            for kw in rule.get("exclude") or []:
                if kw:
                    excludes.append(re.compile(re.escape(str(kw))))

            keywords = [str(k) for k in (rule.get("keywords") or [])]

            # 机械臂动作的说法不在 agent.yaml 里 —— 那是 arm_service 的领域知识,
            # 统一放在 configs/actions.yaml。这里按 intent 取过来。
            if intent == "device":
                keywords = self._arm_keywords() + keywords

            for pattern in rule.get("patterns") or []:
                try:
                    self._compiled.append(
                        (re.compile(pattern), intent, str(pattern), excludes)
                    )
                except re.error as exc:
                    logger.warning("忽略非法正则 %r: %s", pattern, exc)
            for kw in keywords:
                if kw:
                    self._compiled.append(
                        (
                            re.compile(re.escape(str(kw))),
                            intent,
                            str(kw),
                            excludes,
                        )
                    )

    def _arm_keywords(self) -> List[str]:
        """机械臂动作的说法 (来自 configs/actions.yaml)。"""
        arm = getattr(self.cfg, "arm_actions", None)
        if arm is None:
            return []
        return [str(p) for p in getattr(arm, "say_phrases", [])]

    def match_rules(self, text: str) -> Optional[Tuple[str, str]]:
        """返回 (intent, 命中内容); 没命中返回 None。

        命中多条规则时取**关键词最长**的那条 —— 说「伸左手」时,
        「伸左手」(3字) 应该赢过「伸手」(2字)。这样配置文件之间不需要
        排先后顺序, 加新说法不会意外改变已有匹配结果。
        """
        best: Optional[Tuple[str, str]] = None
        best_len = -1
        for pattern, intent, raw, excludes in self._compiled:
            if not pattern.search(text):
                continue
            # 命中本规则自己的排除词则跳过这条规则
            if any(ex.search(text) for ex in excludes):
                continue
            # 正则规则没有长度可比, 直接采用 (正则本来就比关键词精确)
            length = len(raw)
            if length > best_len:
                best = (intent, raw)
                best_len = length
        return best

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

    async def _handler_accepts(self, intent: str, req: AgentRequest) -> bool:
        """规则命中后, 分支能不能匹配到**具体动作**。

        与 `_confirm` 的区别: `_confirm` 是给 LLM 分类做二次确认用的
        (带 trace); 这里是给规则快速通道用的 —— 分支匹配不上具体动作时,
        要把机会让给 tool calling, 而不是直接回一句「暂不支持」。

        分支抛异常时一律按「接受」处理 (保守, 保持原有行为不变)。
        """
        handler = self.registry.get(intent)
        if handler is None:
            return False
        try:
            score = await handler.classify(req)
        except Exception as exc:  # pragma: no cover - 分支自检异常不致命
            logger.warning("分支 [%s] 自检异常: %s", intent, exc)
            return True
        return score > 0

    def _default(self, trace: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
        default = self.cfg.router.default_route
        if default not in self.registry:
            # 兜底: 任意一个已启用的分支
            names = self.registry.names()
            default = names[0] if names else ""
        trace.append({"step": "default", "ms": 0.0, "detail": default})
        return default, trace

    # ------------------------------------------------------------ tool calling

    #: 「假确认」的特征词 —— 模型用文字声称动作已完成, 但没调工具。
    #: 这时用户的设备其实什么都没动, 属于严重错误, 必须重试。
    _FAKE_CONFIRM_MARKERS = (
        "已关闭", "已打开", "已开启", "已关掉", "已开",
        "关掉了", "打开了", "开起来了", "关好了", "开好了",
        "已经关", "已经开", "帮你关", "帮你开", "帮你执行",
        "帮你做", "已经执行", "已执行", "已切换", "已播放",
        "完成了", "搞定了", "好了",
    )

    def _tool_intent(self, spec: ActionSpec) -> str:
        """某个 tool 命中后应该走哪个分支。

        分支名写在 tool 定义里 (`configs/tools.yaml` 的 `intent` 字段),
        没写就按 kind 推断 —— 所以加 tool 时不用改这里。
        """
        intent = str(getattr(spec, "intent", "") or "")
        if intent:
            return intent
        return INTENT_BY_KIND.get(spec.kind, "tool")

    def _looks_like_fake_confirm(self, content: str) -> bool:
        """判断模型是不是「用嘴说做完了」。

        模型偶尔会跳过工具调用, 直接回答「好的, 帮你关掉空调」——
        但设备其实什么都没动。这叫**假确认**, 是最不能接受的错误:
        用户以为动作做了, 实际没有。

        判据: 回复里出现了「已完成类」的措辞, 而且是在回答一个指令。
        """
        text = (content or "").strip()
        if not text:
            return False
        return any(marker in text for marker in self._FAKE_CONFIRM_MARKERS)

    def _tool_messages(self, req: AgentRequest) -> List[Dict[str, Any]]:
        """构造 tool calling 的消息列表。

        用**专用的**系统提示词, 不用 chat 的 system_prompt ——
        后者的指令是「回答用户」, 会让模型用文字描述动作已完成 (假确认),
        而不是真的去调工具。
        """
        messages: List[Dict[str, Any]] = []
        tc = self._tool_calling_cfg()
        system = str(getattr(tc, "system_prompt", "") or "") if tc else ""
        if not system:
            system = getattr(self.cfg, "system_prompt", "")
        if system:
            messages.append({"role": "system", "content": system})
        messages.extend(req.history or [])
        messages.append({"role": "user", "content": req.text})
        return messages

    async def decide(self, req: AgentRequest) -> Decision:
        """分流, 返回完整结论。

        流程:
        1. **规则命中** -> 直接返回, 零延迟 (不调 LLM)
        2. **规则未命中** -> 走原生 function calling:
           - 模型要调工具 -> 带上工具与参数
           - 模型不调工具  -> 它的 content 就是闲聊回复 (省掉第二次 LLM)
        3. tool calling 关掉/超时/出错 -> 回落原有的 LLM 分类 + 默认分支
        """
        trace: List[Dict[str, Any]] = []
        mode = self.cfg.router.mode

        # ---- 1. 规则快速通道 ----
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
                if self.cfg.router.trust_rules:
                    # 规则命中不代表**具体动作**能匹配上。
                    # 例: 规则里的关键词有「打开」, 但 TOOLS 表的正则要求
                    # 「打开」后面紧跟「灯/电灯/台灯」—— 说「帮我把灯打开」
                    # 规则命中了, 正则却匹配不上, 结果就是「暂不支持」。
                    #
                    # tool calling 可用时, 把这种「规则命中但匹配不上」的
                    # 情况升级给模型去判断到底想开哪个设备。
                    # tool calling 关闭时保持原样 (规则命中就交给分支,
                    # 由分支自己回报「暂不支持」)。
                    needs_escalation = self._tool_calling_enabled() and not (
                        await self._handler_accepts(hit[0], req)
                    )
                    if needs_escalation:
                        trace.append(
                            {
                                "step": "rules",
                                "ms": 0.0,
                                "detail": f"{hit[0]}:no-specific-match",
                            }
                        )
                    else:
                        trace.append(
                            {"step": "trust", "ms": 0.0, "detail": f"skip-llm:{hit[0]}"}
                        )
                        return Decision(
                            intent=hit[0], matched=hit[1], by="rule", trace=trace
                        )
                else:
                    confirmed = await self._confirm(req, hit[0], trace)
                    if confirmed:
                        return Decision(
                            intent=hit[0], matched=hit[1], by="rule", trace=trace
                        )
            if mode == "rules" and not self._tool_calling_enabled():
                intent, _ = self._default(trace)
                return Decision(intent=intent, by="rule", trace=trace)

        # ---- 2. tool calling 升级路径 ----
        if self._tool_calling_enabled() and self.llm is not None:
            decision, llm_dead = await self._decide_by_tools(req, trace)
            if decision is not None:
                return decision
            if llm_dead:
                # LLM 本身不可用 (限流/网络错误) -> 再调一次只会再失败一次,
                # 直接走默认分支, 不浪费往返。
                logger.warning("LLM 不可用, 直接回落默认分支")
                intent, _ = self._default(trace)
                return Decision(intent=intent, by="default", trace=trace)

        # ---- 3. 回落: 原有 LLM 分类 + 默认分支 ----
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
                    return Decision(intent=intent, by="llm", trace=trace)

        intent, _ = self._default(trace)
        return Decision(intent=intent, by="default", trace=trace)

    async def _decide_by_tools(
        self, req: AgentRequest, trace: List[Dict[str, Any]]
    ) -> Tuple[Optional[Decision], bool]:
        """用原生 function calling 决定动作。

        返回 (decision, llm_dead):
        - decision 非 None -> 用它
        - decision 为 None 且 llm_dead=True  -> LLM 本身不可用, 别再试了
        - decision 为 None 且 llm_dead=False -> 这条路没走通, 可以回落
        """
        tc = self._tool_calling_cfg()
        if tc is None or self.llm is None:
            return None, False

        tools = self.catalog.to_openai()
        if not tools:
            return None, False

        timeout = max(0.5, float(getattr(tc, "timeout", 5.0)))
        started = time.time()
        try:
            result = await asyncio.wait_for(
                self.llm.tool_call(
                    self._tool_messages(req),
                    tools,
                    tool_choice=getattr(tc, "tool_choice", "auto"),
                    model=getattr(tc, "model", "") or None,
                    temperature=getattr(tc, "temperature", 0.0),
                    max_tokens=getattr(tc, "max_tokens", 256),
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("tool calling 超时 (%.1fs), 回落默认分支", timeout)
            trace.append({"step": "tools", "ms": timeout * 1000, "detail": "timeout"})
            return None, True
        except LLMError as exc:
            logger.warning("tool calling 失败: %s", exc)
            trace.append({"step": "tools", "ms": 0.0, "detail": "error"})
            return None, True

        elapsed = round((time.time() - started) * 1000, 1)

        # ---- 模型没调工具 ----
        if not result.has_calls:
            content = (result.content or "").strip()

            # **假确认检测**: 模型用文字说「帮你关掉空调」, 但没调工具。
            # 设备其实什么都没动, 用户却以为做了 —— 这是最严重的错误。
            # 这时用 tool_choice=required 强制它必须调工具, 重试一次。
            if self._looks_like_fake_confirm(content):
                logger.warning("检测到假确认 (%r), 用 required 重试", content[:40])
                trace.append(
                    {"step": "tools", "ms": elapsed, "detail": "fake-confirm"}
                )
                retried = await self._retry_with_required(req, tools, tc, trace)
                if retried is not None:
                    return retried, False
                # 重试也没成 -> 老实说做不到, 不要假装做完了
                return (
                    Decision(
                        intent=self.cfg.router.default_route,
                        by="tools",
                        content="抱歉，我没能执行这个操作，请再说一遍。",
                        trace=trace,
                    ),
                    False,
                )

            trace.append(
                {"step": "tools", "ms": elapsed, "detail": f"chat:{len(content)}chars"}
            )
            default = self.cfg.router.default_route
            if default not in self.registry:
                default = self.registry.names()[0] if self.registry.names() else "chat"
            # content 留空时不能直接返回 —— 交给 chat 分支再问一次
            if not content:
                return None, False
            return (
                Decision(intent=default, by="tools", content=content, trace=trace),
                False,
            )

        # ---- 模型调了工具 ----
        # 模型可能一次给多个调用 (实测会), 全部保留下来按序执行。
        known: List[Tuple[ToolCall, ActionSpec]] = []
        for call in result.calls:
            spec = self.catalog.get(call.name)
            if spec is None:
                # 模型幻觉出不存在的工具 -> 忽略这一个, 不影响其它
                logger.warning("模型选了未知工具 %r, 忽略", call.name)
                continue
            if self._tool_intent(spec) not in self.registry:
                logger.warning("工具 %r 没有对应分支, 忽略", call.name)
                continue
            known.append((call, spec))

        if not known:
            trace.append(
                {"step": "tools", "ms": elapsed, "detail": "no-valid-tool"}
            )
            return None, False

        first_call, first_spec = known[0]
        intent = self._tool_intent(first_spec)
        trace.append(
            {
                "step": "tools",
                "ms": elapsed,
                "detail": f"{first_call.name}:{intent}:{len(known)}",
            }
        )
        return (
            Decision(
                intent=intent,
                matched=first_call.name,
                by="tools",
                tool=first_spec,
                tool_args=first_call.arguments,
                tool_calls=[c for c, _ in known],
                content=(result.content or "").strip(),
                trace=trace,
            ),
            False,
        )

    async def _retry_with_required(
        self,
        req: AgentRequest,
        tools: List[Dict[str, Any]],
        tc: Any,
        trace: List[Dict[str, Any]],
    ) -> Optional[Decision]:
        """用 tool_choice=required 强制模型调工具, 重试一次。

        只在检测到**假确认**时用 (模型用文字声称动作已完成)。
        纯闲聊不会被重试 —— 闲聊本来就不该调工具。
        """
        timeout = max(0.5, float(getattr(tc, "timeout", 5.0)))
        started = time.time()
        try:
            result = await asyncio.wait_for(
                self.llm.tool_call(
                    self._tool_messages(req),
                    tools,
                    tool_choice="required",
                    model=getattr(tc, "model", "") or None,
                    temperature=getattr(tc, "temperature", 0.0),
                    max_tokens=getattr(tc, "max_tokens", 256),
                ),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, LLMError) as exc:
            logger.warning("required 重试失败: %s", exc)
            trace.append({"step": "tools-retry", "ms": 0.0, "detail": "error"})
            return None

        elapsed = round((time.time() - started) * 1000, 1)
        if not result.has_calls:
            trace.append({"step": "tools-retry", "ms": elapsed, "detail": "no-call"})
            return None

        known: List[Tuple[ToolCall, ActionSpec]] = []
        for call in result.calls:
            spec = self.catalog.get(call.name)
            if spec is None:
                continue
            if self._tool_intent(spec) not in self.registry:
                continue
            known.append((call, spec))

        if not known:
            trace.append({"step": "tools-retry", "ms": elapsed, "detail": "no-valid"})
            return None

        first_call, first_spec = known[0]
        intent = self._tool_intent(first_spec)
        trace.append(
            {
                "step": "tools-retry",
                "ms": elapsed,
                "detail": f"{first_call.name}:{intent}",
            }
        )
        return Decision(
            intent=intent,
            matched=first_call.name,
            by="tools",
            tool=first_spec,
            tool_args=first_call.arguments,
            tool_calls=[c for c, _ in known],
            content=(result.content or "").strip(),
            trace=trace,
        )
