"""Skill 加载和初始化。

初始化所有内置 Skill，包括：
- 已实现的 Skill 类（vision.recognize、vision.grasp）
- 简单配置的 Skill Spec（weather、time、find_object）
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from skill.builtin.vision_recognize import VisionRecognizeSkill
from skill.builtin.vision_grasp import VisionGraspSkill
from skill.builtin.tool_skills import (
    weather_spec,
    time_spec,
    date_spec,
    find_object_spec,
)
from skill.catalog import SkillCatalog

logger = logging.getLogger("skill.loader")


def load_all_skills(catalog: SkillCatalog, llm: Optional[Any] = None) -> None:
    """加载所有内置 Skill。
    
    Args:
        catalog: SkillCatalog 实例
        llm: LLM 客户端（可选，某些 Skill 可能需要）
    """
    # 注册已实现的 Skill 类
    vision_recognize_spec = VisionRecognizeSkill.spec()
    catalog.register(
        vision_recognize_spec,
        lambda spec, ctx: VisionRecognizeSkill(spec, ctx)
    )

    vision_grasp_spec = VisionGraspSkill.spec()
    catalog.register(
        vision_grasp_spec,
        lambda spec, ctx: VisionGraspSkill(spec, ctx)
    )

    # 注册配置型 Skill（无实现类，仅配置）
    # 这些 Skill 在 tool_handler 中被直接处理，不需要实例化
    catalog.register(weather_spec)  # 无工厂函数
    catalog.register(time_spec)      # 无工厂函数
    catalog.register(date_spec)      # 无工厂函数
    catalog.register(find_object_spec)  # 无工厂函数

    logger.info(f"已加载 {len(catalog.get_all_specs())} 个 Skill")
    
    # 按分支统计
    for branch in ["chat", "tool", "device"]:
        specs = catalog.find_by_branch(branch)
        if specs:
            ids = [s.id for s in specs]
            logger.info(f"  {branch} 分支: {ids}")


__all__ = ["load_all_skills"]
