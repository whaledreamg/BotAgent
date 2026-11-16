"""参数提取器框架：统一的参数提取接口。

支持：
- 不同参数类型的提取器（城市、物体等）
- 提取器的自动注册和查询
- 易于扩展的设计
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

logger = logging.getLogger("agent.parameter_extractors")


class BaseParameterExtractor(ABC):
    """参数提取器基类。"""

    def __init__(self, param_type: str, description: str = ""):
        """初始化提取器。

        Args:
            param_type: 参数类型（如 "city", "object"）
            description: 提取器描述
        """
        self.param_type = param_type
        self.description = description

    @abstractmethod
    def extract(self, text: str) -> Optional[str]:
        """从文本中提取参数。

        Args:
            text: 输入文本

        Returns:
            提取的参数值，如果提取失败返回 None
        """
        pass

    def is_valid_result(self, value: str) -> bool:
        """检查提取结果是否有效。

        子类可以重写此方法进行自定义验证。

        Args:
            value: 提取的值

        Returns:
            是否有效
        """
        return bool(value and len(value.strip()) > 0)


class CityExtractor(BaseParameterExtractor):
    """城市名提取器。"""

    def __init__(self):
        super().__init__("city", "从文本中提取城市名")

    def extract(self, text: str) -> Optional[str]:
        """从「查一下北京天气」这类句子里挑出可能的城市名。

        挑不出就返回 None —— None 代表「没指定城市」。

        **这里做的是「粗筛」, 不是最终判定**: 句子里可能混着「适合出门」
        这种没法穷举的修饰词。所以结果还要交给天气 API 校验。
        """
        s = text.strip()

        # 先处理连接词和"再查时间"这样的后续任务标记
        # 「先查南京天气再查时间」-> 「先查南京天气」
        # 「先查南京天气然后讲个笑话」-> 「先查南京天气」
        s = re.sub(r"(再|然后|接着|和|跟|以及)(查|看|问|说|讲|唱|播放|放|开始|进行).*$", "", s)
        # 更宽松的连接词匹配（即使后面没有特定动词）
        s = re.sub(r"(然后|接着|之后|完了|做好了)(\s*[^天气]+)?$", "", s)

        # 天气相关的词本身不是城市
        s = re.sub(r"(天气预报|天气|气温|温度|下雨|下雪|刮风|台风)", "", s)

        # 疑问词/副词/语气词 —— 都不属于城市名
        s = re.sub(
            r"(怎么样|怎么样了|如何|好不好|是否|能不能|可不可以|会不会|要不要"
            r"|适合|不适合|值得|需要|大概|大约|多少|几"
            r"|一下|一下的|吗|呢|啊|呀|哈|嘛|的)",
            "",
            s,
        )
        # 时间词
        s = re.sub(r"(今天|今日|明天|明日|后天|昨天|现在|目前|此刻|当下|今儿|时间)", "", s)

        # 动词前缀 —— 可能出现在城市名前面, 要**循环剥**
        for _ in range(4):
            new = re.sub(
                r"^(帮我|帮忙|给我|替我|请问|请|查询|查一下|查查|查|看看|看下|看|看一下|先|后|想要|让|让我|告诉我|告诉)",
                "",
                s,
            )
            if new == s:
                break
            s = new

        # 只保留中英文数字
        s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)

        # 少于 2 个字不可能是地名 —— 当成「没指定城市」
        return s if len(s) >= 2 else None

    def is_valid_result(self, value: str) -> bool:
        """检查城市提取结果是否有效。

        检查是否包含异常情况：
        - 包含连接词（"和"、"跟"）
        - 末尾有修饰词（"后面"、"前面"）
        - 开头残留动词（"看"、"听"）
        """
        if not super().is_valid_result(value):
            return False

        # 检查连接词
        if any(c in value for c in ["和", "跟", "、"]):
            logger.debug(f"[参数提取] 城市包含连接词: {value}")
            return False

        # 检查末尾修饰词
        if re.search(r"(后面|前面|里面|上面|下面)$", value):
            logger.debug(f"[参数提取] 城市末尾有修饰词: {value}")
            return False

        # 检查开头残留动词
        if value.startswith(("看", "听", "瞧", "瞅")):
            logger.debug(f"[参数提取] 城市开头有动词: {value}")
            return False

        return True


class ObjectExtractor(BaseParameterExtractor):
    """物体名提取器。"""

    def __init__(self):
        super().__init__("object", "从文本中提取物体名")

    def extract(self, text: str) -> Optional[str]:
        """从「找一下手机」这类句子里提取物体名。"""
        s = text.strip()

        if not s:
            return None

        # 第 1 步：移除后缀疑问词和虚词（从末尾开始）
        s = re.sub(
            r"(在哪里|在哪儿|在哪|有没有|有没|看见了吗|看到没|看见|看到|吗|呢|啊|呀|了|的|有|是|怎么样|怎么样了|如何|怎样)$",
            "",
            s,
        )

        # 第 2 步：移除前缀动词/辅词和"一下"（循环移除）
        for _ in range(4):
            new = re.sub(
                r"^(找|找到|找一下|找找|找一找|拿|拿一下|看|看看|看下|帮我|帮忙|请|请问|一下|一|下)",
                "",
                s,
            )
            if new == s:
                break
            s = new

        # 第 3 步：移除数量词
        s = re.sub(r"^(个|两个|三个|几个|二|三|四|五|六|七|八|九|十)", "", s)

        # 第 4 步：移除位置词
        s = re.sub(r"^(桌上|桌下|桌里|手里|手上|柜里|柜上)", "", s)

        # 第 5 步：只保留中英文
        s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)

        # 至少 1 个字
        return s if len(s) >= 1 else None


class LocationExtractor(BaseParameterExtractor):
    """位置/地点提取器。"""

    def __init__(self):
        super().__init__("location", "从文本中提取位置/地点")

    def extract(self, text: str) -> Optional[str]:
        """从文本中提取位置名。"""
        # 与城市提取器类似的逻辑
        s = text.strip()

        # 移除位置相关动词
        s = re.sub(r"^(在|去|到|前往|出发|离开|前往)", "", s)

        # 只保留中英文
        s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)

        return s if len(s) >= 2 else None


class ParameterExtractorRegistry:
    """参数提取器注册中心。"""

    def __init__(self):
        """初始化提取器注册表。"""
        self._extractors: Dict[str, BaseParameterExtractor] = {}
        self._register_default_extractors()

    def _register_default_extractors(self):
        """注册默认的提取器。"""
        self.register(CityExtractor())
        self.register(ObjectExtractor())
        self.register(LocationExtractor())
        logger.info("[提取器注册] 注册了 3 个默认提取器")

    def register(self, extractor: BaseParameterExtractor):
        """注册一个提取器。

        Args:
            extractor: 提取器实例
        """
        self._extractors[extractor.param_type] = extractor
        logger.info(f"[提取器注册] 注册提取器: {extractor.param_type}")

    def get(self, param_type: str) -> Optional[BaseParameterExtractor]:
        """获取指定类型的提取器。

        Args:
            param_type: 参数类型

        Returns:
            提取器实例，如果不存在返回 None
        """
        return self._extractors.get(param_type)

    def extract(self, param_type: str, text: str) -> Optional[str]:
        """使用对应的提取器提取参数。

        Args:
            param_type: 参数类型
            text: 输入文本

        Returns:
            提取的参数值，如果提取失败返回 None
        """
        extractor = self.get(param_type)
        if not extractor:
            logger.warning(f"[提取器] 未找到类型为 {param_type} 的提取器")
            return None

        try:
            result = extractor.extract(text)
            if result:
                logger.debug(f"[提取器] {param_type} 提取成功: {result}")
            return result
        except Exception as e:
            logger.error(f"[提取器] {param_type} 提取失败: {e}")
            return None

    def is_valid_result(self, param_type: str, value: str) -> bool:
        """检查提取结果是否有效。

        Args:
            param_type: 参数类型
            value: 提取的值

        Returns:
            是否有效
        """
        extractor = self.get(param_type)
        if not extractor:
            # 没有特殊验证器，只检查非空
            return bool(value and len(value.strip()) > 0)

        return extractor.is_valid_result(value)


# 全局提取器注册表实例
_global_registry: Optional[ParameterExtractorRegistry] = None


def get_global_registry() -> ParameterExtractorRegistry:
    """获取全局提取器注册表。

    Returns:
        全局 ParameterExtractorRegistry 实例
    """
    global _global_registry
    if _global_registry is None:
        _global_registry = ParameterExtractorRegistry()
    return _global_registry


__all__ = [
    "BaseParameterExtractor",
    "CityExtractor",
    "ObjectExtractor",
    "LocationExtractor",
    "ParameterExtractorRegistry",
    "get_global_registry",
]
