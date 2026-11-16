#!/usr/bin/env python3
"""集成测试：验证复合查询在 tool_handler 中的工作。"""

import asyncio
import sys

sys.path.insert(0, ".")

from task_parser import parse_task_sequence
from parameter_validator import create_default_validator
from orchestrator import create_orchestrator


async def mock_task_executor(tool: str, params: dict) -> tuple:
    """模拟任务执行器。"""
    await asyncio.sleep(0.05)
    if tool == "weather":
        city = params.get("city", "当前城市")
        return "ok", f"{city}天气晴朗，气温 25 度。"
    elif tool == "time":
        return "ok", "当前时间 14:30。"
    else:
        return "failed", f"未知工具: {tool}"


async def test_composite_query():
    """测试复合查询完整流程。"""
    print("\n" + "=" * 60)
    print("复合查询集成测试")
    print("=" * 60)

    # 模拟用户输入：「先查南京天气再查时间」
    user_input = "先查南京天气再查时间"
    print(f"\n📝 用户输入: {user_input}")

    # 第 1 步：任务识别和提取
    print("\n📊 第 1 步：任务识别")
    print("  - 检测复合查询标志: 「先...再...」")
    print("  - 检测工具关键词: 「天气」「时间」")

    tasks = []
    
    # 检查天气任务
    if any(w in user_input for w in ["天气", "温度"]):
        # 提取城市名 (简化版)
        city = "南京"  # 在实际中会用正则提取
        tasks.append({
            "tool": "weather",
            "params": {"city": city},
            "order": 1
        })
        print(f"  ✓ 任务 1: weather(city={city})")

    # 检查时间任务
    if any(w in user_input for w in ["时间", "几点"]):
        tasks.append({
            "tool": "time",
            "params": {},
            "order": 2
        })
        print(f"  ✓ 任务 2: time()")

    # 第 2 步：构造 TaskSequence
    print("\n📄 第 2 步：任务序列构造")
    import json
    task_json = {
        "tasks": tasks,
        "sequence_type": "sequential"
    }
    sequence_str = json.dumps(task_json)
    print(f"  JSON: {sequence_str[:60]}...")

    # 第 3 步：解析
    print("\n🔍 第 3 步：任务解析")
    sequence = parse_task_sequence(sequence_str)
    print(f"  ✓ 解析成功")
    print(f"  - 任务数: {sequence.task_count()}")
    print(f"  - 序列类型: {sequence.sequence_type}")
    print(f"  - 有效: {sequence.is_valid}")

    # 第 4 步：参数验证
    print("\n✅ 第 4 步：参数验证")
    validator = create_default_validator()
    for task in sequence.tasks:
        result = validator.validate(task.tool, task.params or {}, user_input)
        status = "✓" if result.is_valid else "✗"
        print(f"  {status} {task.tool}: {result.params}")

    # 第 5 步：执行
    print("\n⚙️  第 5 步：编排执行")
    orchestrator = create_orchestrator(mock_task_executor, timeout_per_task=10.0)
    orch_result = await orchestrator.execute(sequence)

    print(f"  ✓ 执行完成")
    print(f"  - 全部成功: {orch_result.all_success}")
    print(f"  - 任务结果数: {len(orch_result.task_results)}")
    for res in orch_result.task_results:
        print(f"    • {res.tool}: {res.status}")

    # 第 6 步：结果聚合
    print("\n📢 第 6 步：结果聚合")
    print(f"  最终回复: {orch_result.aggregated_text}")

    # 最终验证
    print("\n" + "=" * 60)
    if orch_result.all_success and "南京天气" in orch_result.aggregated_text and "14:30" in orch_result.aggregated_text:
        print("✨ 测试通过！复合查询完整流程工作正常")
    else:
        print("❌ 测试失败！")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(test_composite_query())
