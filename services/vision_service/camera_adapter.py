"""相机适配器基类。

定义所有相机实现都应遵循的标准接口。这样，无论具体是 Orbbec 头部相机、
左腕相机还是右腕相机，上层服务代码都能以统一的方式使用它们。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger("vision.camera_adapter")


class CameraError(RuntimeError):
    """相机不可用或采帧失败。"""


@dataclass
class Intrinsics:
    """相机内参 (针孔模型)。

    3D 反算用: X = (u - cx) * Z / fx, Y = (v - cy) * Z / fy
    """

    fx: float = 0.0
    fy: float = 0.0
    cx: float = 0.0
    cy: float = 0.0
    width: int = 0
    height: int = 0

    @property
    def valid(self) -> bool:
        return self.fx > 0 and self.fy > 0 and self.width > 0 and self.height > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fx": round(self.fx, 4),
            "fy": round(self.fy, 4),
            "cx": round(self.cx, 4),
            "cy": round(self.cy, 4),
            "width": self.width,
            "height": self.height,
        }


@dataclass
class Frame:
    """一次采集的结果。"""

    #: 彩色图, BGR (OpenCV 习惯)
    color: Optional[np.ndarray] = None
    #: 深度图, 原始 uint16 值 (未乘 scale)
    depth_raw: Optional[np.ndarray] = None
    #: 深度值 -> 米的换算系数
    depth_scale: float = 0.001
    #: 彩色相机内参
    intrinsics: Intrinsics = None
    #: 深度相机内参 (深度图自己的分辨率)
    depth_intrinsics: Intrinsics = None
    #: 深度相机 -> 彩色相机 的外参 (3x3 旋转 + 3 维平移, 平移单位米)
    #: 没有时是 None —— 深度与彩色就是同一个坐标系。
    depth_to_color: Optional[Tuple[np.ndarray, np.ndarray]] = None
    #: 标定好的彩色内参 (覆盖 SDK 标称内参)。
    calibrated_intrinsics: Optional[Intrinsics] = None
    #: 标定好的畸变系数 (5 个或更多)
    calibrated_dist: Optional[np.ndarray] = None

    def __post_init__(self):
        if self.intrinsics is None:
            self.intrinsics = Intrinsics()
        if self.depth_intrinsics is None:
            self.depth_intrinsics = Intrinsics()

    @property
    def depth_meters(self) -> Optional[np.ndarray]:
        """深度图 (米)。"""
        if self.depth_raw is None:
            return None
        return self.depth_raw.astype(np.float32) * self.depth_scale


class CameraAdapter(ABC):
    """相机适配器基类。

    所有相机实现都应继承此类，实现标准接口。这样上层代码只需要与
    CameraAdapter 交互，而不用关心具体的相机硬件。

    使用示例::

        adapter = create_camera_adapter(cfg)  # 工厂方法
        adapter.open()
        frame = adapter.read()
        adapter.close()
    """

    @abstractmethod
    def open(self) -> None:
        """打开相机，启动数据流。失败时抛 CameraError。"""

    @abstractmethod
    def close(self) -> None:
        """停止数据流并释放资源。"""

    @abstractmethod
    def read(self, timeout_ms: Optional[int] = None) -> Optional[Frame]:
        """采一帧。超时返回 None。"""

    @property
    @abstractmethod
    def opened(self) -> bool:
        """相机是否已打开。"""

    @property
    @abstractmethod
    def device_info(self) -> Dict[str, Any]:
        """设备信息 (只读)。
        
        返回包含 name, serial_number, pid, vid 等字段的字典。
        """

    @property
    @abstractmethod
    def intrinsics(self) -> Intrinsics:
        """彩色相机内参 (只读)。"""

    @property
    @abstractmethod
    def depth_scale(self) -> float:
        """深度原始值 -> 米的换算系数 (只读)。"""

    @abstractmethod
    def map_bbox_to_depth(self, bbox: Tuple, color_intr: Intrinsics) -> Tuple:
        """把彩色图上的框映射到深度图上。

        当深度图与彩色图分辨率不同或坐标系不同时，需要做坐标变换。

        Args:
            bbox: (x1, y1, x2, y2) 彩色图上的框
            color_intr: 彩色相机内参

        Returns:
            (x1, y1, x2, y2) 深度图上的框，已裁到深度图范围内
        """

    def __enter__(self) -> CameraAdapter:
        """上下文管理器支持。"""
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        """上下文管理器支持。"""
        self.close()


__all__ = ["CameraError", "CameraAdapter", "Frame", "Intrinsics"]
