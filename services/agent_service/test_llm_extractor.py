"""LLM 参数提取器单元测试"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from llm_parameter_extractor import (
    ParameterSpec,
    SkillSpec,
    LLMParameterExtractor,
)


@pytest.fixture
def mock_llm():
    """创建 Mock LLM"""
    llm = AsyncMock()
    return llm


@pytest.fixture
def weather_skill():
    """创建天气 Skill"""
    params = [
        ParameterSpec(
            name="city",
            description="城市名称",
            param_type="string",
            required=True,
            examples=["北京", "上海", "南京"],
        ),
        ParameterSpec(
            name="date",
            description="查询日期",
            param_type="string",
            required=False,
            default="今天",
        ),
    ]
    return SkillSpec(
        name="weather",
        label="查询天气",
        description="查询指定城市的天气情况",
        category="query",
        parameters=params,
    )


@pytest.mark.asyncio
async def test_extract_city_weather(mock_llm, weather_skill):
    """测试提取城市天气"""
    # 设置 Mock 返回值
    mock_llm.json_completion.return_value = {
        "extracted_params": {"city": "北京"},
        "confidence": 0.98,
        "reasoning": "用户明确说了查北京天气",
    }

    extractor = LLMParameterExtractor(mock_llm)
    params, confidence, reasoning = await extractor.extract(
        "查北京今天的天气",
        weather_skill,
    )

    assert params == {"city": "北京"}
    assert confidence == 0.98
    assert "北京" in reasoning


@pytest.mark.asyncio
async def test_extract_with_optional_params(mock_llm, weather_skill):
    """测试提取包含可选参数"""
    mock_llm.json_completion.return_value = {
        "extracted_params": {"city": "上海", "date": "明天"},
        "confidence": 0.95,
        "reasoning": "用户问明天上海天气",
    }

    extractor = LLMParameterExtractor(mock_llm)
    params, confidence, reasoning = await extractor.extract(
        "明天上海的天气怎么样",
        weather_skill,
    )

    assert params == {"city": "上海", "date": "明天"}
    assert confidence == 0.95


@pytest.mark.asyncio
async def test_extract_failure(mock_llm, weather_skill):
    """测试提取失败"""
    mock_llm.json_completion.side_effect = Exception("LLM 调用失败")

    extractor = LLMParameterExtractor(mock_llm)
    params, confidence, reasoning = await extractor.extract(
        "这是一个随机的查询",
        weather_skill,
    )

    assert params is None
    assert confidence == 0.0
    assert "失败" in reasoning


@pytest.mark.asyncio
async def test_skill_without_parameters():
    """测试没有参数的 Skill"""
    mock_llm = AsyncMock()
    mock_llm.json_completion.return_value = {
        "extracted_params": {},
        "confidence": 1.0,
        "reasoning": "该技能不需要参数",
    }

    skill = SkillSpec(
        name="joke",
        label="讲笑话",
        description="给用户讲一个笑话",
        category="query",
        parameters=[],
    )

    extractor = LLMParameterExtractor(mock_llm)
    params, confidence, reasoning = await extractor.extract(
        "讲个笑话",
        skill,
    )

    assert params == {}
    assert confidence == 1.0


def test_parameter_spec_to_prompt():
    """测试参数规格生成 Prompt"""
    param = ParameterSpec(
        name="city",
        description="城市名称",
        param_type="string",
        required=True,
        examples=["北京", "上海"],
        notes="中文城市名",
    )

    prompt_desc = param.to_prompt_description()
    assert "city" in prompt_desc
    assert "必填" in prompt_desc
    assert "北京" in prompt_desc
    assert "中文城市名" in prompt_desc
