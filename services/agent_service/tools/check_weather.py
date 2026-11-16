#!/usr/bin/env python3
"""天气工具接入自检。

覆盖:
1. 城市名提取 (规则路径): 从各种说法里挑出城市
2. 工具匹配
3. 真实调用 Open-Meteo (--live), 含「没说城市」的分支

用法::

    python tools/check_weather.py          # 离线: 城市提取 + 匹配
    python tools/check_weather.py --live   # 额外真实调 API
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers.tool_handler import ToolHandler  # noqa: E402
from weather_client import (  # noqa: E402
    CityNotFound,
    CityNotSpecified,
    WeatherClient,
    WeatherError,
)

PASSED = 0
FAILED = 0


def check(desc: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {desc}")
    else:
        FAILED += 1
        print(f"  [FAIL] {desc}  {detail}")


# ------------------------------------------------------------ 城市提取

CITY_CASES = [
    ("北京天气怎么样", "北京"),
    ("查一下上海天气", "上海"),
    ("帮我查一下深圳的天气", "深圳"),
    ("查询广州天气", "广州"),
    ("杭州今天天气如何", "杭州"),
    ("成都的天气", "成都"),
    # 没提城市 -> 空串 (代表「没说城市」)
    ("今天天气怎么样", ""),
    ("查天气", ""),
    ("天气", ""),
    ("现在气温多少", ""),
    ("会下雨吗", ""),
]


def test_city_extract() -> None:
    print("\n[1] 城市名提取")
    handler = ToolHandler()
    for text, expect in CITY_CASES:
        got = handler._extract_weather_city(text)
        check(f"{text!r} -> {expect!r}", got == expect, f"实际 {got!r}")


# ------------------------------------------------------------ 匹配

MATCH_CASES = [
    ("今天天气怎么样", "weather"),
    ("北京天气怎么样", "weather"),
    ("查一下上海天气", "weather"),
    ("现在气温多少", "weather"),
    ("今天几号", "date"),
    ("现在几点", "time"),
    # 句子里夹了修饰词 -> 提取出的候选城市查不到, 由 API 侧回落
    ("今天天气如何适合出门", "weather"),
]


def test_match() -> None:
    print("\n[2] 工具匹配")
    handler = ToolHandler()
    for text, expect in MATCH_CASES:
        name, _, args = handler._match(text)
        ok = name == expect
        detail = f"实际 {name!r} args={args}"
        check(f"{text!r} -> {expect!r}", ok, detail)


# ------------------------------------------------------------ live

async def _live(city: str) -> None:
    client = WeatherClient(timeout=10.0)
    try:
        report = await client.query(city)
        print(
            f"  天气: {report.city} {report.condition} "
            f"{report.temperature}°C 体感{report.apparent}°C "
            f"湿度{report.humidity}% 风{report.wind}km/h "
            f"今日{report.temp_min}~{report.temp_max}°C "
            f"降水概率{report.precip_prob}%"
        )
        check(f"查「{city}」有结果", bool(report.city))
        check("天气现象不是未知", report.condition != "未知", report.condition)
        check("气温不是 0 (数据解析正确)", report.temperature != 0.0)
    except WeatherError as exc:
        check(f"查「{city}」", False, str(exc))
    finally:
        await client.aclose()


async def test_live() -> None:
    print("\n[3] 真实调用 Open-Meteo")
    await _live("北京")
    await _live("上海")

    # 没说城市 -> 应该抛 CityNotSpecified (上层反问用户)
    client = WeatherClient(timeout=10.0)
    try:
        await client.query("")
        check("没说城市时抛 CityNotSpecified", False, "没有抛异常")
    except CityNotSpecified:
        check("没说城市时抛 CityNotSpecified", True)
    except WeatherError as exc:
        check("没说城市时抛 CityNotSpecified", False, f"抛的是 {type(exc).__name__}")
    finally:
        await client.aclose()

    # 配了 default_city -> 没城市就用它
    client = WeatherClient(timeout=10.0, default_city="广州")
    try:
        report = await client.query("")
        check(
            "配了 default_city 时用默认城市",
            report.city.startswith("广州"),
            report.city,
        )
    except WeatherError as exc:
        check("配了 default_city 时用默认城市", False, str(exc))
    finally:
        await client.aclose()

    # 查不到的城市 -> CityNotFound
    client = WeatherClient(timeout=10.0)
    try:
        await client.query("阿巴瓦阿巴瓦阿巴瓦")
        check("查不到的城市抛 CityNotFound", False, "没有抛异常")
    except CityNotFound:
        check("查不到的城市抛 CityNotFound", True)
    except WeatherError as exc:
        check("查不到的城市抛 CityNotFound", False, f"抛的是 {type(exc).__name__}")
    finally:
        await client.aclose()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="真实调 API")
    args = parser.parse_args()

    test_city_extract()
    test_match()
    if args.live:
        await test_live()

    print(f"\n通过 {PASSED} / 失败 {FAILED}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
