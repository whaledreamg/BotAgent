"""编排执行引擎：按照依赖关系执行多个任务。

支持：
- Sequential 顺序执行
- Parallel 并行执行
- DAG 有向无环图（未来扩展）
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    from task_parser import Task, TaskSequence
except ImportError:
    from .task_parser import Task, TaskSequence

logger = logging.getLogger("agent.orchestrator")


@dataclass
class TaskExecutionResult:
    """单个任务的执行结果。"""

    #: 任务的 tool 名称
    tool: str = ""
    #: 任务 order
    order: int = 0
    #: 执行状态：success, failed, timeout, skipped
    status: str = "pending"
    #: 返回的结果 (成功时)
    result: Any = None
    #: 错误信息 (失败时)
    error: str = ""
    #: 执行耗时 (ms)
    elapsed_ms: int = 0


@dataclass
class OrchestrationResult:
    """编排执行的最终结果。"""

    #: 所有任务的执行结果
    task_results: List[TaskExecutionResult] = field(default_factory=list)
    #: 是否全部成功
    all_success: bool = False
    #: 聚合结果文本 (供 TTS 播报)
    aggregated_text: str = ""
    #: 总耗时 (ms)
    total_elapsed_ms: int = 0
    #: 错误信息 (如果有)
    error: str = ""


class TaskOrchestrator:
    """任务编排引擎。"""

    def __init__(self, executor_func: callable, timeout_per_task: float = 5.0):
        """初始化编排器。

        Args:
            executor_func: 执行单个任务的异步函数
                          签名：async def execute_task(tool: str, params: Dict) -> Tuple[str, Any]
                          返回 (status, result)，其中 status 为 'ok' 或 'failed'
            timeout_per_task: 单个任务的超时时间 (秒)
        """
        self.executor_func = executor_func
        self.timeout_per_task = timeout_per_task

    async def execute(
        self, sequence: TaskSequence
    ) -> OrchestrationResult:
        """执行任务序列。

        Args:
            sequence: 任务序列 (来自 task_parser)

        Returns:
            OrchestrationResult 对象
        """
        result = OrchestrationResult()

        if not sequence.is_valid:
            result.error = f"任务序列无效: {sequence.raw_response}"
            logger.error(f"[编排] {result.error}")
            return result

        if not sequence.tasks:
            result.error = "任务序列为空"
            logger.error(f"[编排] {result.error}")
            return result

        # 根据序列类型选择执行策略
        if sequence.sequence_type == "parallel":
            await self._execute_parallel(sequence, result)
        elif sequence.sequence_type == "sequential":
            await self._execute_sequential(sequence, result)
        else:
            # 默认顺序执行
            await self._execute_sequential(sequence, result)

        # 聚合结果
        self._aggregate_results(result)

        result.all_success = all(r.status == "success" for r in result.task_results)
        logger.info(
            f"[编排] 完成: {len(result.task_results)} 个任务, "
            f"成功={result.all_success}, 耗时={result.total_elapsed_ms}ms"
        )

        return result

    async def _execute_sequential(
        self, sequence: TaskSequence, result: OrchestrationResult
    ):
        """顺序执行任务。"""
        logger.info(f"[编排] 顺序执行 {len(sequence.tasks)} 个任务")

        task_results_map: Dict[int, TaskExecutionResult] = {}

        for task in sequence.tasks:
            task_result = await self._execute_single_task(task)
            task_results_map[task.order] = task_result
            result.task_results.append(task_result)

            # 如果任务失败且有依赖任务，后续任务可能会跳过
            if task_result.status == "failed":
                logger.warning(
                    f"[编排] 任务 {task.order} ({task.tool}) 失败，"
                    f"可能影响后续依赖任务"
                )

    async def _execute_parallel(
        self, sequence: TaskSequence, result: OrchestrationResult
    ):
        """并行执行任务（假设它们无依赖）。"""
        logger.info(f"[编排] 并行执行 {len(sequence.tasks)} 个任务")

        # 创建所有任务的执行 coroutine
        tasks_coro = [self._execute_single_task(task) for task in sequence.tasks]

        # 并行执行
        task_results = await asyncio.gather(*tasks_coro, return_exceptions=True)

        # 处理结果
        for task_result in task_results:
            if isinstance(task_result, Exception):
                # 任务异常
                error_result = TaskExecutionResult(
                    status="failed", error=str(task_result)
                )
                result.task_results.append(error_result)
            else:
                result.task_results.append(task_result)

    async def _execute_single_task(self, task: Task) -> TaskExecutionResult:
        """执行单个任务。

        Args:
            task: Task 对象

        Returns:
            TaskExecutionResult 对象
        """
        task_result = TaskExecutionResult(tool=task.tool, order=task.order)

        logger.info(
            f"[编排] 执行任务 {task.order}: {task.tool} "
            f"params={task.params}"
        )

        try:
            # 使用 executor_func 执行任务
            status, result_data = await asyncio.wait_for(
                self.executor_func(task.tool, task.params),
                timeout=self.timeout_per_task,
            )

            task_result.status = "success" if status == "ok" else "failed"
            task_result.result = result_data

            if task_result.status == "success":
                logger.info(f"[编排] 任务 {task.order} 成功")
            else:
                logger.warning(f"[编排] 任务 {task.order} 失败: {result_data}")
                task_result.error = str(result_data)

        except asyncio.TimeoutError:
            task_result.status = "timeout"
            task_result.error = f"超时 ({self.timeout_per_task}s)"
            logger.error(f"[编排] 任务 {task.order} 超时")

        except Exception as e:
            task_result.status = "failed"
            task_result.error = str(e)
            logger.exception(f"[编排] 任务 {task.order} 异常: {e}")

        return task_result

    @staticmethod
    def _aggregate_results(result: OrchestrationResult):
        """聚合多个任务的结果为最终回复。"""
        if not result.task_results:
            result.aggregated_text = "没有任务执行。"
            return

        # 收集成功的结果
        success_results = [r for r in result.task_results if r.status == "success"]
        failed_results = [r for r in result.task_results if r.status != "success"]

        parts = []

        # 添加成功的结果
        for task_result in success_results:
            if task_result.result:
                parts.append(str(task_result.result))

        # 添加失败的信息
        if failed_results:
            error_parts = []
            for task_result in failed_results:
                if task_result.status == "timeout":
                    error_parts.append(
                        f"{task_result.tool}查询超时"
                    )
                else:
                    # 如果有详细的错误信息，使用它；否则用默认消息
                    error_msg = task_result.error or f"{task_result.tool}查询失败"
                    error_parts.append(error_msg)

            if error_parts:
                # 将失败信息附加到结果中
                parts.append("、".join(error_parts))

        result.aggregated_text = "。".join(parts) + "。" if parts else "操作完成。"


def create_orchestrator(
    executor_func: callable, timeout_per_task: float = 5.0
) -> TaskOrchestrator:
    """创建编排器。

    Args:
        executor_func: 执行单个任务的异步函数
        timeout_per_task: 单个任务的超时时间

    Returns:
        TaskOrchestrator 对象
    """
    return TaskOrchestrator(executor_func, timeout_per_task)
