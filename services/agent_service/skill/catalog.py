"""Skill 目录和加载系统。

管理所有 Skill 的生命周期：
1. 注册 Skill 规范
2. 按需实例化 Skill
3. 提供查询和发现接口
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from skill.base import Skill
from skill.spec import SkillSpec

logger = logging.getLogger("skill.catalog")


class SkillCatalog:
    """Skill 目录和加载系统。
    
    管理两种类型的 Skill：
    1. 已实现的 Skill 类（vision.recognize、vision.grasp）
    2. 简单配置的 Skill Spec（weather、time、find_object）
    """

    def __init__(self):
        """初始化目录。"""
        # 按 ID 存储 Spec
        self._specs: Dict[str, SkillSpec] = {}

        # 按 ID 存储工厂函数（已实现的 Skill 类）
        self._factories: Dict[str, Callable[..., Skill]] = {}

        # 实例缓存（可选）
        self._instances: Dict[str, Skill] = {}

    def register(self, spec: SkillSpec, factory: Optional[Callable[..., Skill]] = None) -> None:
        """注册一个 Skill。
        
        Args:
            spec: SkillSpec 定义
            factory: Skill 的工厂函数（可选，如果 Skill 只是配置则不需要）
        """
        if spec.id in self._specs:
            logger.warning(f"Skill {spec.id} 已存在，将被覆盖")

        self._specs[spec.id] = spec
        if factory:
            self._factories[spec.id] = factory
        logger.info(f"已注册 Skill: {spec.id} ({spec.name})")

    def get_spec(self, skill_id: str) -> Optional[SkillSpec]:
        """按 ID 查询 spec。"""
        return self._specs.get(skill_id)

    def get_all_specs(self) -> List[SkillSpec]:
        """获取所有已注册的 spec。"""
        return list(self._specs.values())

    def find_by_branch(self, branch: str) -> List[SkillSpec]:
        """按分支查询。"""
        return [spec for spec in self._specs.values() if spec.branch == branch]

    def find_by_category(self, category: str) -> List[SkillSpec]:
        """按分类查询。"""
        return [spec for spec in self._specs.values() if spec.category == category]

    def to_openai_tools(self) -> List[Dict[str, Any]]:
        """转成 OpenAI function calling 格式。"""
        return [spec.to_openai_tool() for spec in self._specs.values()]

    def to_openai_tools_by_branch(self, branch: str) -> List[Dict[str, Any]]:
        """按分支转成 OpenAI function calling 格式。"""
        specs = self.find_by_branch(branch)
        return [spec.to_tool_call_definition() for spec in specs]

    async def get_or_create_instance(
        self,
        skill_id: str,
        ctx: Optional[Any] = None,
        **kwargs: Any,
    ) -> Optional[Skill]:
        """获取或创建 Skill 实例。
        
        Args:
            skill_id: Skill ID
            ctx: SkillContext（可选）
            **kwargs: 传递给工厂函数的额外参数

        Returns:
            Skill 实例或 None（如果未找到或没有实现）
        """
        spec = self._specs.get(skill_id)
        if not spec:
            logger.warning(f"Skill {skill_id} 未注册")
            return None

        # 如果没有工厂函数，说明这是一个配置型 Skill（不需要实例）
        factory = self._factories.get(skill_id)
        if not factory:
            logger.debug(f"Skill {skill_id} 没有实现类（配置型 Skill）")
            return None

        try:
            instance = factory(spec, ctx, **kwargs)
            return instance
        except Exception as exc:
            logger.exception(f"创建 Skill {skill_id} 实例失败: {exc}")
            return None

    def list_all(self) -> List[Dict[str, Any]]:
        """列出所有已注册的 Skill。"""
        return [
            {
                "id": spec.id,
                "name": spec.name,
                "branch": spec.branch,
                "category": spec.category,
                "source": spec.source,
                "tags": spec.tags,
            }
            for spec in self._specs.values()
        ]

    def __repr__(self) -> str:
        return f"<SkillCatalog with {len(self._specs)} skills>"
