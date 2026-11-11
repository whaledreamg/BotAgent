"""视觉服务客户端。

Agent 的 tool 分支通过它问视觉服务「某某物体在哪」。

接口:
- GET  /health             相机/模型状态
- GET  /v1/objects/{name}  按类别名查目标 (取不到会现场检测一次)
- POST /v1/detect          触发一次检测

只依赖 httpx, 与 llm.py 用同一套代理清洗逻辑。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("agent.vision")

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


class VisionError(RuntimeError):
    """调用视觉服务失败。"""


class VisionClient:
    """视觉服务客户端。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8803",
        timeout: float = 10.0,
    ) -> None:
        _sanitize_proxy_env()
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        # trust_env=False: 视觉服务在本机 (127.0.0.1), 不该走任何代理。
        # 环境里全局设着 HTTP_PROXY 时, httpx 会把回环请求也发给代理,
        # 结果就是连不上自己的服务。
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, trust_env=False
        )

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------ 查询

    async def health(self) -> Dict[str, Any]:
        """服务状态。"""
        try:
            resp = await self._client.get("/health")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise VisionError(f"视觉服务健康检查失败: {exc}") from exc

    async def find(
        self, name: str, frame: str = "camera"
    ) -> Dict[str, Any]:
        """按类别名查物体。

        返回 {"found": bool, "count": int, "objects": [...]}。
        """
        try:
            resp = await self._client.get(
                f"/v1/objects/{name}", params={"frame": frame}
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise VisionError(f"查询物体失败: {exc}") from exc

    async def detect(
        self,
        classes: Optional[List[str]] = None,
        conf: Optional[float] = None,
        frame: str = "camera",
    ) -> Dict[str, Any]:
        """触发一次检测。"""
        payload: Dict[str, Any] = {"frame": frame}
        if classes:
            payload["classes"] = classes
        if conf is not None:
            payload["conf"] = conf
        try:
            resp = await self._client.post("/v1/detect", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise VisionError(f"检测失败: {exc}") from exc

    async def frames(
        self, x: float = 0.0, y: float = 0.0, z: float = 1.0
    ) -> Dict[str, Any]:
        """一次拿到所有坐标系下的同一个点 —— 调试标定用。"""
        try:
            resp = await self._client.get(
                "/v1/frames", params={"x": x, "y": y, "z": z}
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise VisionError(f"取坐标系快照失败: {exc}") from exc


__all__ = ["VisionClient", "VisionError"]
