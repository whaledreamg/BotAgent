"""视觉服务的请求/响应模型。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class BBox(BaseModel):
    """像素框 (x1, y1, x2, y2)。"""

    x1: float
    y1: float
    x2: float
    y2: float

    def as_tuple(self):
        return (self.x1, self.y1, self.x2, self.y2)


class Pose3D(BaseModel):
    """3D 位置。

    `frame` 说明这个坐标在哪个系里:
    - "camera":      相机坐标系 (X右 Y下 Z前), 外参没标定时就是这个
    - "world":       世界坐标系, 需要外参 (+ 头部补偿)
    - "arm_base":    机械臂基座坐标系, 需要配好手眼外参
    - "left_base":   左臂基座坐标系 (双臂, 需要运行时基座位姿)
    - "right_base":  右臂基座坐标系
    """

    x: float
    y: float
    z: float
    frame: str = "camera"
    #: 深度是否可信 (物体反光/透明时会是 False)
    depth_valid: bool = True
    #: 检测框内有效深度像素占比
    valid_ratio: float = 0.0
    #: 沿光轴的距离 (米)
    depth: float = 0.0


class Keypoint(BaseModel):
    x: float
    y: float
    confidence: float


class Object(BaseModel):
    """一个被识别出来的物体。"""

    label: str
    confidence: float
    bbox: BBox
    pose: Optional[Pose3D] = None
    keypoints: Optional[List[Keypoint]] = None


class DetectRequest(BaseModel):
    """触发一次检测。"""

    #: 只保留这些类别 (空 = 全部)
    classes: Optional[List[str]] = None
    #: 置信度阈值覆盖
    conf: Optional[float] = None
    #: 用姿态模型 (需要已配置 pose_weights)
    pose: bool = False
    #: 返回坐标系: camera / world / arm_base / left_base / right_base
    frame: str = "camera"


class DetectResponse(BaseModel):
    """检测结果。"""

    objects: List[Object] = Field(default_factory=list)
    #: 本次检测的耗时 (毫秒)
    latency_ms: int = 0
    #: 帧的时间戳 (微秒)
    timestamp_us: int = 0
    #: 坐标系说明 (实际拿到的)
    frame: str = "camera"
    #: 请求的坐标系 (可能因为外参没配好而没拿到)
    requested_frame: str = "camera"


class ObjectSummary(BaseModel):
    """按类别聚合的摘要 —— Agent 播报用这个。"""

    label: str
    count: int
    #: 最近一次的 3D 位置 (第一个目标)
    pose: Optional[Pose3D] = None
    confidences: List[float] = Field(default_factory=list)


class StatusResponse(BaseModel):
    """服务状态。"""

    status: str = "ok"
    camera_connected: bool = False
    camera_name: str = ""
    camera_serial: str = ""
    detector_loaded: bool = False
    pose_loaded: bool = False
    device: str = "cpu"
    weights: str = ""
    #: 当前坐标系: camera / world / arm_base / left_base / right_base
    frame: str = "camera"
    extrinsic_configured: bool = False
    #: 是否用了动态外参 (双臂分档 / 头部补偿)
    extrinsic_dynamic: bool = False
    #: 动态外参现在能不能用 (基座位姿拿到了没)
    extrinsic_dynamic_ready: bool = True
    #: 标定矩阵描述的是「相机 -> 哪个系」: world / head / arm_base
    extrinsic_matrix_frame: str = "world"
    #: 双臂: 当前能拿到基座位姿的是哪几条臂 (["left", "right"])
    arms_available: List[str] = Field(default_factory=list)
    #: 是否做了头部动态补偿 (标定系为 head 时)
    head_compensation: bool = False
    #: 控制器给的头部位姿拿到了没
    head_frame_available: bool = False
    #: 内参是不是标定过的 (没标的话外参也不准)
    calibrated_intrinsics: bool = False
    last_detection_ms: int = 0
    last_object_count: int = 0
    errors: List[str] = Field(default_factory=list)
    #: 外参 provider 的自述
    provider: Dict[str, Any] = Field(default_factory=dict)


class HealthResponse(StatusResponse):
    pass


__all__ = [
    "BBox",
    "DetectRequest",
    "DetectResponse",
    "HealthResponse",
    "Keypoint",
    "Object",
    "ObjectSummary",
    "Pose3D",
    "StatusResponse",
]
