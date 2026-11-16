"""Skill 基类定义。"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple

from skill.result import SkillResult
from skill.spec import SkillSpec

logger = logging.getLogger("skill.base")


class Skill(ABC):
    """所有 Skill 的基类。"""

    def __init__(self, spec: SkillSpec, ctx: Optional[Any] = None):
        """
        初始化 Skill。

        Args:
            spec: Skill 规范定义
            ctx: SkillContext（如果有的话）
        """
        self.spec = spec
        self.ctx = ctx

    @abstractmethod
    async def execute(self, args: Dict[str, Any]) -> SkillResult:
        """
        执行 skill。

        Args:
            args: 参数字典

        Returns:
            SkillResult 对象，包含执行结果、状态更新、下一步建议等
        """
        raise NotImplementedError

    async def validate_args(self, args: Dict[str, Any]) -> Tuple[bool, str]:
        """
        参数校验。

        Args:
            args: 参数字典

        Returns:
            (是否有效, 错误信息或空字符串)
        """
        # 简单的必填字段检查
        if not self.spec.parameters:
            return True, ""

        required = self.spec.parameters.get("required", [])
        for key in required:
            if key not in args or args[key] in (None, ""):
                return False, f"缺少必填参数: {key}"

        return True, ""

    async def can_execute(self) -> Tuple[bool, str]:
        """
        前置条件检查（依赖的状态是否齐全）。

        Returns:
            (是否可执行, 原因)
        """
        if not self.ctx:
            return True, ""

        # 检查依赖的其他 skill 是否已执行
        for required_skill in self.spec.requires:
            decisions = self.ctx.decisions if hasattr(self.ctx, "decisions") else []
            if not any(d.skill_id == required_skill for d in decisions):
                return False, f"依赖 skill {required_skill} 未执行"

        # 检查依赖的状态字段是否存在
        state = self.ctx.state if hasattr(self.ctx, "state") else {}
        for required_state in self.spec.requires_state:
            if required_state not in state:
                return False, f"依赖状态 {required_state} 未设置"

        return True, ""

    async def aclose(self) -> None:
        """清理资源（如果需要）。"""
        pass

    def __repr__(self) -> str:
        return f"<Skill {self.spec.id} v{self.spec.version}>"


class SkillError(RuntimeError):
    """Skill 执行错误。"""

    pass


__all__ = ["Skill", "SkillError"]
