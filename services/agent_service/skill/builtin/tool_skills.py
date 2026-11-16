"""内置 Tool Skill 集合。

定义所有内置的 Tool 类 Skill（查天气、查时间、找物体等）。
"""

from __future__ import annotations

from skill.spec import SkillSpec

__all__ = [
    "weather_spec",
    "time_spec",
    "date_spec",
    "find_object_spec",
]

# 查天气 Skill
weather_spec = SkillSpec(
    id="weather",
    name="天气查询",
    description="查询指定城市的天气信息，包括气温、风向等",
    branch="tool",
    category="query",
    kind="query",
    speak=True,
    timeout_ms=5000,
    source="builtin",
    natural_language_examples=[
        "天气怎么样",
        "今天天气",
        "南京天气",
        "查一下天气",
        "气温多少",
        "现在温度",
        "会下雨吗",
    ],
    definition={
        "boundary": "查询当前天气，不支持未来天气预报",
        "detector_class": "WeatherDetector",
        "post_processors": ["weather_formatter"],
        "documentation": """
# weather - 天气查询

## 职责边界
- ✓ 查询当前城市天气
- ✓ 查询指定城市天气
- ✓ 返回温度、风向、降水等信息
- ✗ 不支持天气预报
- ✗ 不支持历史天气

## 输入参数
- city (可选): 城市名，如「南京」「北京」

## 输出格式
```json
{
    "city": "南京",
    "temperature": 15,
    "weather": "多云",
    "wind": "东风 3 级",
    "humidity": 65
}
```

## 使用示例
- "南京天气怎么样" → 查询南京当前天气
- "气温多少" → 查询当前城市温度
- "会下雨吗" → 查询是否有降水
"""
    },
    parameters={
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "城市名（可选，不指定则查询默认城市）"
            }
        },
    },
    returns={
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "temperature": {"type": "number"},
            "weather": {"type": "string"},
            "wind": {"type": "string"},
            "humidity": {"type": "number"},
        },
    },
)

# 查时间 Skill
time_spec = SkillSpec(
    id="time",
    name="时间查询",
    description="查询当前时间",
    branch="tool",
    category="query",
    kind="query",
    speak=True,
    timeout_ms=1000,
    source="builtin",
    natural_language_examples=[
        "现在几点",
        "几点了",
        "什么时间",
        "现在时间",
        "报时",
    ],
    definition={
        "boundary": "仅返回当前时间，不支持时区转换",
        "detector_class": "TimeDetector",
        "post_processors": ["time_formatter"],
        "documentation": """
# time - 时间查询

## 职责边界
- ✓ 返回当前时间（时:分:秒）
- ✓ 支持口语化表述
- ✗ 不支持时区转换
- ✗ 不支持倒计时

## 输出格式
```json
{
    "hour": 14,
    "minute": 30,
    "second": 45,
    "formatted": "下午2点30分"
}
```

## 使用示例
- "现在几点" → 返回当前时间
- "什么时间" → 返回当前时间
"""
    },
    parameters={
        "type": "object",
        "properties": {},
    },
    returns={
        "type": "object",
        "properties": {
            "hour": {"type": "integer"},
            "minute": {"type": "integer"},
            "second": {"type": "integer"},
            "formatted": {"type": "string"},
        },
    },
)

# 查日期 Skill
date_spec = SkillSpec(
    id="date",
    name="日期查询",
    description="查询当前日期和星期几",
    branch="tool",
    category="query",
    kind="query",
    speak=True,
    timeout_ms=1000,
    source="builtin",
    natural_language_examples=[
        "今天几号",
        "今天什么日期",
        "今天星期几",
        "现在日期",
    ],
    definition={
        "boundary": "仅返回当前日期，不支持农历或其他历法",
        "detector_class": "DateDetector",
        "post_processors": ["date_formatter"],
        "documentation": """
# date - 日期查询

## 职责边界
- ✓ 返回当前日期
- ✓ 返回星期几
- ✗ 不支持农历
- ✗ 不支持其他历法

## 输出格式
```json
{
    "year": 2026,
    "month": 11,
    "day": 14,
    "weekday": "星期六",
    "formatted": "2026年11月14日 星期六"
}
```

## 使用示例
- "今天几号" → 返回日期和星期
- "现在什么日期" → 返回完整日期
"""
    },
    parameters={
        "type": "object",
        "properties": {},
    },
    returns={
        "type": "object",
        "properties": {
            "year": {"type": "integer"},
            "month": {"type": "integer"},
            "day": {"type": "integer"},
            "weekday": {"type": "string"},
            "formatted": {"type": "string"},
        },
    },
)

# 找物体 Skill
find_object_spec = SkillSpec(
    id="find_object",
    name="物体查找",
    description="识别摄像头看到的物体并判断其位置",
    branch="tool",
    category="vision",
    kind="query",
    speak=True,
    timeout_ms=15000,
    source="builtin",
    natural_language_examples=[
        "看看有没有杯子",
        "物体在哪",
        "有几个瓶子",
        "找一下人",
        "看到了什么",
    ],
    definition={
        "boundary": "仅查询摄像头能看到的物体，不支持隐藏物体或被遮挡的物体",
        "detector_class": "VisionDetector",
        "post_processors": ["object_formatter"],
        "documentation": """
# find_object - 物体查找

## 职责边界
- ✓ 识别摄像头看到的物体
- ✓ 返回物体位置和数量
- ✓ 支持是/否判断
- ✗ 不支持看不到的物体
- ✗ 不支持完全被遮挡的物体

## 输入参数
- target: 要找的物体名，如「杯子」

## 输出格式
```json
{
    "found": true,
    "target": "杯子",
    "count": 1,
    "position": "左前方",
    "confidence": 0.95
}
```

## 使用示例
- "有没有杯子" → 是/否判断
- "有几个杯子" → 返回数量
- "杯子在哪" → 返回位置
"""
    },
    parameters={
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "要找的物体名"
            }
        },
        "required": ["target"],
    },
    returns={
        "type": "object",
        "properties": {
            "found": {"type": "boolean"},
            "target": {"type": "string"},
            "count": {"type": "integer"},
            "position": {"type": "string"},
            "confidence": {"type": "number"},
        },
    },
)
