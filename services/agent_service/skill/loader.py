"""Skill 加载和初始化。

初始化所有内置 Skill，包括：
- 已实现的 Skill 类（vision.recognize、vision.grasp）
- 简单配置的 Skill Spec（weather、time、find_object）
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from skill.builtin.vision_recognize import VisionRecognizeSkill
from skill.builtin.vision_grasp import VisionGraspSkill
from skill.builtin.tool_skills import (
    weather_spec,
    time_spec,
    date_spec,
    find_object_spec,
    describe_scene_spec,
)
from skill.catalog import SkillCatalog

logger = logging.getLogger("skill.loader")


def load_all_skills(
    catalog: SkillCatalog,
    llm: Optional[Any] = None,
    disabled: Optional[List[str]] = None,
) -> None:
    """加载所有内置 Skill。

    Args:
        catalog: SkillCatalog 实例
        llm: LLM 客户端（可选，某些 Skill 可能需要）
        disabled: 要**跳过**的 skill id 列表 (configs/agent.yaml 的
            ``skills.disabled``)。关掉的 Skill 不会注册, 模型看不见它,
            路由也找不到它 —— 等于这个功能整个下线。
    """
    off = {str(x).strip() for x in (disabled or []) if str(x).strip()}

    def _on(spec: Any) -> bool:
        """该 Skill 是否要加载。"""
        if spec.id in off:
            logger.info("Skill %s 已按配置禁用, 跳过", spec.id)
            return False
        return True

    # 注册已实现的 Skill 类
    vision_recognize_spec = VisionRecognizeSkill.spec()
    if _on(vision_recognize_spec):
        catalog.register(
            vision_recognize_spec,
            lambda spec, ctx: VisionRecognizeSkill(spec, ctx)
        )

    vision_grasp_spec = VisionGraspSkill.spec()
    if _on(vision_grasp_spec):
        catalog.register(
            vision_grasp_spec,
            # vision_client / arm_client 由调用方经 get_or_create_instance 的 kwargs 传入
            lambda spec, ctx, **kw: VisionGraspSkill(spec, ctx, **kw)
        )

    # 注册配置型 Skill（无实现类，仅配置）
    # 这些 Skill 在 tool_handler 中被直接处理，不需要实例化
    for spec in (weather_spec, time_spec, date_spec, find_object_spec, describe_scene_spec):
        if _on(spec):
            catalog.register(spec)  # 无工厂函数

    loaded = len(catalog.get_all_specs())
    logger.info(f"已加载 {loaded} 个 Skill" + (f" (禁用 {len(off)} 个)" if off else ""))

    # 按分支统计
    for branch in ["chat", "tool", "device"]:
        specs = catalog.find_by_branch(branch)
        if specs:
            ids = [s.id for s in specs]
            logger.info(f"  {branch} 分支: {ids}")


__all__ = ["load_all_skills"]
