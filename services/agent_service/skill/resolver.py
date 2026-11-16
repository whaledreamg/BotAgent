"""Skill 解析器：意图 -> Skill 调用序列。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from skill.registry import SkillRegistry

logger = logging.getLogger("skill.resolver")


@dataclass
class SkillCall:
    """一个 skill 调用。"""

    skill_id: str                           # 如 "vision.recognize"
    params: Dict[str, Any]                  # 参数
    source: str = "resolver"                # 来源（规则 / LLM）


class SkillResolver:
    """把用户意图解析成 skill 调用序列。"""

    def __init__(self, registry: SkillRegistry, llm: Optional[Any] = None):
        """
        初始化解析器。

        Args:
            registry: SkillRegistry
            llm: LLM 客户端（可选，用于 LLM 路径）
        """
        self.registry = registry
        self.llm = llm

    async def resolve(
        self,
        intent_text: str,
        use_llm: bool = True,
    ) -> List[SkillCall]:
        """
        把用户意图解析成 skill 调用序列。

        Args:
            intent_text: 用户的意图（如「帮我抓个杯子」）
            use_llm: 是否允许使用 LLM（如果规则未匹配）

        Returns:
            SkillCall 列表
        """
        logger.debug(f"解析意图: {intent_text}")

        # 1. 规则快速通道（简单的启发式匹配）
        calls = self._resolve_by_rules(intent_text)
        if calls:
            logger.info(f"规则匹配成功，得到 {len(calls)} 个 skill 调用")
            return calls

        # 2. LLM 路径（如果开启）
        if use_llm and self.llm:
            calls = await self._resolve_by_llm(intent_text)
            if calls:
                logger.info(f"LLM 解析成功，得到 {len(calls)} 个 skill 调用")
                return calls

        logger.warning("无法解析意图")
        return []

    def _resolve_by_rules(self, text: str) -> List[SkillCall]:
        """用简单规则解析（零延迟快速通道）。"""
        calls = []
        text_lower = text.lower()

        # 视觉识别类
        if any(kw in text_lower for kw in ["有什么", "看到", "识别", "有几个", "多少个", "有没有"]):
            calls.append(SkillCall(skill_id="vision.recognize", params={"question": text}))

        # 物体抓取类（如果提到特定物体名）
        target_keywords = {
            "杯子": "杯子",
            "瓶子": "瓶子",
            "球": "球",
            "苹果": "苹果",
            "螺丝": "螺丝",
        }
        for keyword, target_name in target_keywords.items():
            if keyword in text_lower and any(
                kw in text_lower for kw in ["抓", "拿", "取", "给我", "帮我"]
            ):
                calls.append(
                    SkillCall(
                        skill_id="vision.grasp",
                        params={"target": target_name, "frame": "arm_base"},
                    )
                )
                break

        return calls

    async def _resolve_by_llm(self, text: str) -> List[SkillCall]:
        """用 LLM 解析（可能有延迟）。"""
        if not self.llm:
            return []

        try:
            # 构造 prompt
            specs = self.registry.get_all_specs()
            tools_desc = "\n".join(
                f"- {s.id}: {s.name} - {s.description}" for s in specs
            )

            prompt = f"""根据用户意图，选择合适的 skill 调用。

可用的 skill:
{tools_desc}

用户意图: {text}

请返回 JSON 格式的结果:
{{
    "calls": [
        {{"skill_id": "...", "params": {{...}}}},
        ...
    ]
}}

仅返回 JSON，不要其他内容。"""

            messages = [{"role": "user", "content": prompt}]

            # 调用 LLM（简化版，实际应用中应该用 function calling）
            # result = await self.llm.json_completion(messages)
            logger.debug("LLM 解析（未实现）")
            return []

        except Exception as exc:
            logger.warning(f"LLM 解析失败: {exc}")
            return []


__all__ = ["SkillCall", "SkillResolver"]
