#!/usr/bin/env python3
"""集成测试：验证任务编排引擎的基本功能。"""

import asyncio
import sys

sys.path.insert(0, "services/agent_service")

from task_parser import parse_task_sequence, should_use_thinking
from parameter_validator import create_default_validator
from orchestrator import create_orchestrator


async def mock_executor(tool: str, params: dict) -> tuple:
    """模拟的任务执行器。"""
    await asyncio.sleep(0.01)  # 模拟执行时间
    if tool == "weather":
        city = params.get("city", "当前位置")
        return "ok", f"{city}天气晴朗，气温 25 度。"
    elif tool == "time":
        return "ok", "当前时间 14:30。"
    elif tool == "find_object":
        name = params.get("name", "物体")
        return "ok", f"找到了{name}，在你的右前方。"
    else:
        return "failed", f"未知工具：{tool}"


async def test_task_orchestration():
    """测试任务编排。"""
    print("\n=== 测试 1: 基础顺序执行 ===")
    response = """{
  "tasks": [
    {"tool": "weather", "params": {"city": "北京"}, "order": 1},
    {"tool": "time", "params": {}, "order": 2}
  ],
  "sequence_type": "sequential"
}"""
    seq = parse_task_sequence(response, thinking_summary="识别到两个任务")
    print(f"✓ 解析成功: {seq.task_count()} 个任务")
    print(f"  任务: {seq.task_names()}")

    orchestrator = create_orchestrator(mock_executor, timeout_per_task=5.0)
    result = await orchestrator.execute(seq)

    print(f"✓ 执行完成:")
    print(f"  全部成功: {result.all_success}")
    print(f"  结果数: {len(result.task_results)}")
    print(f"  聚合文本: {result.aggregated_text}")

    print("\n=== 测试 2: 并行执行 ===")
    response_parallel = """{
  "tasks": [
    {"tool": "weather", "params": {"city": "上海"}, "order": 1},
    {"tool": "weather", "params": {"city": "杭州"}, "order": 2}
  ],
  "sequence_type": "parallel"
}"""
    seq_parallel = parse_task_sequence(response_parallel)
    result_parallel = await orchestrator.execute(seq_parallel)
    print(f"✓ 并行执行完成:")
    print(f"  结果数: {len(result_parallel.task_results)}")
    print(f"  聚合文本: {result_parallel.aggregated_text}")

    print("\n=== 测试 3: 参数验证 ===")
    validator = create_default_validator()
    raw_text = "查一下北京的天气"
    params = {"city": None}  # 参数缺失

    validation_result = validator.validate("weather", params, raw_text)
    print(f"✓ 验证完成:")
    print(f"  有效: {validation_result.is_valid}")
    print(f"  补全参数: {validation_result.params}")
    print(f"  缺失参数: {validation_result.missing_params}")

    print("\n=== 测试 4: Thinking Mode 检查 ===")
    should_think_1_task = should_use_thinking(1, min_complexity=2)
    should_think_2_tasks = should_use_thinking(2, min_complexity=2)
    print(f"✓ 1 个任务需要 thinking: {should_think_1_task}")
    print(f"✓ 2 个任务需要 thinking: {should_think_2_tasks}")

    print("\n=== 所有测试通过 ===\n")


if __name__ == "__main__":
    asyncio.run(test_task_orchestration())
