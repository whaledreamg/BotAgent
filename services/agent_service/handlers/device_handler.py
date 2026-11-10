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

    def _match(self, text: str) -> Tuple[Optional[str], str]:
        """返回 (动作名, 匹配方式)。匹配不到返回 (None, "")。"""
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
        """能匹配到机械臂动作才认可。"""
        name, _ = self._match(req.text)
        if name:
            return 1.0
        # 拿不准时让 LLM 判一次 (规则没命中, 但可能是别的说法)
        return 0.0

    # ------------------------------------------------------------ 主流程

    async def handle(self, req: AgentRequest) -> BranchResult:
        started = time.time()
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

        client = self._get_client()

        # ---- 安全门阀预检 ----
        #
        # 先探一下机械臂状态再决定要不要动手。机械臂有急停/故障/未使能时,
        # 应该说清楚「现在动不了, 因为...」, 而不是先回一句「好的」然后
        # 机器人纹丝不动。
        safety = await client.check_safety()
        if not safety.get("ok", True):
            reason = str(safety.get("reason") or "机械臂现在不能动")
            logger.warning("[机械臂] 安全门阀拒绝 %r: %s", name, reason)
            return BranchResult(
                reply=f"{reason}，请先处理一下",
                speak=True,
                action={
                    "name": name,
                    "args": {},
                    "status": "blocked",
                    "detail": reason,
                    "safety_code": str(safety.get("code") or ""),
                    "speak": True,
                },
                trace=[
                    self._trace(
                        "safety",
                        (time.time() - started) * 1000,
                        f"{name}:{safety.get('code')}",
                    )
                ],
            )

        try:
            body = await client.play(name)
        except ArmError as exc:
            logger.error("[机械臂] 下发失败: %s", exc)
            return BranchResult(
                reply="机械臂现在动不了，请稍后再试。",
                speak=True,
                action={
                    "name": name,
                    "args": {},
                    "status": "failed",
                    "detail": str(exc),
                    "speak": True,
                },
                trace=[
                    self._trace(
                        "execute", (time.time() - started) * 1000, f"{name}:failed"
                    )
                ],
            )

        duration = body.get("duration", 0)
        logger.info(
            "[机械臂] 已下发 %r (%s, %.1fs), 服务端 phase=%s",
            name,
            how,
            duration,
            body.get("phase"),
        )
        return BranchResult(
            reply=f"好的，{name}",
            speak=True,
            action={
                "name": name,
                "args": {},
                "status": "ok",
                "duration": duration,
                "matched_by": how,
                "speak": True,
            },
            trace=[
                self._trace(
                    "execute", (time.time() - started) * 1000, f"{name}:ok"
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
