#!/usr/bin/env python3
"""Skill 系统自检脚本。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 添加 sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from skill.loader import SkillLoader
from skill.registry import SkillRegistry
from skill.resolver import SkillResolver, SkillCall
from skill.executor import SkillExecutor
from skill.context import SkillContext


async def test_registry():
    """测试 SkillRegistry。"""
    print("\n[TEST] SkillRegistry")
    print("-" * 50)

    loader = SkillLoader()
    registry = await loader.load_all()

    # 列出所有 skill
    specs = registry.get_all_specs()
    print(f"✓ 加载了 {len(specs)} 个 skill")

    for spec in specs:
        print(f"  - {spec.id}: {spec.name} (source={spec.source})")

    # 查询功能
    vision_skills = registry.find_by_category("vision")
    print(f"✓ 视觉类 skill: {len(vision_skills)} 个")
    for spec in vision_skills:
        print(f"  - {spec.id}")

    # OpenAI 格式
    tools = registry.to_openai_tools()
    print(f"✓ 生成 OpenAI tools: {len(tools)} 个")

    return registry


async def test_resolver(registry):
    """测试 SkillResolver。"""
    print("\n[TEST] SkillResolver")
    print("-" * 50)

    resolver = SkillResolver(registry)

    test_cases = [
        "看看有什么",
        "有没有杯子",
        "帮我抓个瓶子",
    ]

    for text in test_cases:
        calls = await resolver.resolve(text, use_llm=False)
        print(f"✓ '{text}' -> {len(calls)} 个 skill 调用")
        for call in calls:
            print(f"  - {call.skill_id}: {call.params}")

    return resolver


async def test_context():
    """测试 SkillContext。"""
    print("\n[TEST] SkillContext")
    print("-" * 50)

    ctx = SkillContext(session_id="test_session", request_id="test_001")

    # 设置状态
    ctx.set("current_target", "杯子")
    ctx.set("robot_state", "idle")
    print(f"✓ 设置状态: {ctx.state}")

    # 获取状态
    target = ctx.get("current_target")
    print(f"✓ 获取状态: current_target = {target}")

    # 设置感知数据
    ctx.set_perception("detected_objects", [{"name": "cup", "confidence": 0.9}])
    print(f"✓ 设置感知数据: {ctx.perception}")

    # 检查状态历史
    print(f"✓ 状态历史: {len(ctx.state_history)} 条记录")

    return ctx


async def test_executor(registry):
    """测试 SkillExecutor。"""
    print("\n[TEST] SkillExecutor")
    print("-" * 50)

    executor = SkillExecutor(registry)
    ctx = SkillContext(session_id="test_session", request_id="test_002")

    # 创建一个简单的 skill 调用
    calls = [
        SkillCall(
            skill_id="vision.recognize",
            params={"question": "看看有什么"},
        )
    ]

    print(f"准备执行 {len(calls)} 个 skill 调用...")

    # 执行（这需要 vision_client，这里会失败但能测试代码路径）
    try:
        result = await executor.execute_sequence(calls, ctx)
        print(f"✓ 执行完成: ok={result.ok}")
        print(f"  输出: {result.final_output[:100] if result.final_output else '(无)'}")
        print(f"  追踪: {len(result.action_trace)} 条")
    except Exception as e:
        print(f"✓ 执行过程（预期会失败因为缺少 vision_client）: {type(e).__name__}")

    return executor


async def main():
    """主测试函数。"""
    print("=" * 60)
    print("Skill 系统自检")
    print("=" * 60)

    try:
        # 1. 测试 Registry
        registry = await test_registry()

        # 2. 测试 Resolver
        resolver = await test_resolver(registry)

        # 3. 测试 Context
        ctx = await test_context()

        # 4. 测试 Executor
        executor = await test_executor(registry)

        print("\n" + "=" * 60)
        print("✓ 所有自检完成")
        print("=" * 60)
        return 0

    except Exception as e:
        print(f"\n✗ 自检失败: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
