"""device 分支: 机械臂动作控制。

职责:
1. 把用户说的话匹配到动作库里的动作名;
2. 调用 arm_service 下发播放;
3. 用一句话告诉用户「我在做什么」(要朗读, 否则用户不知道机器人听懂了没有)。

**动作名的来源**: arm_service 的动作库是「一个 JSON 文件 = 一个动作」,
文件名就是动作名 (挥手.json -> "挥手")。所以这里下发的就是动作名本身。

说法 -> 动作名的对应关系在 `configs/actions.yaml`, 不在代码里 ——
加动作只改配置, 不改代码。

匹配顺序 (先命中先用):
1. 精确等于某个动作名 (用户直接说了「左挥手」)
2. 命中 configs/actions.yaml 里的说法 (「挥个手」-> 左挥手)
3. 都没命中 -> LLM 从动作清单里挑一个 (带超时, 挑不中就说不支持)
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from arm_client import ArmClient, ArmError
from config import ArmActionConfig, Config
from handlers.base import AgentRequest, BranchResult, Handler

logger = logging.getLogger("agent.device")


class DeviceHandler(Handler):
    """机械臂动作分支。"""

    #: 该分支要朗读 —— 机械臂动了必须出声确认
    speak = True

    def __init__(self, config: Any = None, llm: Any = None) -> None:
        super().__init__(config=config, llm=llm)
        self._client: Optional[ArmClient] = None
        self._map: ArmActionConfig = self._load_map()
        #: 说法 -> 动作名 (按配置文件顺序, 先命中的优先)
        self._phrases: List[Tuple[str, str]] = []
        for item in self._map.actions:
            for phrase in item.get("say") or []:
                self._phrases.append((str(phrase), str(item["action"])))
        self._action_names: List[str] = [
            str(item["action"]) for item in self._map.actions
        ]

    # ------------------------------------------------------------ 配置

    def _load_map(self) -> ArmActionConfig:
        cfg = getattr(self.config, "arm_actions", None)
        if isinstance(cfg, ArmActionConfig):
            return cfg
        return ArmActionConfig()

    def _options(self) -> Dict[str, Any]:
        handlers = getattr(self.config, "handlers", None) or {}
        hcfg = handlers.get(self.name) if isinstance(handlers, dict) else None
        options = getattr(hcfg, "options", None)
        return options if isinstance(options, dict) else {}

    # ------------------------------------------------------------ 客户端

    def _get_client(self) -> ArmClient:
        if self._client is None:
            base_url = self._options().get("base_url") or self._map.base_url
            timeout = float(self._options().get("timeout") or self._map.timeout)
            self._client = ArmClient(base_url=base_url, timeout=timeout)
        return self._client

    async def ensure_actions(self) -> None:
        """拉一次动作清单 (懒加载), 供名称校验与 LLM 兜底用。"""
        client = self._get_client()
        if not client.actions:
            await client.load_actions()

    # ------------------------------------------------------------ 匹配

    def _is_question(self, text: str) -> bool:
        """疑问句式不算动作指令。"""
        for kw in self._map.exclude:
            if kw and kw in text:
                return True
        return False

    def _match_all(self, text: str) -> List[Tuple[str, int]]:
        """返回句子里命中的**所有**动作, 按在文本中的位置排序。

        返回 [(动作名, 首次出现位置), ...]。

    **为什么要按位置而不是按长度**: 用户说「先准备点然后挥挥手」,
    「准备点」在前面「挥手」在后面 —— 顺序就是用户要的执行顺序。
    按长度排会丢掉顺序信息。

    去重: 同一条说法可能被多个短语命中 (如「挥挥手」同时命中
    「挥手」和「挥挥手」), 同一个动作只算一次。
    """
        text = (text or "").strip()
        if not text or self._is_question(text):
            return []

        # (动作名, 位置) —— 取该动作所有命中里**最早**的位置
        hits: Dict[str, int] = {}
        for phrase, action in self._phrases:
            if not phrase:
                continue
            pos = text.find(phrase)
            if pos < 0:
                continue
            if action not in hits or pos < hits[action]:
                hits[action] = pos

        # 精确等于动作名 —— 整个句子就是动作名
        if text in self._action_names:
            return [(text, 0)]

        return sorted(hits.items(), key=lambda kv: kv[1])

    def _match(self, text: str) -> Tuple[Optional[str], str]:
        """返回 (动作名, 匹配方式)。匹配不到返回 (None, "")。

        **只返回一个动作** —— 复合指令 (「先A再B」) 由 `_match_all()` 处理,
        或者交给 tool calling 解析 (见 `classify`)。
        """
        text = (text or "").strip()
        if not text:
            return None, ""

        # 1) 精确等于动作名 —— 用户直接说了动作名
        if text in self._action_names:
            return text, "exact"

        if self._is_question(text):
            return None, ""

        # 2) 说法匹配 (子串)。**最长说法优先** ——
        #    「伸左手」比「伸手」长, 所以说「伸左手」时会先命中「伸左手」,
        #    不会先命中「伸手」。这样配置文件之间不需要排先后顺序,
        #    用户随便怎么放 yaml 都不会误匹配。
        best_action: Optional[str] = None
        best_len = 0
        for phrase, action in self._phrases:
            if not phrase or phrase not in text:
                continue
            if len(phrase) > best_len:
                best_action = action
                best_len = len(phrase)
        if best_action:
            return best_action, "phrase"

        return None, ""

    async def classify(self, req: AgentRequest) -> float:
        """能匹配到机械臂动作才认可。

        **复合指令 (命中多个动作) 也认可** —— 规则路径按动作在句子里出现的
        位置排序, 零延迟, 不依赖 LLM。

        为什么要这样: 之前复合指令一律返回 0.0 升级到 tool calling, 结果是
        复合指令**完全依赖 LLM** —— LLM 限流 (429) 或超时时, 整句话掉到
        chat 分支, 机器人什么都不做。位置排序虽然不如模型懂时序, 但
        「先A再B」这种最常见的说法位置排序就是对的, 没必要为它付 LLM 的
        延迟与失败风险。
        """
        hits = self._match_all(req.text)
        if len(hits) > 1:
            logger.info(
                "[机械臂] 命中 %d 个动作 (%s), 规则路径按位置排序",
                len(hits),
                ", ".join(a for a, _ in hits),
            )
            return 1.0
        name, _ = self._match(req.text)
        if name:
            return 1.0
        # 拿不准时让 LLM 判一次 (规则没命中, 但可能是别的说法)
        return 0.0

    # ------------------------------------------------------------ 主流程

    async def handle(self, req: AgentRequest) -> BranchResult:
        started = time.time()

        # 先看是不是复合指令 (命中多个动作)
        hits = self._match_all(req.text)
        if len(hits) > 1:
            # 复合指令: 按文本位置排序执行。
            #
            # 规则匹配只能拿到「有哪些动作」和它们的**大致**先后 (按在句子里
            # 出现的位置)。像「挥完手再准备点」这种带明确时序的复杂说法,
            # 位置排序可能不准 —— 但比起丢掉一个动作, 顺序错的风险小得多。
            #
            # 这条路径**不依赖 LLM** —— LLM 限流时复合指令照样能执行。
            names = [a for a, _ in hits]
            logger.info("[机械臂] 复合指令: %s", " -> ".join(names))
            result = await self.execute_many(names, source="phrase")
            result.trace.insert(
                0, self._trace("match", (time.time() - started) * 1000, "multi")
            )
            return result

        name, how = self._match(req.text)

        if name is None:
            # 规则没命中 -> LLM 从动作清单里挑
            name, how = await self._match_by_llm(req.text)
            if name is None:
                return BranchResult(
                    reply="暂不支持这个动作。",
                    speak=True,
                    action={
                        "name": "none",
                        "args": {},
                        "status": "unsupported",
                        "speak": True,
                    },
                    trace=[
                        self._trace(
                            "match",
                            (time.time() - started) * 1000,
                            "no-arm-action",
                        )
                    ],
                )

        # 执行交给 execute() —— tool calling 路径也走它, 保证两条路径
        # 的安全门阀行为与播报文案完全一致。
        result = await self.execute(name, source=how)

        # 把匹配耗时也记进 trace
        result.trace.insert(0, self._trace("match", (time.time() - started) * 1000, how))
        return result

    async def execute(
        self,
        name: str,
        args: Optional[Dict[str, Any]] = None,
        source: str = "rule",
    ) -> BranchResult:
        """执行一个已确定的动作名。"""
        return await self.execute_many([name], args=args, source=source)

    async def execute_many(
        self,
        names: List[str],
        args: Optional[Dict[str, Any]] = None,
        source: str = "rule",
    ) -> BranchResult:
        """执行一组已确定的动作 (按顺序)。

        **规则匹配路径与 tool calling 路径共用这一份实现** ——
        安全门阀预检、下发、播报文案全都一样, 两条路径不会出现行为差异。

        多个动作时整个队列**只入控一次** (arm_service 内部处理),
        动作之间不松手。

        `source` 只用于日志与 trace (rule / tool_call / llm)。
        """
        started = time.time()
        args = args or {}
        names = [str(n).strip() for n in (names or []) if str(n).strip()]
        if not names:
            return BranchResult(
                reply="没指定要执行的动作。",
                speak=True,
                action={"name": "none", "args": {}, "status": "failed", "speak": True},
                trace=[self._trace("execute", 0.0, "no-action")],
            )
        client = self._get_client()
        primary = names[0]

        # ---- 安全门阀预检 ----
        #
        # 先探一下机械臂状态再决定要不要动手。机械臂有急停/故障/未使能时,
        # 应该说清楚「现在动不了, 因为...」, 而不是先回一句「好的」然后
        # 机器人纹丝不动。
        safety = await client.check_safety()
        if not safety.get("ok", True):
            reason = str(safety.get("reason") or "机械臂现在不能动")
            logger.warning("[机械臂] 安全门阀拒绝 %r (%s): %s", primary, source, reason)
            return BranchResult(
                reply=f"{reason}，请先处理一下",
                speak=True,
                action={
                    "name": primary,
                    "args": args,
                    "status": "blocked",
                    "detail": reason,
                    "safety_code": str(safety.get("code") or ""),
                    "speak": True,
                },
                trace=[
                    self._trace(
                        "safety",
                        (time.time() - started) * 1000,
                        f"{primary}:{safety.get('code')}",
                    )
                ],
            )

        try:
            body = await client.play_many(names)
        except ArmError as exc:
            logger.error("[机械臂] 下发失败: %s", exc)
            return BranchResult(
                reply="机械臂现在动不了，请稍后再试。",
                speak=True,
                action={
                    "name": primary,
                    "args": args,
                    "status": "failed",
                    "detail": str(exc),
                    "speak": True,
                },
                trace=[
                    self._trace(
                        "execute", (time.time() - started) * 1000, f"{primary}:failed"
                    )
                ],
            )

        duration = body.get("duration", 0)
        logger.info(
            "[机械臂] 已下发 %s (%s, %.1fs), 服务端 phase=%s",
            " -> ".join(names),
            source,
            duration,
            body.get("phase"),
        )
        # 多个动作时把整串念出来 —— 用户要知道接下来会做什么
        reply = f"好的，{'、'.join(names)}"
        return BranchResult(
            reply=reply,
            speak=True,
            action={
                "name": primary,
                "args": args,
                "status": "ok",
                "duration": duration,
                "actions": names,
                "matched_by": source,
                "speak": True,
            },
            trace=[
                self._trace(
                    "execute", (time.time() - started) * 1000, f"{primary}:ok"
                )
            ],
        )

    # ------------------------------------------------------------ LLM 兜底

    async def _match_by_llm(self, text: str) -> Tuple[Optional[str], str]:
        """用 LLM 从动作清单里挑一个。

        只允许输出清单里的动作名 —— 防止模型编出不存在的动作。
        挑不中就返回 None (让用户知道不支持), 不能瞎猜。
        """
        if self.llm is None:
            return None, ""

        await self.ensure_actions()
        names = self._action_names or self._get_client().actions
        if not names:
            return None, ""

        timeout = float(self._options().get("llm_timeout", 1.2))
        prompt = (
            "从下面的机械臂动作名里挑一个与用户话语最匹配的动作, "
            "只输出动作名本身, 匹配不上就输出空字符串。\n"
            f"动作名: {', '.join(names)}\n"
            f"用户话语: {text}"
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ]

        try:
            import asyncio

            data = await asyncio.wait_for(
                self.llm.json_completion_stream(
                    messages, temperature=0.0, max_tokens=32
                ),
                timeout=timeout,
            )
        except Exception as exc:
            logger.info("[机械臂] LLM 兜底未命中 (%s)", exc)
            return None, ""

        if not isinstance(data, dict):
            return None, ""

        picked = str(
            data.get("action") or data.get("name") or data.get("match") or ""
        ).strip()
        # 只接受清单内的名字
        if picked and picked in names:
            return picked, "llm"
        return None, ""

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
