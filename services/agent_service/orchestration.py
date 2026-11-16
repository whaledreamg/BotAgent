"""任务编排集成模块：将多个组件组合在一起。

负责：
1. 调用 LLM 返回任务序列
2. 解析任务序列
3. 验证和补全参数
4. 执行编排
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm import LLMError
from task_parser import TaskSequence, parse_task_sequence
from parameter_validator import ParameterValidator
from parameter_extraction_manager import ParameterExtractionManager
from orchestrator import TaskOrchestrator, create_orchestrator

logger = logging.getLogger("agent.orchestration")


class OrchestrationManager:
    """任务编排管理器：协调解析、验证、执行三个环节。"""

    def __init__(
        self,
        llm: Any,
        parameter_validator: Optional[ParameterValidator] = None,
        executor_func: Optional[Callable] = None,
        timeout_per_task: float = 5.0,
        extraction_manager: Optional[ParameterExtractionManager] = None,
    ):
        """初始化编排管理器。

        Args:
            llm: LLM 实例 (用于调用模型)
            parameter_validator: 参数验证器（可选）
            executor_func: 任务执行器函数
            timeout_per_task: 单个任务的超时时间
            extraction_manager: 混合参数提取管理器（可选）
        """
        self.llm = llm
        self.parameter_validator = parameter_validator
        self.executor_func = executor_func
        self.timeout_per_task = timeout_per_task
        # 如果没有提供 extraction_manager，为 parameter_validator 创建一个
        self.extraction_manager = extraction_manager
        if self.parameter_validator and not extraction_manager:
            self.extraction_manager = ParameterExtractionManager(llm=llm)
            self.parameter_validator.extraction_manager = self.extraction_manager

        if executor_func:
            self.orchestrator = create_orchestrator(executor_func, timeout_per_task)
        else:
            self.orchestrator = None

    async def process_composite_request(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_calling_config: Any,
        raw_text: str,
    ) -> Optional[TaskSequence]:
        """处理一个可能包含多个任务的用户请求。

        Args:
            messages: LLM 消息列表
            tools: 可用工具列表
            tool_calling_config: ToolCallingConfig 对象
            raw_text: 原始用户输入

        Returns:
            解析后的 TaskSequence 对象，如果失败返回 None
        """
        try:
            # 第 1 步：调用 LLM 并返回任务序列
            logger.info("[编排] 调用 LLM 返回任务序列...")

            # 准备 thinking 参数
            thinking_param = None
            if (
                hasattr(tool_calling_config, "thinking")
                and tool_calling_config.thinking
                and tool_calling_config.thinking.enabled
            ):
                thinking_param = {
                    "type": "enabled",
                    "budget_tokens": tool_calling_config.thinking.budget_tokens,
                }
                logger.info(
                    "[编排] 启用 Thinking Mode (budget=%d tokens)",
                    tool_calling_config.thinking.budget_tokens,
                )

            result = await self.llm.tool_call(
                messages,
                tools,
                tool_choice=getattr(tool_calling_config, "tool_choice", "auto"),
                temperature=getattr(tool_calling_config, "temperature", 0.0),
                max_tokens=getattr(tool_calling_config, "max_tokens", 256),
                role="tool_calling_branch",
                thinking=thinking_param,
            )

            # 第 2 步：解析任务序列
            logger.info("[编排] 解析 LLM 响应为任务序列...")
            thinking_summary = ""
            if hasattr(result, "thinking") and result.thinking:
                thinking_summary = str(result.thinking).strip()[:200]

            sequence = parse_task_sequence(result.content or "", thinking_summary)

            if not sequence.is_valid:
                logger.warning("[编排] 任务序列解析失败")
                return None

            logger.info(
                "[编排] 解析成功: %d 个任务, 类型=%s",
                sequence.task_count(),
                sequence.sequence_type,
            )

            # 第 3 步：验证和补全参数
            if self.parameter_validator:
                logger.info("[编排] 验证和补全参数...")
                for task in sequence.tasks:
                    # 使用异步验证方法，支持混合参数提取
                    validation_result = await self.parameter_validator.async_validate(
                        task.tool, task.params or {}, raw_text
                    )

                    if not validation_result.is_valid:
                        logger.warning(
                            f"[编排] 任务 {task.order}({task.tool}) 参数验证失败: "
                            f"缺失={validation_result.missing_params}"
                        )
                        # 如果参数补全失败，这个任务可能需要反问用户
                        # 这里暂时保留，由上层决定处理方式
                    else:
                        task.params = validation_result.params

            return sequence

        except LLMError as e:
            logger.error(f"[编排] LLM 调用失败: {e}")
            return None
        except Exception as e:
            logger.exception(f"[编排] 处理失败: {e}")
            return None

    async def execute_sequence(self, sequence: TaskSequence) -> Any:
        """执行一个任务序列。

        Args:
            sequence: 任务序列

        Returns:
            编排执行结果
        """
        if not self.orchestrator:
            logger.error("[编排] 编排器未初始化")
            return None

        try:
            result = await self.orchestrator.execute(sequence)
            return result
        except Exception as e:
            logger.exception(f"[编排] 执行失败: {e}")
            return None
