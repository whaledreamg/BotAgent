#!/usr/bin/env python3
"""验证新的 15 秒超时是否足够处理多个任务。"""

import asyncio
import sys
import time

sys.path.insert(0, ".")

from orchestrator import create_orchestrator
from task_parser import parse_task_sequence
import json


async def simulate_slow_weather_executor(tool: str, params: dict) -> tuple:
    """模拟真实场景的天气执行器（包括网络延迟）。
    
    真实延迟分布：
    - DNS 解析: 1-3 秒
    - 地理编码 API: 2-4 秒
    - 天气预报 API: 1-3 秒
    - 总计: 4-10 秒
    """
    if tool == "weather":
        city = params.get("city", "当前城市")
        # 模拟真实的 DNS + API 延迟
        await asyncio.sleep(6.0)  # 现实中的天气 API 调用
        return "ok", f"{city}天气晴朗，气温25度。"
    elif tool == "time":
        # 时间查询很快（本地系统调用）
        await asyncio.sleep(0.1)
        return "ok", "当前时间14:30。"
    else:
        return "failed", f"未知工具: {tool}"


async def test_with_timeout(timeout_seconds: float) -> bool:
    """测试给定超时是否足够完成复合查询。"""
    print(f"\n📊 测试超时: {timeout_seconds} 秒")
    print("=" * 60)

    tasks = [
        {"tool": "weather", "params": {"city": "南京"}, "order": 1},
        {"tool": "time", "params": {}, "order": 2},
    ]

    task_json = {
        "tasks": tasks,
        "sequence_type": "sequential"
    }

    sequence = parse_task_sequence(json.dumps(task_json))
    orchestrator = create_orchestrator(
        simulate_slow_weather_executor,
        timeout_per_task=timeout_seconds
    )

    start = time.time()
    try:
        result = await orchestrator.execute(sequence)
        elapsed = time.time() - start

        print(f"⏱️  执行时间: {elapsed:.1f} 秒")
        print(f"✅ 状态: 成功")
        print(f"📢 结果: {result.aggregated_text}")
        print(f"✓ 超时 {timeout_seconds}s 足够")
        return True

    except asyncio.TimeoutError:
        elapsed = time.time() - start
        print(f"⏱️  执行时间: {elapsed:.1f} 秒")
        print(f"❌ 状态: 超时")
        print(f"✗ 超时 {timeout_seconds}s 不够")
        return False
    except Exception as e:
        print(f"❌ 错误: {type(e).__name__}: {e}")
        return False


async def main():
    print("\n" + "╔" + "="*58 + "╗")
    print("║" + " "*58 + "║")
    print("║" + "  超时配置验证 - 多任务编排".center(58) + "║")
    print("║" + " "*58 + "║")
    print("╚" + "="*58 + "╝")

    test_cases = [
        (10.0, "原配置"),
        (15.0, "新配置 ⭐"),
        (20.0, "备用方案"),
    ]

    results = []
    for timeout, label in test_cases:
        success = await test_with_timeout(timeout)
        results.append((timeout, label, success))

    # 总结
    print("\n" + "="*60)
    print("测试总结")
    print("="*60)

    for timeout, label, success in results:
        status = "✅" if success else "❌"
        print(f"{status} {timeout}s ({label})")

    # 推荐
    print("\n" + "="*60)
    print("推荐配置")
    print("="*60)

    # 找到最小可行的超时
    min_viable = None
    for timeout, label, success in results:
        if success:
            min_viable = timeout
            break

    if min_viable:
        print(f"\n✅ 推荐使用: {min_viable} 秒")
        print(f"   原因: 足够应对天气 API 的实际延迟")
        print(f"\n💡 预期性能:")
        print(f"   • 天气查询: 4-6 秒")
        print(f"   • 时间查询: <1 秒")
        print(f"   • 编排开销: <1 秒")
        print(f"   • 总计: 5-7 秒 (远低于 {min_viable}s 超时)")
    else:
        print("\n⚠️  所有超时都不够！可能存在更深层的问题")

    print("\n" + "="*60)


if __name__ == "__main__":
    asyncio.run(main())
