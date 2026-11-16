"""LLM vs 正则提取对比测试"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from parameter_extraction_manager import ParameterExtractionManager
from llm_parameter_extractor import SkillSpec, ParameterSpec
from parameter_cache import ParameterCache


@pytest.fixture
def llm_mock():
    """创建 Mock LLM"""
    return AsyncMock()


@pytest.fixture
def registry_mock():
    """创建 Mock 提取器注册表"""
    registry = MagicMock()

    def extract_side_effect(param_type, text):
        if param_type == "city":
            if "北京" in text:
                return "北京"
            elif "上海" in text:
                return "上海"
            elif "南京" in text:
                return "南京"
        elif param_type == "object":
            if "手机" in text:
                return "手机"
        return None

    registry.extract.side_effect = extract_side_effect
    registry.is_valid_result.return_value = True
    return registry


@pytest.fixture
def weather_skill():
    """创建天气 Skill"""
    params = [
        ParameterSpec(
            name="city",
            description="城市名称",
            param_type="string",
            required=True,
            examples=["北京", "上海"],
        ),
    ]
    return SkillSpec(
        name="weather",
        label="查询天气",
        description="查询指定城市的天气",
        category="query",
        parameters=params,
    )


@pytest.mark.asyncio
async def test_llm_first_cache_hit(llm_mock, registry_mock, weather_skill):
    """测试缓存命中"""
    cache = ParameterCache()
    llm_extractor = AsyncMock()

    manager = ParameterExtractionManager(
        llm=llm_mock,
        registry=registry_mock,
        cache=cache,
        llm_extractor=llm_extractor,
    )

    # 预先存入缓存
    await cache.set(
        "weather",
        "查北京天气",
        {
            "extracted_params": {"city": "北京"},
            "confidence": 0.95,
            "reasoning": "缓存结果",
        },
    )

    # 第一次调用（缓存命中）
    params, confidence, reasoning = await manager.llm_first_extract(
        "查北京天气",
        weather_skill,
    )

    assert params == {"city": "北京"}
    assert confidence == 0.95
    assert reasoning == "缓存命中"
    assert manager.stats["cache_hits"] == 1
    assert llm_extractor.extract.call_count == 0  # LLM 不应被调用


@pytest.mark.asyncio
async def test_llm_first_llm_call(llm_mock, registry_mock, weather_skill):
    """测试 LLM 调用"""
    cache = ParameterCache()

    llm_extractor = AsyncMock()
    llm_extractor.extract.return_value = (
        {"city": "北京"},
        0.98,
        "LLM 提取成功",
    )

    manager = ParameterExtractionManager(
        llm=llm_mock,
        registry=registry_mock,
        cache=cache,
        llm_extractor=llm_extractor,
    )

    # 第一次调用（缓存未命中，LLM 提取）
    params, confidence, reasoning = await manager.llm_first_extract(
        "查北京天气",
        weather_skill,
    )

    assert params == {"city": "北京"}
    assert confidence == 0.98
    assert manager.stats["llm_calls"] == 1
    assert llm_extractor.extract.call_count == 1

    # 第二次调用相同查询（缓存命中）
    params2, confidence2, reasoning2 = await manager.llm_first_extract(
        "查北京天气",
        weather_skill,
    )

    assert params2 == {"city": "北京"}
    assert manager.stats["cache_hits"] == 1
    assert llm_extractor.extract.call_count == 1  # 不增加


@pytest.mark.asyncio
async def test_llm_vs_regex_comparison(llm_mock, registry_mock, weather_skill):
    """测试 LLM vs 正则对比"""
    cache = ParameterCache()

    # 模拟 LLM 结果与正则不同
    llm_extractor = AsyncMock()
    llm_extractor.extract.return_value = (
        {"city": "北京"},
        0.98,
        "LLM 提取成功",
    )

    manager = ParameterExtractionManager(
        llm=llm_mock,
        registry=registry_mock,
        cache=cache,
        llm_extractor=llm_extractor,
    )

    # 调用 LLM-First 提取
    params, confidence, reasoning = await manager.llm_first_extract(
        "查北京天气",
        weather_skill,
    )

    assert params == {"city": "北京"}
    # LLM 和正则结果都是 "北京"，应该匹配
    assert manager.stats["llm_vs_regex_matches"] == 1


@pytest.mark.asyncio
async def test_cache_statistics(llm_mock, registry_mock):
    """测试缓存统计"""
    cache = ParameterCache()

    # 配置 Skill 级别的 TTL
    cache.set_skill_ttl("weather", 3600)

    manager = ParameterExtractionManager(
        llm=llm_mock,
        registry=registry_mock,
        cache=cache,
    )

    # 存入缓存
    await cache.set(
        "weather",
        "查北京天气",
        {
            "extracted_params": {"city": "北京"},
            "confidence": 0.95,
        },
    )

    stats = manager.get_stats()
    assert stats["cache"]["size"] == 1
    assert stats["cache"]["max_size"] == 10000
    assert stats["extraction"]["cache_hits"] == 0


@pytest.mark.asyncio
async def test_cache_expiration():
    """测试缓存过期"""
    cache = ParameterCache(default_ttl=1)  # 1 秒过期

    await cache.set(
        "weather",
        "查北京天气",
        {
            "extracted_params": {"city": "北京"},
            "confidence": 0.95,
        },
    )

    # 立即查询（应命中）
    result = await cache.get("weather", "查北京天气")
    assert result is not None

    # 等待过期后查询（应未命中）
    import asyncio
    await asyncio.sleep(1.1)

    result = await cache.get("weather", "查北京天气")
    assert result is None
