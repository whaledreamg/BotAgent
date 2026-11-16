"""Skill 系统核心模块。"""

from skill.base import Skill, SkillError
from skill.result import SkillResult
from skill.spec import SkillSpec

__all__ = [
    "Skill",
    "SkillError",
    "SkillResult",
    "SkillSpec",
]
