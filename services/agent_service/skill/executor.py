"""Skill 执行引擎：执行 skill 并管理状态。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from skill.base import Skill
from skill.context import SkillContext
from skill.registry import SkillRegistry
from skill.resolver import SkillCall

logger = logging.getLogger("skill.executor")


@dataclass
class ExecutionResult:
    """一次执行序列的结果。"""

    ok: bool                                 # 是否成功
    final_output: str = ""                   # 最终输出
    final_reply: str = ""                    # 最终回复
    speak: bool = False                      # 是否要朗读
    error: str = ""                          # 错误信息
    action_trace: List[Dict[str, Any]] = None  # 执行追踪
    context_final: Optional[SkillContext] = None  # 最终上下文

    def __post_init__(self):
        if self.action_trace is None:
            self.action_trace = []


class SkillExecutor:
    """执行 skill 并管理状态。"""

    def __init__(self, registry: SkillRegistry):
        """
        初始化执行器。

        Args:
            registry: SkillRegistry
        """
        self.registry = registry

    async def execute_sequence(
        self,
        skill_calls: List[SkillCall],
        ctx: SkillContext,
        **extra_kwargs: Any,
    ) -> ExecutionResult:
        """
        多轮执行 skill 序列。

        Args:
            skill_calls: 要执行的 skill 调用列表
            ctx: 执行上下文
            **extra_kwargs: 额外参数（如 vision_client）

        Returns:
            ExecutionResult
        """
        if not skill_calls:
            return ExecutionResult(
                ok=False,
                error="没有 skill 调用",
                context_final=ctx,
            )

        logger.info(f"开始执行 {len(skill_calls)} 个 skill 调用")
        trace = []
        final_output = ""
        final_reply = ""
        final_speak = False

        try:
            for skill_call in skill_calls:
                if ctx.conversation_turn >= ctx.max_turns:
                    logger.warning(f"达到最大轮数 {ctx.max_turns}，停止执行")
                    break

                logger.info(f"执行 skill: {skill_call.skill_id}")
                started = time.time()

                # 创建 skill 实例
                skill = await self.registry.get_or_create_instance(
                    skill_call.skill_id, ctx, **extra_kwargs
                )
                if not skill:
                    error_msg = f"无法创建 skill: {skill_call.skill_id}"
                    logger.error(error_msg)
                    trace.append(
                        {
                            "skill_id": skill_call.skill_id,
                            "status": "error",
                            "error": error_msg,
                            "elapsed_ms": (time.time() - started) * 1000,
                        }
                    )
                    continue

                # 检查前置条件
                can_exec, reason = await skill.can_execute()
                if not can_exec:
                    logger.warning(f"Skill {skill_call.skill_id} 前置条件不满足: {reason}")
                    trace.append(
                        {
                            "skill_id": skill_call.skill_id,
                            "status": "skipped",
                            "reason": reason,
                        }
                    )
                    continue

                # 执行 skill
                try:
                    result = await skill.execute(skill_call.params)

                    # 更新上下文
                    if result.state_updates:
                        for key, value in result.state_updates.items():
                            ctx.set(key, value)

                    if result.perception_updates:
                        for key, value in result.perception_updates.items():
                            ctx.set_perception(key, value)

                    # 记录决策
                    ctx.add_decision(skill_call)

                    # 记录追踪
                    trace.append(
                        {
                            "skill_id": skill_call.skill_id,
                            "status": "ok" if result.ok else "failed",
                            "elapsed_ms": result.elapsed_ms,
                            "output": result.output[:100] if result.output else "",
                            "error": result.error,
                        }
                    )

                    # 保存最终输出
                    if result.output:
                        final_output = result.output
                    if result.reply:
                        final_reply = result.reply
                    if result.speak:
                        final_speak = result.speak

                    logger.info(f"Skill {skill_call.skill_id} 执行完成 ({result.elapsed_ms:.1f}ms)")

                    # 检查是否有下一步建议
                    if result.next_skills:
                        logger.debug(f"Skill 建议下一步: {result.next_skills}")
                        ctx.next_decision = SkillCall(
                            skill_id=result.next_skills[0], params={}
                        )

                except Exception as exc:
                    logger.exception(f"执行 Skill {skill_call.skill_id} 时出错")
                    trace.append(
                        {
                            "skill_id": skill_call.skill_id,
                            "status": "error",
                            "error": str(exc),
                            "elapsed_ms": (time.time() - started) * 1000,
                        }
                    )

                finally:
                    await skill.aclose()

            return ExecutionResult(
                ok=True,
                final_output=final_output,
                final_reply=final_reply,
                speak=final_speak,
                action_trace=trace,
                context_final=ctx,
            )

        except Exception as exc:
            logger.exception("执行序列时出错")
            return ExecutionResult(
                ok=False,
                error=str(exc),
                action_trace=trace,
                context_final=ctx,
            )

    async def _should_continue(
        self,
        result: Any,
        ctx: SkillContext,
    ) -> bool:
        """判断是否继续执行。"""
        # 简单规则：如果结果指定了下一步，就继续
        if hasattr(result, "next_skills") and result.next_skills:
            return True
        # 如果没达到最大轮数，也可以继续
        return ctx.conversation_turn < ctx.max_turns


__all__ = ["ExecutionResult", "SkillExecutor"]
