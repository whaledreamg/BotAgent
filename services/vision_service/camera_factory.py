"""相机工厂。

根据配置文件中的相机品牌和类型，创建对应的相机适配器实例。

支持的相机品牌:
  • "" (空): 笔记本内置摄像头（BuiltinCamera）
  • "orbbec": Orbbec 3D 相机（OrbbecCamera）
  • "usb3vision": USB3Vision 相机（TODO）
  • "realsense": Intel RealSense 相机（TODO）
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from camera_adapter import CameraAdapter, CameraError
from camera import OrbbecCamera
from builtin_camera import BuiltinCamera

logger = logging.getLogger("vision.camera_factory")


def create_camera_adapter(cfg: Any, camera_type: str = "head") -> CameraAdapter:
    """根据相机品牌和类型创建适配器。

    Args:
        cfg: 完整的配置对象（通常是 Config 实例）
        camera_type: 相机类型 ("head" / "left_wrist" / "right_wrist")

    Returns:
        CameraAdapter 的一个实现实例

    Raises:
        CameraError: 如果相机品牌不支持或配置无效
    """
    camera_type = str(camera_type or "head").strip().lower()
    
    # 提取该相机类型的配置
    camera_cfg = _get_camera_config(cfg, camera_type)
    
    # 根据品牌创建对应的相机适配器
    camera_brand = str(camera_cfg.camera_brand or "").strip().lower()
    
    if camera_brand == "" or camera_brand == "builtin":
        # 空品牌名 = 笔记本内置摄像头
        logger.info(f"创建笔记本摄像头适配器 ({camera_type})")
        return BuiltinCamera(camera_cfg)
    
    elif camera_brand == "orbbec":
        # Orbbec 3D 相机
        logger.info(f"创建 Orbbec 相机适配器 ({camera_type})")
        return OrbbecCamera(camera_cfg)
    
    elif camera_brand == "usb3vision":
        # USB3Vision 相机（待实现）
        raise CameraError(
            f"暂不支持 USB3Vision 相机。"
            f"如需支持，请实现 USB3VisionCamera 类并注册到工厂"
        )
    
    elif camera_brand == "realsense":
        # Intel RealSense 相机（待实现）
        raise CameraError(
            f"暂不支持 Intel RealSense 相机。"
            f"如需支持，请实现 RealSenseCamera 类并注册到工厂"
        )
    
    else:
        raise CameraError(
            f"不支持的相机品牌: {camera_brand}。"
            f"支持的品牌: '', 'builtin', 'orbbec', 'usb3vision', 'realsense'"
        )


def _get_camera_config(cfg: Any, camera_type: str) -> Any:
    """从配置中提取特定相机类型的参数。

    Args:
        cfg: 完整的配置对象
        camera_type: 相机类型

    Returns:
        该相机类型的配置对象
    """
    # 优先尝试从 camera_configs 中取相机类型专用配置
    camera_configs = getattr(cfg, "camera_configs", {})
    if camera_type in camera_configs:
        return camera_configs[camera_type]

    # 向后兼容：直接用 cfg.camera（原有的结构）
    base_camera = getattr(cfg, "camera", None)
    if base_camera is not None:
        # 如果只有一个 camera 配置，所有类型都用它
        return base_camera

    raise CameraError(f"找不到相机类型 {camera_type} 的配置")


__all__ = ["create_camera_adapter"]
