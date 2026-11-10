"""HTTP / WebSocket 的请求与响应模型。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PlayRequest(BaseModel):
    """播放一个动作。"""

    #: 动作名 = JSON 文件名 (去掉 .json), 如 "左挥手" / "插兜"
    action: str = Field(..., description="动作名 (JSON 文件名, 不含 .json)")
    #: 倍速; 1.0 = 原速。0.1 ~ 3.0
    speed: float = Field(1.0, ge=0.1, le=3.0, description="播放倍速")
    #: 是否循环播放
    loop: bool = Field(False, description="循环播放")
    #: 是否允许在没有实测关节角时自动补拉一次
    fetch_joints: bool = Field(True, description="缺实测关节角时自动补拉")


class PlayResponse(BaseModel):
    """播放指令的即时应答 (播放是异步的)。"""

    ok: bool = True
    action: str = ""
    key: str = ""
    duration: float = 0.0
    phase: str = "entering"
    message: str = ""


class StopResponse(BaseModel):
    ok: bool = True
    phase: str = "idle"
    message: str = ""


class ActionInfo(BaseModel):
    key: str
    label: str
    source: str
    index: int
    duration: float
    segments: int


class ActionListResponse(BaseModel):
    count: int
    files: List[str]
    labels: List[str]
    actions: List[ActionInfo]

class StatusResponse(BaseModel):
    """当前播放状态。"""

    phase: str
    controller_connected: bool
    streaming: bool
    playback_active: bool
    action: Optional[str] = None
    current_time: float = 0.0
    total_duration: float = 0.0
    progress: float = 0.0
    left_arm: str = ""
    right_arm: str = ""
    last_error: str = ""
    #: 最近一次安全门阀检查结果
    safety: Optional[Dict[str, Any]] = None


class SafetyResponse(BaseModel):
    """安全门阀检查结果。"""

    ok: bool
    code: str = ""
    reason: str = ""
    elapsed_ms: int = 0
    checks: List[Dict[str, Any]] = []


class HealthResponse(BaseModel):
    status: str
    controller_connected: bool
    controller_ws: str
    actions: int
    phase: str
    streaming: bool
    safety_enabled: bool = True
