"""分支处理器注册表。

按配置 (configs/agent.yaml 的 handlers 段) 动态导入并实例化 Handler。
新增分支 = 加一个 handlers/xxx.py + 一段 YAML, 无需改动这里。
"""

from __future__ import annotations

import importlib
import logging
from typing import Dict, List, Optional

from config import Config, HandlerConfig
from handlers.base import Handler

logger = logging.getLogger("registry")


class Registry:
    """name -> Handler 实例的注册表。"""

    def __init__(self) -> None:
        self._handlers: Dict[str, Handler] = {}

    def load(self, cfg: Config, llm: Optional[object] = None) -> None:
        """按配置装载所有启用的分支。"""
        self._handlers.clear()
        for name, hcfg in cfg.handlers.items():
            if not hcfg.enabled:
                logger.info("分支 [%s] 已禁用, 跳过装载", name)
                continue
            handler = self._instantiate(name, hcfg, cfg, llm)
            if handler is None:
                continue
            handler.name = name
            handler.speak = hcfg.speak
            self._handlers[name] = handler
            logger.info("分支 [%s] 已装载: %s.%s", name, hcfg.module, hcfg.cls)

    def _instantiate(
        self, name: str, hcfg: HandlerConfig, cfg: Config, llm: Optional[object]
    ) -> Optional[Handler]:
        if not hcfg.module or not hcfg.cls:
            logger.error("分支 [%s] 配置缺少 module/class", name)
            return None
        try:
            module = importlib.import_module(hcfg.module)
        except ImportError as exc:
            logger.error("分支 [%s] 模块导入失败 (%s): %s", name, hcfg.module, exc)
            return None
        cls = getattr(module, hcfg.cls, None)
        if cls is None:
            logger.error("分支 [%s] 找不到类 %s", name, hcfg.cls)
            return None
        try:
            handler = cls(config=cfg, llm=llm)
        except Exception as exc:  # pragma: no cover - 构造失败降级
            logger.exception("分支 [%s] 实例化失败: %s", name, exc)
            return None
        if not isinstance(handler, Handler):
            logger.error("分支 [%s] 的 %s 不是 Handler 子类", name, hcfg.cls)
            return None
        return handler

    # ------------------------------------------------------------ 访问

    def get(self, name: str) -> Optional[Handler]:
        return self._handlers.get(name)

    def names(self) -> List[str]:
        return list(self._handlers.keys())

    def all(self) -> Dict[str, Handler]:
        return dict(self._handlers)

    def __contains__(self, name: object) -> bool:
        return name in self._handlers

    def __len__(self) -> int:
        return len(self._handlers)
