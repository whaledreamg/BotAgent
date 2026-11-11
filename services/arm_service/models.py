"""HTTP / WebSocket 的请求与响应模型。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PlayRequest(BaseModel):
    """播放一个或一组动作。

    **两种写法** (二选一):
    - ``action``: 单个动作名
    - ``actions``: 动作名列表, 按顺序依次播放

    给 ``actions`` 时整个队列**只入控一次** —— 动作之间不松手, 机械臂
    运动连贯, 也省掉每个动作重新入控的 700ms 对齐开销。
    """

    #: 动作名 = JSON 文件名 (去掉 .json), 如 "左挥手" / "插兜"
    action: Optional[str] = Field(None, description="动作名 (JSON 文件名, 不含 .json)")
    #: 动作序列, 按顺序依次播放
    actions: List[str] = Field(
        default_factory=list, description="动作序列, 按顺序依次执行"
    )
    #: 倍速; 1.0 = 原速。0.1 ~ 3.0
    speed: float = Field(1.0, ge=0.1, le=3.0, description="播放倍速")
    #: 是否循环播放
    loop: bool = Field(False, description="循环播放")
    #: 是否允许在没有实测关节角时自动补拉一次
    fetch_joints: bool = Field(True, description="缺实测关节角时自动补拉")

    def action_list(self) -> List[str]:
        """归一化出要播放的动作列表。

        ``action`` 与 ``actions`` 都给时, 以 ``actions`` 为准 (它更具体)。
        """
        if self.actions:
            return [str(x).strip() for x in self.actions if str(x).strip()]
        if self.action:
            return [str(self.action).strip()]
        return []


class PlayResponse(BaseModel):
    """播放指令的即时应答 (播放是异步的)。"""

    ok: bool = True
    #: 当前开始播放的动作 (队列的第一个)
    action: str = ""
    key: str = ""
    #: 整个队列的总时长 (秒)
    duration: float = 0.0
    #: 队列里的动作 (按顺序)
    actions: List[str] = Field(default_factory=list)
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
    #: 队列里还没播的动作 (按顺序)。空 = 没有排队
    queue: List[str] = Field(default_factory=list)
    #: 整个队列的动作数 (含正在播的)
    queue_total: int = 0


class SafetyResponse(BaseModel):
    """安全门阀检查结果。"""

    ok: bool
    code: str = ""
    reason: str = ""
    elapsed_ms: int = 0
    checks: List[Dict[str, Any]] = []


class KinematicsResponse(BaseModel):
    """运动学快照 —— 手眼标定要用的位姿数据。

    **为什么要有这个接口**: 视觉服务做手眼标定时需要「工具在世界系
    下的位姿」来当量尺。控制器的 `robot/status.cartesian` 就是这个量,
    但它之前只存在于状态推送里, 没被存下来、也没有 HTTP 出口。

    字段全是**可选**的 —— 控制器版本不同, 字段名/结构可能不一样,
    拿不到就留空, 不要编。
    """

    ok: bool = True
    #: 各臂的运动学状态, key 是控制器里的机器人名
    arms: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    #: 升降柱状态 (相机装在升降柱顶端时需要它做动态补偿)
    lifting_column: Optional[Dict[str, Any]] = None
    #: 头/相机安装座在世界系下的位姿 {x,y,z,a,b,c}
    #:
    #: 相机装在机器人头部、且头以后会动时, 视觉服务需要它来把
    #: 「相机 -> 头座」的静态标定量补成「相机 -> 世界系」。
    #: 控制器没提供时为 None —— **不要编一个**, 宁可没有。
    head_frame: Optional[Dict[str, float]] = None
    #: 头部位姿是静态还是挂接派生 (static / derived)
    head_frame_kind: str = ""
    #: 控制器连上了没
    controller_connected: bool = False
    #: 快照时间 (毫秒, 单调时钟)
    timestamp_ms: int = 0
    #: 提示信息 (比如某项拿不到)
    notes: List[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    controller_connected: bool
    controller_ws: str
    actions: int
    phase: str
    streaming: bool
    safety_enabled: bool = True
