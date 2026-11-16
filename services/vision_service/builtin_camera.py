"""内置笔记本摄像头适配器。

支持的相机:
  • 笔记本内置摄像头（通过 OpenCV）
  • USB 通用摄像头（通过 OpenCV）

不需要特殊 SDK，直接通过 OpenCV 的 VideoCapture 访问。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np

from camera_adapter import CameraAdapter, CameraError, Frame, Intrinsics

logger = logging.getLogger("vision.builtin_camera")


class BuiltinCamera(CameraAdapter):
    """笔记本摄像头适配器。
    
    使用 OpenCV 访问系统内置或 USB 连接的摄像头。
    
    使用场景:
      • 笔记本内置摄像头（设备 ID 通常是 0）
      • USB 摄像头（设备 ID 由系统分配）
      • 开发测试环境
    
    限制:
      • 仅支持 RGB 彩色，不提供深度信息
      • 不支持 align_depth_to_color (深度相机不可用)
      • 不支持原生内参标定（需手工输入或在线标定）
    """

    def __init__(self, cfg: Any) -> None:
        """初始化笔记本摄像头。
        
        Args:
            cfg: CameraConfig 对象，包含:
              - serial_number: 摄像头设备 ID (可以是数字或路径)
              - width, height: 分辨率 (0=自动)
              - fps: 帧率 (0=自动)
              - calibrated_intrinsics: 标定内参 (可选)
        """
        self.cfg = cfg
        self._cap = None
        self._device_id = self._parse_device_id(cfg.serial_number)
        self._opened = False
        
        # 分辨率和帧率
        self._width = int(cfg.width or 0)
        self._height = int(cfg.height or 0)
        self._fps = int(cfg.fps or 0)
        
        # 内参 (如果有标定则用标定的，否则需要手工设置或在线标定)
        self._intrinsics = self._load_intrinsics(cfg)
        self._device_info: Dict[str, Any] = {}

    def _parse_device_id(self, serial_number: str) -> int:
        """解析设备 ID。
        
        可以是:
          - 空字符串: 使用第一个可用摄像头 (0)
          - 数字: 直接用作设备 ID
          - 路径: /dev/video0 等
        """
        if not serial_number or serial_number.strip() == "":
            return 0
        
        # 如果是路径，尝试提取数字
        if "/" in serial_number:
            import re
            match = re.search(r"(\d+)", serial_number)
            if match:
                return int(match.group(1))
            return 0
        
        # 尝试直接转换为整数
        try:
            return int(serial_number)
        except ValueError:
            logger.warning(f"无法解析设备 ID: {serial_number}, 使用默认值 0")
            return 0

    def _load_intrinsics(self, cfg: Any) -> Intrinsics:
        """加载或创建内参。
        
        优先级:
          1. 配置中的标定内参
          2. 默认内参 (需要通过标定工具更新)
        """
        intr = Intrinsics()
        
        # 如果有标定内参，加载它
        if cfg.calibrated_intrinsics:
            cal = cfg.calibrated_intrinsics
            intr.fx = float(cal.get("fx", 0))
            intr.fy = float(cal.get("fy", 0))
            intr.cx = float(cal.get("cx", 0))
            intr.cy = float(cal.get("cy", 0))
            intr.width = int(cal.get("width", 0))
            intr.height = int(cal.get("height", 0))
        
        return intr

    def open(self) -> None:
        """打开摄像头。
        
        Raises:
            CameraError: 如果摄像头无法打开
        """
        if self._opened:
            return
        
        try:
            self._cap = cv2.VideoCapture(self._device_id)
            
            if not self._cap.isOpened():
                raise CameraError(f"无法打开摄像头设备 {self._device_id}")
            
            # 设置分辨率
            if self._width > 0 and self._height > 0:
                self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
                self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
            
            # 设置帧率
            if self._fps > 0:
                self._cap.set(cv2.CAP_PROP_FPS, self._fps)
            
            # 获取实际分辨率
            actual_width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            actual_fps = self._cap.get(cv2.CAP_PROP_FPS)
            
            logger.info(
                f"打开摄像头成功 (设备 {self._device_id}): "
                f"{actual_width}x{actual_height}@{actual_fps} FPS"
            )
            
            # 更新分辨率
            self._width = actual_width
            self._height = actual_height
            
            # 如果没有内参，用分辨率计算默认内参
            if not self._intrinsics.valid:
                self._intrinsics.width = actual_width
                self._intrinsics.height = actual_height
                # 假设 FOV 约 60 度的摄像头
                self._intrinsics.fx = actual_width
                self._intrinsics.fy = actual_height
                self._intrinsics.cx = actual_width / 2.0
                self._intrinsics.cy = actual_height / 2.0
                logger.warning(
                    "未配置内参，使用估算值。请通过标定工具更新配置中的 "
                    "calibrated_intrinsics"
                )
            
            self._device_info = {
                "name": f"Builtin/USB Camera (Device {self._device_id})",
                "serial_number": str(self._device_id),
                "pid": 0,
                "vid": 0,
                "type": "builtin",
            }
            
            self._opened = True
            
        except Exception as e:
            logger.error(f"打开摄像头失败: {e}")
            raise CameraError(f"打开摄像头失败: {e}")

    def close(self) -> None:
        """关闭摄像头。"""
        if self._cap is not None:
            self._cap.release()
            self._cap = None
            self._opened = False
            logger.debug("摄像头已关闭")

    def read(self, timeout_ms: Optional[int] = None) -> Optional[Frame]:
        """采一帧。
        
        Args:
            timeout_ms: 超时时间 (毫秒，未使用，因为 OpenCV 没有超时机制)
        
        Returns:
            Frame 对象，或 None 如果采帧失败
        """
        if not self._opened or self._cap is None:
            return None
        
        try:
            ret, frame_bgr = self._cap.read()
            
            if not ret or frame_bgr is None:
                logger.warning("采帧失败")
                return None
            
            # OpenCV 返回 BGR，保持原有格式
            return Frame(
                color=frame_bgr,
                depth_raw=None,  # 没有深度信息
                depth_scale=0.001,
                intrinsics=self._intrinsics,
                depth_intrinsics=Intrinsics(),  # 空内参
                depth_to_color=None,  # 没有深度-彩色映射
                calibrated_intrinsics=None,
                calibrated_dist=None,
            )
            
        except Exception as e:
            logger.error(f"采帧异常: {e}")
            return None

    @property
    def opened(self) -> bool:
        """摄像头是否已打开。"""
        return self._opened

    @property
    def device_info(self) -> Dict[str, Any]:
        """设备信息。"""
        return self._device_info.copy()

    @property
    def intrinsics(self) -> Intrinsics:
        """彩色相机内参。"""
        return self._intrinsics

    @property
    def depth_scale(self) -> float:
        """深度换算系数。
        
        笔记本摄像头没有深度信息，返回 1.0。
        """
        return 1.0

    def map_bbox_to_depth(self, bbox: Tuple, color_intr: Intrinsics) -> Tuple:
        """映射框到深度图。
        
        笔记本摄像头没有深度图，直接返回原框。
        """
        return bbox


__all__ = ["BuiltinCamera"]
