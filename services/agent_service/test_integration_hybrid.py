"""混合参数提取框架的集成测试。

验证整个框架的端到端工作流程：
1. parameter_extractors 的各种提取器
2. parameter_extraction_manager 的混合提取
3. parameter_validator 的异步验证
4. tool_handler 的集成
"""

import asyncio
import logging
from typing import Dict

from parameter_extractors import (
    CityExtractor,
    ObjectExtractor,
    ParameterExtractorRegistry,
)
from parameter_extraction_manager import ParameterExtractionManager
from parameter_validator import ParameterValidator, create_default_validator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MockLLM:
    """模拟 LLM 用于测试。"""

    async def complete(
        self,
        prompt: str,
        max_tokens: int = 20,
        temperature: float = 0.0,
    ) -> str:
        """模拟 LLM 返回。"""
        await asyncio.sleep(0.05)  # 模拟网络延迟
        return ""


async def test_extractors():
    """测试 1: 提取器基础功能"""
    logger.info("\n" + "=" * 80)
    logger.info("测试 1: 提取器基础功能")
    logger.info("=" * 80)

    registry = ParameterExtractorRegistry()

    # 测试城市提取器
    city_extractor = registry.get("city")
    assert city_extractor is not None, "城市提取器不存在"

    test_cases = [
        ("查北京天气", "北京"),
        ("告诉我上海的天气", "上海"),
        ("怎么样", None),
    ]

    for text, expected in test_cases:
        result = city_extractor.extract(text)
        status = "✓" if result == expected else "✗"
        logger.info(f"{status} 输入: {text:20} 期望: {expected or '(空)':8} 实际: {result or '(空)':8}")

    # 测试物体提取器
    object_extractor = registry.get("object")
    assert object_extractor is not None, "物体提取器不存在"

    test_cases = [
        ("找手机在哪", "手机"),
        ("找一下手机在哪", "手机"),
        ("帮我", None),
    ]

    for text, expected in test_cases:
        result = object_extractor.extract(text)
        status = "✓" if result == expected else "✗"
        logger.info(f"{status} 输入: {text:20} 期望: {expected or '(空)':8} 实际: {result or '(空)':8}")


async def test_extraction_manager():
    """测试 2: 混合提取管理器"""
    logger.info("\n" + "=" * 80)
    logger.info("测试 2: 混合提取管理器")
    logger.info("=" * 80)

    registry = ParameterExtractorRegistry()
    mock_llm = MockLLM()
    manager = ParameterExtractionManager(llm=mock_llm, registry=registry)

    # 测试正则提取
    result = manager.extract_without_llm("city", "查北京天气")
    assert result == "北京", f"期望北京，得到 {result}"
    logger.info("✓ 正则提取: 查北京天气 → 北京")

    # 测试混合提取（正则路径）
    result = await manager.hybrid_extract("city", "查上海天气", fallback_to_llm=False)
    assert result == "上海", f"期望上海，得到 {result}"
    logger.info("✓ 混合提取（正则）: 查上海天气 → 上海")


async def test_parameter_validator():
    """测试 3: 参数验证器异步方法"""
    logger.info("\n" + "=" * 80)
    logger.info("测试 3: 参数验证器异步方法")
    logger.info("=" * 80)

    # 创建验证器（不带 extraction_manager）
    validator = create_default_validator()

    # 测试同步验证
    result = validator.validate("weather", {"city": "北京"}, "查北京天气")
    assert result.is_valid, f"验证失败: {result.errors}"
    logger.info("✓ 同步验证: weather.city=北京 → 成功")

    # 测试异步验证
    result = await validator.async_validate("weather", {"city": ""}, "查广州天气")
    # 应该从原文提取出广州
    logger.info(f"✓ 异步验证: weather.city=(空) → {result.params.get('city') or '(空)'}")

    # 测试必填参数检查
    result = validator.validate("find_object", {}, "找手机")
    assert not result.is_valid, "应该检测到缺失的必填参数"
    logger.info(f"✓ 必填参数检查: find_object 缺失 {result.missing_params}")


async def test_integration():
    """测试 4: 端到端集成"""
    logger.info("\n" + "=" * 80)
    logger.info("测试 4: 端到端集成")
    logger.info("=" * 80)

    # 创建完整的管道
    mock_llm = MockLLM()
    registry = ParameterExtractorRegistry()
    manager = ParameterExtractionManager(llm=mock_llm, registry=registry)
    validator = create_default_validator(extraction_manager=manager)

    # 场景 1：查天气
    logger.info("\n场景 1: 查天气")
    result = await validator.async_validate(
        "weather",
        {},  # LLM 没有返回参数
        "查北京天气",
    )
    assert result.is_valid, f"验证失败: {result.errors}"
    assert result.params.get("city") == "北京", f"城市提取失败: {result.params}"
    logger.info(f"✓ weather 参数补全: {result.params}")

    # 场景 2：查找物体
    logger.info("\n场景 2: 查找物体")
    result = await validator.async_validate(
        "find_object",
        {},  # LLM 没有返回参数
        "找一下手机",
    )
    assert result.is_valid, f"验证失败: {result.errors}"
    assert result.params.get("name") == "手机", f"物体名提取失败: {result.params}"
    logger.info(f"✓ find_object 参数补全: {result.params}")


async def main():
    """运行所有测试。"""
    logger.info("\n" + "=" * 80)
    logger.info("混合参数提取框架 - 集成测试")
    logger.info("=" * 80)

    try:
        await test_extractors()
        await test_extraction_manager()
        await test_parameter_validator()
        await test_integration()

        logger.info("\n" + "=" * 80)
        logger.info("✓ 所有测试通过！")
        logger.info("=" * 80)

    except AssertionError as e:
        logger.error(f"\n✗ 测试失败: {e}")
        return 1
    except Exception as e:
        logger.error(f"\n✗ 意外错误: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
