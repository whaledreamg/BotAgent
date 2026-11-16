"""LLM-First 参数提取集成测试

测试完整的工作流程：缓存 → LLM → 正则
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from parameter_extraction_manager import ParameterExtractionManager
from llm_parameter_extractor import SkillSpec, ParameterSpec, LLMParameterExtractor
from parameter_cache import ParameterCache
from thinking_logger import ThinkingLogger
import tempfile
import os


@pytest.fixture
def temp_log_dir():
    """创建临时日志目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def create_weather_skill():
    """创建天气 Skill"""
    return SkillSpec(
        name="weather",
        label="查询天气",
        description="查询指定城市的天气",
        category="query",
        parameters=[
            ParameterSpec(
                name="city",
                description="城市名",
                param_type="string",
                required=True,
                examples=["北京", "上海"],
            ),
        ],
    )


@pytest.mark.asyncio
async def test_end_to_end_llm_first_flow(temp_log_dir):
    """测试完整的 LLM-First 工作流"""
    # 初始化组件
    llm_mock = AsyncMock()
    registry_mock = MagicMock()
    cache = ParameterCache()
    thinking_logger = ThinkingLogger(log_dir=temp_log_dir)

    # 配置 Mock LLM 返回值
    llm_mock.json_completion.return_value = {
        "extracted_params": {"city": "北京"},
        "confidence": 0.98,
        "reasoning": "用户明确提到北京",
    }

    # 配置 Registry 正则提取
    registry_mock.extract.return_value = "北京"
    registry_mock.is_valid_result.return_value = True

    # 创建 LLM 提取器
    llm_extractor = LLMParameterExtractor(llm_mock)

    # 创建参数提取管理器
    manager = ParameterExtractionManager(
        llm=llm_mock,
        registry=registry_mock,
        cache=cache,
        llm_extractor=llm_extractor,
        thinking_logger=thinking_logger,
    )

    # 配置缓存 TTL
    cache.set_skill_ttl("weather", 3600)

    skill = create_weather_skill()

    # 第一次调用：缓存未命中，触发 LLM 提取
    print("\n=== 第一次调用（缓存未命中）===")
    params1, conf1, reason1 = await manager.llm_first_extract("查北京天气", skill)

    assert params1 == {"city": "北京"}
    assert conf1 == 0.98
    assert manager.stats["llm_calls"] == 1
    assert manager.stats["cache_hits"] == 0

    # 验证缓存已存储
    assert len(cache) == 1
    print(f"✓ LLM 调用成功，参数: {params1}")
    print(f"✓ 缓存大小: {len(cache)}")

    # 第二次调用：相同查询，缓存命中
    print("\n=== 第二次调用（缓存命中）===")
    params2, conf2, reason2 = await manager.llm_first_extract("查北京天气", skill)

    assert params2 == {"city": "北京"}
    assert conf2 == 0.98
    assert reason2 == "缓存命中"
    assert manager.stats["cache_hits"] == 1
    assert manager.stats["llm_calls"] == 1  # 不增加

    print(f"✓ 缓存命中")
    print(f"✓ 总缓存命中数: {manager.stats['cache_hits']}")

    # 验证统计信息
    print("\n=== 统计信息 ===")
    stats = manager.get_stats()
    print(f"缓存统计: {stats['cache']}")
    print(f"提取统计: {stats['extraction']}")

    # 验证思考日志已记录
    print("\n=== 验证日志文件 ===")
    log_files = list(__import__("pathlib").Path(temp_log_dir).rglob("*.jsonl"))
    assert len(log_files) > 0, "应该有日志文件被创建"
    print(f"✓ 创建了 {len(log_files)} 个日志文件")

    # 读取日志文件验证内容
    if log_files:
        with open(log_files[0], "r", encoding="utf-8") as f:
            import json
            for line in f:
                log_entry = json.loads(line)
                assert log_entry["skill"] == "weather"
                assert log_entry["user_query"] == "查北京天气"
                print(f"✓ 日志记录: {log_entry['id']}")


@pytest.mark.asyncio
async def test_cache_expiration_flow(temp_log_dir):
    """测试缓存过期流程"""
    cache = ParameterCache(default_ttl=1)  # 1 秒过期

    llm_extractor = AsyncMock()
    llm_extractor.extract.return_value = (
        {"city": "北京"},
        0.98,
        "LLM 提取成功",
    )

    registry_mock = MagicMock()
    registry_mock.extract.return_value = "北京"

    manager = ParameterExtractionManager(
        registry=registry_mock,
        cache=cache,
        llm_extractor=llm_extractor,
    )

    skill = create_weather_skill()

    # 第一次调用
    print("\n=== 第一次调用 ===")
    await manager.llm_first_extract("查北京天气", skill)
    assert manager.stats["llm_calls"] == 1
    print("✓ LLM 调用完成")

    # 立即第二次调用（应该缓存命中）
    print("=== 第二次调用（缓存命中）===")
    await manager.llm_first_extract("查北京天气", skill)
    assert manager.stats["cache_hits"] == 1
    print("✓ 缓存命中")

    # 等待缓存过期
    print("=== 等待缓存过期 ===")
    await asyncio.sleep(1.1)

    # 第三次调用（缓存已过期，应触发新的 LLM 调用）
    print("=== 第三次调用（缓存已过期）===")
    await manager.llm_first_extract("查北京天气", skill)
    assert manager.stats["llm_calls"] == 2
    print("✓ 缓存已过期，触发新的 LLM 调用")


@pytest.mark.asyncio
async def test_thinking_logger_integration(temp_log_dir):
    """测试思考日志集成"""
    logger = ThinkingLogger(log_dir=temp_log_dir)

    # 记录一条日志
    trace_id = logger.log_extraction(
        skill="weather",
        user_query="查北京天气",
        prompt="从用户查询中提取参数...",
        llm_response={
            "model": "gpt-4o-mini",
            "prompt_tokens": 156,
            "completion_tokens": 42,
        },
        extracted_params={"city": "北京"},
        confidence=0.98,
        reasoning="用户明确提到北京",
        total_ms=92.0,
        timing_breakdown={
            "prompt_building": 1.2,
            "llm_call": 87.4,
            "response_parsing": 2.1,
            "validation": 0.8,
        },
    )

    print(f"\n=== 日志记录成功 ===")
    print(f"Trace ID: {trace_id}")

    # 验证日志文件存在
    from pathlib import Path
    log_files = list(Path(temp_log_dir).rglob("*.jsonl"))
    assert len(log_files) > 0, "应该创建日志文件"
    print(f"✓ 创建了日志文件: {log_files[0]}")

    # 验证日志内容
    import json
    with open(log_files[0], "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            assert entry["id"] == trace_id
            assert entry["skill"] == "weather"
            assert entry["results"]["extracted_params"]["city"] == "北京"
            assert entry["results"]["confidence"] == 0.98
            print(f"✓ 日志内容验证通过")


if __name__ == "__main__":
    # 运行测试
    print("启动 LLM-First 集成测试...\n")
    pytest.main([__file__, "-v", "-s"])
