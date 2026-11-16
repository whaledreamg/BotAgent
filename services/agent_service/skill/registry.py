"""Skill 注册表与查询。"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from skill.base import Skill
from skill.spec import SkillSpec

logger = logging.getLogger("skill.registry")


class SkillRegistry:
    """Skill 注册表与查询系统。"""

    def __init__(self):
        """初始化注册表。"""
        #: 按 ID 存储 Spec
        self._specs: Dict[str, SkillSpec] = {}

        #: 按 ID 存储工厂函数
        self._factories: Dict[str, Callable[..., Skill]] = {}

        #: 实例缓存（可选）
        self._instances: Dict[str, Skill] = {}

    def register(self, spec: SkillSpec, factory: Callable[..., Skill]) -> None:
        """
        注册一个 skill。

        Args:
            spec: SkillSpec 定义
            factory: Skill 的工厂函数（调用时返回新实例）
        """
        if spec.id in self._specs:
            logger.warning(f"Skill {spec.id} 已存在，将被覆盖")

        self._specs[spec.id] = spec
        self._factories[spec.id] = factory
        logger.info(f"已注册 skill: {spec.id} ({spec.name})")

    def get_spec(self, skill_id: str) -> Optional[SkillSpec]:
        """
        按 ID 查询 spec。

        Args:
            skill_id: Skill ID（如 "vision.recognize"）

        Returns:
            SkillSpec 或 None
        """
        return self._specs.get(skill_id)

    def get_all_specs(self) -> List[SkillSpec]:
        """获取所有已注册的 spec。"""
        return list(self._specs.values())

    def find_by_category(self, category: str) -> List[SkillSpec]:
        """
        按分类查询。

        Args:
            category: 分类名（如 "vision"）

        Returns:
            匹配的 SkillSpec 列表
        """
        return [spec for spec in self._specs.values() if spec.category == category]

    def find_by_tag(self, tag: str) -> List[SkillSpec]:
        """
        按标签查询。

        Args:
            tag: 标签名（如 "realtime"）

        Returns:
            匹配的 SkillSpec 列表
        """
        return [spec for spec in self._specs.values() if tag in spec.tags]

    def find_by_name_pattern(self, pattern: str) -> List[SkillSpec]:
        """
        模糊匹配名称。

        Args:
            pattern: 名称模式

        Returns:
            匹配的 SkillSpec 列表
        """
        pattern_lower = pattern.lower()
        return [
            spec
            for spec in self._specs.values()
            if pattern_lower in spec.name.lower()
            or pattern_lower in spec.description.lower()
        ]

    def find_by_branch(self, branch: str) -> List[SkillSpec]:
        """Phase 2：按分支查询。
        
        用于 Router 的动态工具发现 —— 从 registry 获取指定分支的所有 Skill。
        
        Args:
            branch: 分支名 ("chat" / "tool" / "device")
        
        Returns:
            该分支的所有 SkillSpec 列表
        """
        return [
            spec for spec in self._specs.values()
            if spec.branch == branch
        ]

    async def get_or_create_instance(
        self,
        skill_id: str,
        ctx: Optional[Any] = None,
        **kwargs: Any,
    ) -> Optional[Skill]:
        """
        获取或创建 skill 实例。

        Args:
            skill_id: Skill ID
            ctx: SkillContext（可选）
            **kwargs: 传递给工厂函数的额外参数

        Returns:
            Skill 实例或 None（如果未找到）
        """
        spec = self._specs.get(skill_id)
        if not spec:
            logger.warning(f"Skill {skill_id} 未注册")
            return None

        factory = self._factories.get(skill_id)
        if not factory:
            logger.warning(f"Skill {skill_id} 没有工厂函数")
            return None

        try:
            instance = factory(spec, ctx, **kwargs)
            return instance
        except Exception as exc:
            logger.exception(f"创建 Skill {skill_id} 实例失败: {exc}")
            return None

    def to_openai_tools(self) -> List[Dict[str, Any]]:
        """
        转成 OpenAI function calling 格式。

        Returns:
            工具列表（可直接传给 LLM）
        """
        return [spec.to_openai_tool() for spec in self._specs.values()]

    def to_openai_tools_by_branch(self, branch: str) -> List[Dict[str, Any]]:
        """Phase 2：转成 OpenAI function calling 格式，仅包含指定分支的 Skill。
        
        用于 Tool calling 时只显示该分支的工具，避免混淆。
        
        Args:
            branch: 分支名 ("chat" / "tool" / "device")
        
        Returns:
            该分支的工具列表（可直接传给 LLM）
        """
        specs = self.find_by_branch(branch)
        return [spec.to_tool_call_definition() for spec in specs]

    def list_all(self) -> List[Dict[str, Any]]:
        """列出所有已注册的 skill。"""
        return [
            {
                "id": spec.id,
                "name": spec.name,
                "category": spec.category,
                "source": spec.source,
                "tags": spec.tags,
            }
            for spec in self._specs.values()
        ]

    def __repr__(self) -> str:
        return f"<SkillRegistry with {len(self._specs)} skills>"


__all__ = ["SkillRegistry"]
