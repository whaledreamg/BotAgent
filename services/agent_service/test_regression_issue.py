"""针对生产环境问题的回归测试

测试用例来自实际的用户查询"先查南京天气然后讲个笑话"
"""

import asyncio
import logging
from parameter_extractors import CityExtractor
from parameter_extraction_manager import ParameterExtractionManager

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)


class MockLLM:
    """模拟 LLM"""
    async def complete(self, prompt: str, max_tokens: int = 20, temperature: float = 0.0) -> str:
        await asyncio.sleep(0.05)
        return ""


async def test_regression():
    """回归测试：针对实际生产问题"""
    
    logger.info("=" * 80)
    logger.info("回归测试: 先查南京天气然后讲个笑话")
    logger.info("=" * 80)
    
    # 问题查询
    problem_query = "先查南京天气然后讲个笑话"
    
    # 1. 测试正则提取
    logger.info("\n1. 测试正则提取器")
    logger.info("-" * 80)
    
    extractor = CityExtractor()
    regex_result = extractor.extract(problem_query)
    
    logger.info(f"输入: {problem_query}")
    logger.info(f"结果: {regex_result}")
    
    if regex_result == "南京":
        logger.info("✓ 正则提取正确")
    else:
        logger.info(f"✗ 正则提取错误: 期望 '南京', 得到 '{regex_result}'")
    
    # 2. 测试混合提取
    logger.info("\n2. 测试混合提取管理器")
    logger.info("-" * 80)
    
    llm = MockLLM()
    manager = ParameterExtractionManager(llm=llm)
    
    # 正则优先路径
    hybrid_result_no_llm = manager.extract_without_llm("city", problem_query)
    logger.info(f"输入: {problem_query}")
    logger.info(f"正则路径结果: {hybrid_result_no_llm}")
    
    if hybrid_result_no_llm == "南京":
        logger.info("✓ 混合提取 (正则路径) 正确")
    else:
        logger.info(f"✗ 混合提取 (正则路径) 错误: 期望 '南京', 得到 '{hybrid_result_no_llm}'")
    
    # 混合路径
    hybrid_result = await manager.hybrid_extract("city", problem_query, fallback_to_llm=True)
    logger.info(f"混合路径结果 (正则+LLM降级): {hybrid_result}")
    
    if hybrid_result == "南京":
        logger.info("✓ 混合提取 (完整路径) 正确")
    else:
        logger.info(f"✗ 混合提取 (完整路径) 错误: 期望 '南京', 得到 '{hybrid_result}'")
    
    # 3. 额外的相关测试
    logger.info("\n3. 相似场景测试")
    logger.info("-" * 80)
    
    test_cases = [
        "先查北京天气然后讲个笑话",
        "查上海天气接着播放音乐",
        "看杭州天气之后找个餐厅",
        "查广州天气完了再查时间",
        "南京天气怎么样然后讲个笑话",
    ]
    
    for query in test_cases:
        result = extractor.extract(query)
        logger.info(f"  {query:25} → {result or '(空)'}")
    
    logger.info("\n" + "=" * 80)
    logger.info("✅ 回归测试完成")
    logger.info("=" * 80)


if __name__ == "__main__":
    asyncio.run(test_regression())
