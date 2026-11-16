#!/usr/bin/env python3
"""Skill 工作流演示：展示完整的感知-决策-行动闭环。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 添加 sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from skill.loader import SkillLoader
from skill.resolver import SkillResolver, SkillCall
from skill.executor import SkillExecutor
from skill.context import SkillContext


async def demo_scenario_1():
    """场景 1：用户说「看看有什么」-> 识别物体"""
    print("\n" + "=" * 70)
    print("场景 1：图像识别 - 用户问「看看有什么」")
    print("=" * 70)

    # 初始化
    loader = SkillLoader()
    registry = await loader.load_all()
    resolver = SkillResolver(registry)
    executor = SkillExecutor(registry)

    # 创建上下文
    ctx = SkillContext(session_id="demo_1", request_id="req_001")

    # 用户意图
    user_input = "看看有什么"
    print(f"\n用户输入: 「{user_input}」")

    # 解析意图
    print("\n[步骤 1] 意图解析...")
    skill_calls = await resolver.resolve(user_input, use_llm=False)
    print(f"  → 解析结果: {len(skill_calls)} 个 skill 调用")
    for call in skill_calls:
        print(f"    • {call.skill_id}: {call.params}")

    # 执行
    print("\n[步骤 2] 执行 skill...")
    result = await executor.execute_sequence(skill_calls, ctx)
    print(f"  → 执行状态: {'成功' if result.ok else '失败'}")
    print(f"  → 最终输出: {result.final_output}")

    # 显示上下文
    print("\n[步骤 3] 执行后的上下文状态:")
    print(f"  • 当前轮数: {ctx.conversation_turn}")
    print(f"  • 已执行决策: {len(ctx.decisions)} 个")
    print(f"  • 状态字段: {list(ctx.state.keys())}")

    return ctx


async def demo_scenario_2():
    """场景 2：用户说「帮我抓个杯子」-> 识别+规划"""
    print("\n" + "=" * 70)
    print("场景 2：多轮编排 - 用户说「帮我抓个杯子」")
    print("=" * 70)

    # 初始化
    loader = SkillLoader()
    registry = await loader.load_all()
    resolver = SkillResolver(registry)
    executor = SkillExecutor(registry)

    # 创建上下文
    ctx = SkillContext(session_id="demo_2", request_id="req_002", max_turns=3)

    # 用户意图
    user_input = "帮我抓个杯子"
    print(f"\n用户输入: 「{user_input}」")

    # 解析意图（可能返回多个 skill）
    print("\n[步骤 1] 意图解析...")
    skill_calls = await resolver.resolve(user_input, use_llm=False)
    print(f"  → 解析结果: {len(skill_calls)} 个 skill 调用")
    for i, call in enumerate(skill_calls, 1):
        print(f"    {i}. {call.skill_id}")
        for k, v in call.params.items():
            print(f"       - {k}: {v}")

    # 执行
    print("\n[步骤 2] 多轮执行...")
    result = await executor.execute_sequence(skill_calls, ctx)
    print(f"  → 执行状态: {'成功' if result.ok else '失败'}")

    if result.action_trace:
        print("  → 执行追踪:")
        for trace in result.action_trace:
            print(f"    • {trace.get('skill_id')}: {trace.get('status')}")

    print(f"  → 最终输出: {result.final_output[:100]}...")

    # 显示最终上下文
    print("\n[步骤 3] 执行后的上下文状态:")
    print(f"  • 当前轮数: {ctx.conversation_turn}")
    print(f"  • 已执行决策: {len(ctx.decisions)} 个")
    print(f"  • 状态更新: {list(ctx.state.keys())}")
    if "grasp_target" in ctx.state:
        print(f"    - 抓取目标: {ctx.state['grasp_target'].get('name')}")
        target_pose = ctx.state["grasp_target"].get("target_pose")
        if target_pose:
            print(f"    - 目标位置: ({target_pose[0]:.3f}, {target_pose[1]:.3f}, {target_pose[2]:.3f})")

    return ctx


async def demo_state_management():
    """演示状态管理和上下文跟踪"""
    print("\n" + "=" * 70)
    print("演示：状态管理与上下文跟踪")
    print("=" * 70)

    ctx = SkillContext(session_id="demo_state", request_id="req_003")

    # 模拟多步操作
    operations = [
        ("robot_position", {"x": 0, "y": 0, "z": 0}),
        ("current_target", "杯子"),
        ("camera_frame", "arm_base"),
        ("grasp_success", True),
    ]

    print("\n设置多个状态字段:")
    for key, value in operations:
        ctx.set(key, value)
        print(f"  ✓ {key} = {value}")

    print("\n状态历史（最后 3 条）:")
    for record in ctx.state_history[-3:]:
        print(
            f"  • {record['key']}: {record['old_value']} → {record['new_value']}"
        )

    print("\n检索状态:")
    print(f"  • current_target = {ctx.get('current_target')}")
    print(f"  • camera_frame = {ctx.get('camera_frame')}")
    print(f"  • unknown_key = {ctx.get('unknown_key', '(未设置)')}")


async def main():
    """主演示函数。"""
    print("\n" + "=" * 70)
    print("Agent Skill Framework 演示")
    print("=" * 70)
    print("\n本演示展示四阶段框架的完整工作流:")
    print("  第一阶段: Skill 定义与封装")
    print("  第二阶段: 注册与发现")
    print("  第三阶段: 意图理解与调用编排")
    print("  第四阶段: 感知-决策-行动闭环")

    try:
        # 场景 1
        ctx1 = await demo_scenario_1()

        # 场景 2
        ctx2 = await demo_scenario_2()

        # 状态管理
        await demo_state_management()

        print("\n" + "=" * 70)
        print("✓ 演示完成")
        print("=" * 70)
        return 0

    except Exception as e:
        print(f"\n✗ 演示失败: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
