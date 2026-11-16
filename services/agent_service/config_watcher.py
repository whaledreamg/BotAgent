"""配置文件监听和热更新模块。

支持两种模式：
1. 文件监听模式：监听 agent.yaml 文件变化，自动触发热更新
2. 手动触发模式：通过 API 调用手动触发热更新

性能考量：
- 文件监听每 2 秒检查一次（stat() 操作 < 1ms）
- 仅在文件修改时执行热更新
- 不阻塞正常请求处理
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("config_watcher")


class ConfigWatcher:
    """监听配置文件变化，触发热更新回调。
    
    使用文件修改时间戳（mtime）检测变化，避免轮询文件内容。
    """

    def __init__(
        self,
        config_path: Path,
        callback: Callable[[], asyncio.coroutine],
        check_interval: float = 2.0,
    ) -> None:
        """初始化监听器。
        
        Args:
            config_path: 要监听的配置文件路径
            callback: 文件变化时的回调函数（async）
            check_interval: 检查间隔（秒），默认 2 秒
        """
        self.config_path = Path(config_path)
        self.callback = callback
        self.check_interval = check_interval
        self.last_mtime: Optional[float] = None
        self.is_running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """启动监听。"""
        if self.is_running:
            logger.warning("ConfigWatcher 已在运行")
            return

        self.is_running = True
        logger.info(f"ConfigWatcher 启动，监听文件: {self.config_path}")
        logger.info(f"检查间隔: {self.check_interval}s")

        while self.is_running:
            try:
                if self.config_path.is_file():
                    current_mtime = self.config_path.stat().st_mtime
                    # 首次运行初始化 last_mtime
                    if self.last_mtime is None:
                        self.last_mtime = current_mtime
                        logger.debug(f"初始化 mtime: {self.last_mtime}")
                    # 检测到文件更新
                    elif current_mtime > self.last_mtime:
                        logger.info(
                            f"检测到配置文件更新 "
                            f"(prev={self.last_mtime}, now={current_mtime})"
                        )
                        self.last_mtime = current_mtime
                        try:
                            await self.callback()
                        except Exception as e:
                            logger.error(f"执行热更新回调失败: {e}", exc_info=True)
                else:
                    logger.warning(f"配置文件不存在: {self.config_path}")

            except Exception as e:
                logger.error(f"监听配置文件时出错: {e}", exc_info=True)

            await asyncio.sleep(self.check_interval)

    async def stop(self) -> None:
        """停止监听。"""
        if not self.is_running:
            logger.debug("ConfigWatcher 未运行")
            return

        self.is_running = False
        logger.info("ConfigWatcher 已停止")

        # 等待监听任务完成
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("等待 ConfigWatcher 任务完成超时")
                self._task.cancel()

    def spawn_task(self) -> asyncio.Task:
        """生成后台监听任务。
        
        在 FastAPI 启动事件中使用：
        ```python
        @app.on_event("startup")
        async def startup():
            watcher_task = config_watcher.spawn_task()
            app.state.watcher_task = watcher_task
        ```
        """
        self._task = asyncio.create_task(self.start())
        return self._task

    def __repr__(self) -> str:
        return (
            f"ConfigWatcher(path={self.config_path}, "
            f"interval={self.check_interval}s, running={self.is_running})"
        )
