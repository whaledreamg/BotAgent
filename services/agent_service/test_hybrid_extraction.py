"""混合参数提取框架的测试。

测试 11 个用例，对比正则和混合提取的性能。
"""

import asyncio
import logging
import time
from typing import Dict, List, Tuple

from parameter_extractors import get_global_registry
from parameter_extraction_manager import ParameterExtractionManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# 测试用例：(输入文本, 参数类型, 期望结果, 场景描述)
TEST_CASES: List[Tuple[str, str, str, str]] = [
    # 城市提取 - 简单场景
    ("查北京天气", "city", "北京", "简单城市查询"),
    ("告诉我上海的天气", "city", "上海", "包含介词"),
    ("请看看杭州今天的天气", "city", "杭州", "包含敬词"),

    # 城市提取 - 复杂场景
    ("先查南京天气再查时间", "city", "南京", "复合意图"),
    ("查一下北京和上海的天气", "city", None, "多城市（无法处理）"),
    ("南京天气怎么样", "city", "南京", "疑问句"),

    # 物体提取
    ("找一下手机在哪", "object", "手机", "简单物体查询"),
    ("看看桌上有没有钥匙", "object", None, "复杂嵌套（无法处理）"),
    ("找一个苹果", "object", "苹果", "包含数量词"),

    # 物体提取 - 无效情况
    ("帮我", "object", None, "只有动词"),
    ("怎么样", "object", None, "只有疑问词"),
]


class MockLLM:
    """模拟 LLM 用于测试。"""

    async def complete(
        self,
        prompt: str,
        max_tokens: int = 20,
        temperature: float = 0.0,
    ) -> str:
        """模拟 LLM 返回。"""
        # 模拟延迟
        await asyncio.sleep(0.1)
        # 简单的反射：从提示词中提取预期结果
        if "城市名" in prompt:
            if "句子: \"查北京天气\"" in prompt:
                return "北京"
            elif "句子: \"先查南京天气再查时间\"" in prompt:
                return "南京"
        return ""


async def test_single_case(
    manager: ParameterExtractionManager,
    text: str,
    param_type: str,
    expected: str,
    description: str,
) -> Dict:
    """测试单个用例。"""
    start = time.time()

    # 正则提取
    regex_result = manager.extract_without_llm(param_type, text)
    regex_elapsed = (time.time() - start) * 1000

    # 混合提取
    start = time.time()
    hybrid_result = await manager.hybrid_extract(param_type, text, fallback_to_llm=True)
    hybrid_elapsed = (time.time() - start) * 1000

    # 判断结果
    regex_correct = regex_result == expected
    hybrid_correct = hybrid_result == expected

    return {
        "text": text,
        "param_type": param_type,
        "description": description,
        "expected": expected,
        "regex_result": regex_result,
        "regex_elapsed_ms": regex_elapsed,
        "regex_correct": regex_correct,
        "hybrid_result": hybrid_result,
        "hybrid_elapsed_ms": hybrid_elapsed,
        "hybrid_correct": hybrid_correct,
    }


async def run_all_tests():
    """运行所有测试。"""
    logger.info("=" * 80)
    logger.info("混合参数提取框架测试")
    logger.info("=" * 80)

    # 创建管理器
    registry = get_global_registry()
    mock_llm = MockLLM()
    manager = ParameterExtractionManager(llm=mock_llm, registry=registry)

    results: List[Dict] = []

    # 运行所有测试
    for text, param_type, expected, description in TEST_CASES:
        result = await test_single_case(manager, text, param_type, expected, description)
        results.append(result)

    # 输出结果
    logger.info("\n测试结果汇总：")
    logger.info("-" * 80)

    regex_correct_count = 0
    hybrid_correct_count = 0
    total_time_regex = 0.0
    total_time_hybrid = 0.0

    for result in results:
        status_regex = "✓" if result["regex_correct"] else "✗"
        status_hybrid = "✓" if result["hybrid_correct"] else "✗"

        logger.info(
            f"{status_regex} {status_hybrid} | {result['description']:15} | "
            f"{result['param_type']:8} | 正则: {result['regex_result'] or '(空)':8} "
            f"({result['regex_elapsed_ms']:.2f}ms) | "
            f"混合: {result['hybrid_result'] or '(空)':8} "
            f"({result['hybrid_elapsed_ms']:.2f}ms)"
        )

        if result["regex_correct"]:
            regex_correct_count += 1
        if result["hybrid_correct"]:
            hybrid_correct_count += 1

        total_time_regex += result["regex_elapsed_ms"]
        total_time_hybrid += result["hybrid_elapsed_ms"]

    logger.info("-" * 80)
    logger.info(f"正则提取准确度: {regex_correct_count}/{len(TEST_CASES)} ({100*regex_correct_count//len(TEST_CASES)}%)")
    logger.info(f"混合提取准确度: {hybrid_correct_count}/{len(TEST_CASES)} ({100*hybrid_correct_count//len(TEST_CASES)}%)")
    logger.info(f"正则提取总耗时: {total_time_regex:.2f}ms (平均 {total_time_regex/len(TEST_CASES):.2f}ms)")
    logger.info(f"混合提取总耗时: {total_time_hybrid:.2f}ms (平均 {total_time_hybrid/len(TEST_CASES):.2f}ms)")
    logger.info("=" * 80)

    # 返回测试结果
    return {
        "total": len(TEST_CASES),
        "regex_correct": regex_correct_count,
        "hybrid_correct": hybrid_correct_count,
        "regex_accuracy": f"{100*regex_correct_count//len(TEST_CASES)}%",
        "hybrid_accuracy": f"{100*hybrid_correct_count//len(TEST_CASES)}%",
        "details": results,
    }


if __name__ == "__main__":
    test_result = asyncio.run(run_all_tests())

    # 最终总结
    logger.info("\n测试总结：")
    logger.info(f"总用例数: {test_result['total']}")
    logger.info(f"正则准确度: {test_result['regex_accuracy']}")
    logger.info(f"混合准确度: {test_result['hybrid_accuracy']}")
