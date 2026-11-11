"""机械臂运动学客户端 —— 视觉服务用来拿基座在世界系下的位姿。

**为什么需要它**: 当目标是**世界系**时, 相机到世界系的关系是

    T_world_cam = T_world_base · T_base_cam

``T_base_cam`` 是标定出来的静态量, 但 ``T_world_base`` 会随升降柱升降、
挂接安装而变化 —— 必须运行时从控制器拿。

控制器那边由 ``arm_service`` 的 ``/v1/kinematics`` 接口提供数据
(它把 ``robot/status`` 里的 ``cartesian`` / ``baseFrame`` 存下来了)。

**跨环境**: 视觉服务跑在 conda ``yolo`` 环境, arm_service 跑在
``botagent`` 环境 —— 所以这里只用 HTTP, 不 import arm_service 的代码。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("vision.arm_kinematics")


class ArmKinematicsClient:
    """``arm_service /v1/kinematics`` 的 HTTP 客户端。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8802",
        timeout: float = 3.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            logger.debug("请求 %s 失败: %s", url, exc)
        except Exception as exc:
            logger.debug("请求 %s 出错: %s", url, exc)
        return None

    def snapshot(self, refresh: bool = False) -> Optional[Dict[str, Any]]:
        """拿一份运动学快照。

        ``refresh=True`` 会让 arm_service 主动向控制器拉一次最新状态。
        """
        path = "/v1/kinematics?refresh=true" if refresh else "/v1/kinematics"
        return self._get(path)

    @staticmethod
    def base_frame(snapshot: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
        """从快照里取第一条臂的基座世界位姿。

        取第一条是因为通常只有一条臂的基座是「相机所在的那个」——
        真正要紧的是这个机器人身上的相机。
        """
        if not snapshot:
            return None
        arms = snapshot.get("arms") or {}
        for _name, data in arms.items():
            if not isinstance(data, dict):
                continue
            base = data.get("base_frame")
            if isinstance(base, dict):
                return base
        return None


__all__ = ["ArmKinematicsClient"]
