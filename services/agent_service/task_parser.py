"""任务解析模块：从 LLM 返回的响应中解析出任务序列。

支持复合意图识别：
- 单任务：「查天气」
- 复合任务：「先查天气再查时间」
- 并行任务：「查上海和杭州的天气」
- 依赖任务：「根据天气建议穿衣」

使用 Thinking Mode 改进复合意图的理解。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("agent.task_parser")


@dataclass
class Task:
    """单个任务的定义。"""

    #: 工具/技能名称
    tool: str = ""
    #: 任务参数
    params: Dict[str, Any] = field(default_factory=dict)
    #: 任务执行顺序 (从 1 开始)
    order: int = 0
    #: 依赖的任务 order 列表 (为空则无依赖)
    depends_on: List[int] = field(default_factory=list)
    #: 任务的自然语言描述 (用于日志)
    description: str = ""

    def is_valid(self) -> bool:
        """检查任务定义是否有效。"""
        return bool(self.tool and self.order > 0)


@dataclass
class TaskSequence:
    """任务序列：编排引擎的输入。"""

    #: 所有任务列表
    tasks: List[Task] = field(default_factory=list)
    #: 序列类型：sequential (顺序)、parallel (并行)、dag (依赖)
    sequence_type: str = "sequential"
    #: 模型的思考过程总结 (来自 thinking mode)
    thinking_summary: str = ""
    #: 原始响应文本
    raw_response: str = ""
    #: 是否成功解析
    is_valid: bool = False

    def has_tasks(self) -> bool:
        """是否包含任务。"""
        return len(self.tasks) > 0

    def task_count(self) -> int:
        """任务数量。"""
        return len(self.tasks)

    def task_names(self) -> List[str]:
        """所有任务的工具名列表。"""
        return [t.tool for t in self.tasks]

    def validate(self) -> Tuple[bool, str]:
        """验证任务序列的有效性。

        Returns:
            (is_valid, error_message)
        """
        if not self.tasks:
            return False, "没有任务"

        # 检查每个任务都有效
        for task in self.tasks:
            if not task.is_valid():
                return False, f"无效的任务: {task}"

        # 检查 order 唯一性
        orders = [t.order for t in self.tasks]
        if len(orders) != len(set(orders)):
            return False, "任务 order 不唯一"

        # 检查依赖是否合法
        valid_orders = set(orders)
        for task in self.tasks:
            for dep in task.depends_on:
                if dep not in valid_orders:
                    return False, f"任务 {task.order} 的依赖 {dep} 不存在"

        return True, ""


def _extract_json_from_response(response: str) -> Optional[Dict[str, Any]]:
    """从 LLM 响应中提取 JSON 对象。

    LLM 可能返回 "... 让我为你执行以下任务 {...} ..."
    这个函数尝试找到并解析其中的 JSON。
    """
    if not response:
        return None

    # 尝试直接解析
    response = response.strip()
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass

    # 尝试找到 { ... } 块
    start = response.find("{")
    if start < 0:
        return None

    depth = 0
    for i in range(start, len(response)):
        if response[i] == "{":
            depth += 1
        elif response[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(response[start : i + 1])
                except json.JSONDecodeError:
                    pass

    return None


def parse_task_sequence(response: str, thinking_summary: str = "") -> TaskSequence:
    """从 LLM 响应解析任务序列。

    Args:
        response: LLM 的文本响应
        thinking_summary: 来自 thinking mode 的思考过程总结

    Returns:
        TaskSequence 对象
    """
    seq = TaskSequence(raw_response=response, thinking_summary=thinking_summary)

    # 尝试提取 JSON
    data = _extract_json_from_response(response)
    if not data:
        logger.warning("[任务解析] 无法从响应中提取 JSON: %s", response[:200])
        return seq

    # 检查是否是任务序列格式
    if not isinstance(data, dict) or "tasks" not in data:
        logger.warning("[任务解析] JSON 不包含 tasks 字段")
        return seq

    # 解析任务列表
    raw_tasks = data.get("tasks") or []
    if not isinstance(raw_tasks, list):
        logger.warning("[任务解析] tasks 不是列表")
        return seq

    tasks: List[Task] = []
    for i, raw_task in enumerate(raw_tasks):
        if not isinstance(raw_task, dict):
            logger.warning(f"[任务解析] 第 {i} 个任务不是字典")
            continue

        task = Task(
            tool=str(raw_task.get("tool", "")).strip(),
            params=raw_task.get("params", {}) if isinstance(raw_task.get("params"), dict) else {},
            order=int(raw_task.get("order", i + 1)),
            depends_on=raw_task.get("depends_on", [])
            if isinstance(raw_task.get("depends_on"), list)
            else [],
            description=str(raw_task.get("description", "")).strip(),
        )

        if task.tool:
            tasks.append(task)

    if not tasks:
        logger.warning("[任务解析] 没有有效任务")
        return seq

    seq.tasks = tasks
    seq.sequence_type = str(data.get("sequence_type", "sequential")).lower()

    # 验证
    is_valid, err = seq.validate()
    seq.is_valid = is_valid

    if not is_valid:
        logger.warning(f"[任务解析] 验证失败: {err}")
    else:
        logger.info(
            "[任务解析] 成功: %d 个任务, 类型=%s",
            len(tasks),
            seq.sequence_type,
        )

    return seq


def should_use_thinking(task_count: int, min_complexity: int = 2) -> bool:
    """判断是否应该启用 thinking mode。

    简单查询 (单一任务) 不需要 thinking, 节省成本。
    只有复杂意图 (多个任务) 才启用。

    Args:
        task_count: 识别到的任务数 (这个数字来自前一次的预测)
        min_complexity: 最少任务数阈值

    Returns:
        是否启用 thinking
    """
    return task_count >= min_complexity
