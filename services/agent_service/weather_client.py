"""天气服务客户端 (Open-Meteo)。

两个数据源, 全部免费、**免 key**:
- 地理编码  https://geocoding-api.open-meteo.com/v1/search
- 天气预报  https://api.open-meteo.com/v1/forecast

**为什么用 Open-Meteo**: 不需要申请 API key, 不需要注册, 部署时不会有
「key 没配导致功能不可用」这种坑; 城市名支持中文 (language=zh)。

**必须由用户说出城市名**: 语音助手里「今天天气怎么样」这种说法很常见,
但没说城市就没法查 —— 与其靠 IP 定位去猜 (IP 定位在移动网络/代理下
经常落在几百公里外, 报出来的城市是错的), 不如**反问用户一句**
「查哪个城市的天气」。
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger("agent.weather")

#: httpx 不认识的代理协议 (如 socks://) —— 出现这类环境变量时 httpx 会直接抛
#: ValueError, 导致整个请求 500, 所以这里先把它们摘掉。
_BAD_PROXY_SCHEMES = ("socks://", "socks4://", "socks5://", "socks4a://", "socks5h://")
_PROXY_ENV_KEYS = (
    "all_proxy", "ALL_PROXY",
    "http_proxy", "HTTP_PROXY",
    "https_proxy", "HTTPS_PROXY",
    "socks_proxy", "SOCKS_PROXY",
    "socks5_proxy", "SOCKS5_PROXY",
)


def _sanitize_proxy_env() -> None:
    for key in _PROXY_ENV_KEYS:
        value = os.environ.get(key)
        if value and value.lower().startswith(_BAD_PROXY_SCHEMES):
            os.environ.pop(key, None)


class WeatherError(RuntimeError):
    """天气服务调用失败。"""


class CityNotSpecified(WeatherError):
    """用户没说城市。

    这不是故障, 是**需要反问用户** —— 由上层回一句
    「查哪个城市的天气」, 而不是报「服务不可用」。
    """


class CityNotFound(WeatherError):
    """城市名查不到 (可能是说错了, 也可能是小地名)。"""


#: WMO weather code -> 中文天气现象。
#: 只列 Open-Meteo 实际会返回的码 (0~99)。
WMO_CODES: Dict[int, str] = {
    0: "晴",
    1: "晴间多云",
    2: "多云",
    3: "阴",
    45: "有雾",
    48: "有雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "阵雨",
    81: "中等阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "强雷阵雨伴冰雹",
}


def describe_code(code: Any) -> str:
    """WMO 码 -> 中文天气现象; 未知码返回「未知」。"""
    try:
        return WMO_CODES.get(int(code), "未知")
    except (TypeError, ValueError):
        return "未知"


@dataclass
class WeatherReport:
    """一次天气查询的结果。"""

    #: 城市名 (已归一化, 如「北京」)
    city: str = ""
    #: 天气现象 (中文)
    condition: str = ""
    #: 当前气温 (°C)
    temperature: float = 0.0
    #: 体感温度 (°C)
    apparent: float = 0.0
    #: 相对湿度 (%)
    humidity: int = 0
    #: 风速 (km/h)
    wind: float = 0.0
    #: 今日最高温 (°C)
    temp_max: float = 0.0
    #: 今日最低温 (°C)
    temp_min: float = 0.0
    #: 今日最大降水概率 (%)
    precip_prob: int = 0
    #: 附加信息 (给日志看)
    extra: Dict[str, Any] = field(default_factory=dict)


class WeatherClient:
    """天气客户端。

    城市 -> 经纬度的结果会缓存 (默认 24 小时, 地理编码结果不会变)。
    """

    def __init__(
        self,
        geocode_url: str = "https://geocoding-api.open-meteo.com/v1/search",
        forecast_url: str = "https://api.open-meteo.com/v1/forecast",
        timeout: float = 5.0,
        default_city: str = "",
        geo_cache_ttl: float = 86400.0,
    ) -> None:
        _sanitize_proxy_env()
        self.geocode_url = geocode_url
        self.forecast_url = forecast_url
        self.timeout = timeout
        #: 配置里写死的默认城市; 留空则**必须由用户说出城市名**
        self.default_city = default_city
        self._geo_cache_ttl = geo_cache_ttl

        # 天气接口是**外网**, 要尊重代理环境变量 (和 LLM 客户端一致)。
        self._client = httpx.AsyncClient(timeout=timeout)

        self._geo_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------ 对外

    async def query(self, city: str = "") -> WeatherReport:
        """查天气。

        `city` 为空 (或等于「默认城市」这类占位词) 时:
        - 配了 `default_city` -> 查那个城市
        - 没配 -> 抛 `CityNotSpecified`, 让上层反问用户
        """
        city = (city or "").strip()
        if not city or city in ("默认城市", "默认", "当前城市", "本地"):
            if self.default_city:
                city = self.default_city
            else:
                raise CityNotSpecified("用户没说城市")

        place = await self._resolve_place(city)

        report = await self._forecast(place["latitude"], place["longitude"])
        report.city = place.get("name") or city
        return report

    async def _resolve_place(self, city: str) -> Dict[str, Any]:
        """把用户说的城市名解析成经纬度。

        用户的话里可能夹着修饰词 (「适合出门」「现在」), 直接拿整串去查
        一定查不到。所以这里**逐级缩短**候选词重试:
        「适合出门」-> 「适合出」-> 「适合」-> 失败。

        缩短时优先砍**尾部**, 因为中文地名的修饰通常在后面
        (「深圳的天气」-> 「深圳」)。

        全部候选都查不到就抛 `CityNotFound`。
        """
        candidates = [city]
        # 逐级砍尾, 最少留 2 个字 (两字城市: 北京/上海/广州/南京…)
        trimmed = city
        while len(trimmed) > 2:
            trimmed = trimmed[:-1]
            candidates.append(trimmed)

        # 完整串优先; 砍尾的候选**要求有常住人口** ——
        # 否则「阿巴瓦阿巴瓦阿巴瓦」砍到「阿巴」会命中西藏阿巴县这种
        # 没有人口数据的小地名, 报出来的天气是那个无人区的。
        for idx, cand in enumerate(candidates):
            try:
                return await self._geocode(cand, require_population=(idx > 0))
            except CityNotFound:
                continue
        raise CityNotFound(f"查不到城市: {city}")

    # ------------------------------------------------------------ 地理编码

    async def _geocode(
        self, city: str, require_population: bool = False
    ) -> Dict[str, Any]:
        """城市名 -> 经纬度 (带缓存)。

        **只认中国境内 (含港澳台)** 的结果 —— 中文语音助手服务的是国内用户,
        而 Open-Meteo 的地名库是全球的: 搜「阿巴」会匹配到埃塞俄比亚的
        阿巴市, 搜「南京」也可能匹配到国外同名的地方。限定国家才不会
        给出离谱的答案。

        `require_population=True` 时, 必须有人口数据才算命中 ——
        用于「砍尾候选」, 避免匹配到同名的小村小地名。
        """
        key = city.strip()
        now = time.time()
        cached = self._geo_cache.get(key)
        if cached and now - cached[0] < self._geo_cache_ttl:
            return cached[1]

        try:
            resp = await self._client.get(
                self.geocode_url,
                params={
                    "name": key,
                    "count": 10,
                    "language": "zh",
                    "format": "json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            import traceback
            error_msg = f"查询城市失败: {exc}\n{traceback.format_exc()}"
            raise WeatherError(error_msg) from exc

        results = data.get("results") or []
        # 先筛出中国境内的结果; 一个都没有再看全球的 (小地名可能没标国家)
        cn = [r for r in results if str(r.get("country_code") or "").upper() == "CN"]
        pool = cn or results
        if not pool:
            raise CityNotFound(f"查不到城市: {key}")

        # **只认「有常住人口」的城镇**。
        #
        # Open-Meteo 的地名库混着各种行政单位: 搜「上海」会同时返回
        # 上海市 (2487万) 和浙江/云南/四川一堆同名的「上海」乡/村
        # (没有人口数据); 搜「阿巴」会返回西藏阿巴县 (没有人口数据)。
        # 没有人口数据的条目要么是同名村, 要么是误匹配 —— 直接排掉。
        with_pop = [r for r in pool if (r.get("population") or 0) >= 20000]
        if with_pop:
            pool = with_pop
        elif require_population:
            raise CityNotFound(f"查不到城市: {key}")

        # 取人口最多的那个 —— 「北京」会匹配到北京市/重庆市/成都市,
        # 人口最多的那个才是用户通常想查的。
        best = max(pool, key=lambda r: r.get("population") or 0)

        # 名字要对得上 —— 模糊匹配偶尔会给出不相干的地名
        name = str(best.get("name") or "")
        if name and not (name == key or key.startswith(name) or name.startswith(key)):
            raise CityNotFound(f"查不到城市: {key}")

        place = {
            "name": name or key,
            "latitude": float(best.get("latitude") or 0.0),
            "longitude": float(best.get("longitude") or 0.0),
        }
        self._geo_cache[key] = (now, place)
        return place

    # ------------------------------------------------------------ 天气预报

    async def _forecast(self, latitude: float, longitude: float) -> WeatherReport:
        """取当前实况 + 今日预报。"""
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "current": (
                "temperature_2m,relative_humidity_2m,"
                "apparent_temperature,weather_code,wind_speed_10m"
            ),
            "daily": (
                "temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max"
            ),
            "timezone": "auto",
            "forecast_days": 1,
        }
        try:
            resp = await self._client.get(self.forecast_url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            import traceback
            error_msg = f"查询天气失败: {exc}\n{traceback.format_exc()}"
            raise WeatherError(error_msg) from exc

        current = data.get("current") or {}
        daily = data.get("daily") or {}

        def _num(value: Any, default: float = 0.0) -> float:
            """取一个数值字段。

            **注意**: `current` 段里是**标量**, `daily` 段里是**数组**
            (按天排)。这里两种都要认 —— 之前只当数组处理, 结果
            current 的温度全是 0。
            """
            if isinstance(value, (list, tuple)):
                value = value[0] if value else default
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        return WeatherReport(
            condition=describe_code(current.get("weather_code")),
            temperature=_num(current.get("temperature_2m")),
            apparent=_num(current.get("apparent_temperature")),
            humidity=int(_num(current.get("relative_humidity_2m"))),
            wind=_num(current.get("wind_speed_10m")),
            temp_max=_num(daily.get("temperature_2m_max")),
            temp_min=_num(daily.get("temperature_2m_min")),
            precip_prob=int(_num(daily.get("precipitation_probability_max"))),
            extra={"timezone": data.get("timezone", "")},
        )


__all__ = [
    "CityNotFound",
    "CityNotSpecified",
    "WeatherClient",
    "WeatherError",
    "WeatherReport",
    "describe_code",
    "WMO_CODES",
]
