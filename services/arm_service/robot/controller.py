"""双臂实时控制状态机。

移植自 ActionPlanner 的 src/robot/RealtimeController.cpp, 去掉 Qt 依赖。

控制面: WebSocket 发 realtime/startControl / stopControl
数据面: UDP 9030, 扁平 JSON, 一包一个关节目标, 不回包。由 JointStreamer
         在独立线程按固定频率发送。

协议约束 (踩错就是机器人不动或直接被停控):
  1. 发起 startControl 后必须立刻并行发首包, 不能等响应。首次入控构建求解器
     可能耗时数百毫秒, 等着发就会错过 2000ms 首包等待窗, 控制器随即报
     「直达通道陈旧, 已自动回收到规划通道」。
  2. 首包必须贴近入控瞬间的实测关节角, 否则 firstFrameJumpRejected 并停控。
     所以先持续发实测锚点, 收到轨迹起点后再平滑过渡。
  3. 缺帧保持窗只有 5 个锚点周期 (下限 30ms), 发送必须稳定不抖。
  4. 只有 stopControl 才交还通道; 停发不会自动掉线。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from robot.connection import RobotConnection
from robot.streamer import JointStreamer
from timeline.model import JOINT_COUNT, JointState

logger = logging.getLogger("arm.controller")


class Phase:
    """服务对外暴露的播放阶段。"""

    IDLE = "idle"
    CONNECTING = "connecting"
    ENTERING = "entering"     # 入控中
    ALIGNING = "aligning"     # 对齐轨迹起点
    PLAYING = "playing"
    STOPPING = "stopping"
    ERROR = "error"


class ControllerError(RuntimeError):
    """入控或播放失败。"""


def _blend(a: List[float], b: List[float], s: float) -> List[float]:
    n = min(len(a), len(b))
    return [a[i] + s * (b[i] - a[i]) for i in range(n)]


class ArmController:
    """机器人实时控制: 入控 -> 对齐 -> 播放 -> 停控。"""

    def __init__(
        self,
        connection: RobotConnection,
        frequency: int = 100,
        command_port: int = 9030,
        lead_in_ms: int = 700,
        joint_limit: Optional[Dict[str, Any]] = None,
        idle_stop_ms: int = 1000,
        on_phase: Optional[Callable[[str, str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._conn = connection
        self._lead_in_ms = lead_in_ms
        self._joint_limit = joint_limit or {
            "enable": True,
            "maxVelocity": 1.5,
            "maxAcceleration": 5.0,
        }
        self._on_phase = on_phase
        self._on_error = on_error

        self._streamer = JointStreamer(port=command_port, frequency=frequency)
        self._streamer.set_idle_stop_ms(idle_stop_ms)

        self._lock = threading.RLock()
        self._phase = Phase.IDLE
        self._last_error = ""

        self._left_name = ""
        self._right_name = ""
        #: 各臂的使能状态。机器人下使能时入控会被拒 (24077), 播放前要补使能。
        self._left_enabled = False
        self._right_enabled = False
        self._left_actual: List[float] = []
        self._right_actual: List[float] = []
        self._left_anchor: List[float] = []
        self._right_anchor: List[float] = []
        self._left_latest: List[float] = []
        self._right_latest: List[float] = []

        self._streaming = False
        self._playback_active = False
        self._lead_in_active = False
        self._lead_in_start = 0.0
        self._lead_in_from: Tuple[List[float], List[float]] = ([], [])
        self._lead_in_to: Tuple[List[float], List[float]] = ([], [])
        #: 入控/播放过程中是否已出错。出错后必须停止推进, 否则会掩盖真实原因。
        self._failed = False

    # ------------------------------------------------------------ 状态

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def streaming(self) -> bool:
        return self._streaming

    @property
    def playback_active(self) -> bool:
        """起点对齐是否已完成。

        streaming 为真只代表通道已建立, 此时机器人可能还在从实测角平滑靠向
        轨迹起点, 过渡期收到的帧会被丢弃。只有本项为真时才可以推进时间轴。
        """
        return self._playback_active

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def left_arm_name(self) -> str:
        return self._left_name

    @property
    def right_arm_name(self) -> str:
        return self._right_name

    def set_arm_names(self, left: str, right: str) -> None:
        with self._lock:
            self._left_name = left or ""
            self._right_name = right or ""

    def set_actual_joints(self, arm_name: str, joints: List[float]) -> None:
        """收到实测关节角。

        arm_name 可能是逻辑名 (left/right), 也可能是控制器里的真实机器人名。
        """
        if not arm_name or len(joints) < JOINT_COUNT:
            return

        key = arm_name.lower()
        is_left = key == "left"
        is_right = key == "right"
        if not is_left and not is_right:
            with self._lock:
                if self._left_name and arm_name == self._left_name:
                    is_left = True
                elif self._right_name and arm_name == self._right_name:
                    is_right = True
        if not is_left and not is_right:
            return

        with self._lock:
            if is_left:
                self._left_actual = list(joints[:JOINT_COUNT])
                # 起流前锚点要持续跟着实测走: 首包基准越贴近当前姿态越安全
                if self._streaming and not self._playback_active and not self._lead_in_active:
                    self._left_anchor = list(self._left_actual)
                    self._left_latest = list(self._left_actual)
                    self._streamer.set_target(self._left_name, self._left_latest)
            else:
                self._right_actual = list(joints[:JOINT_COUNT])
                if self._streaming and not self._playback_active and not self._lead_in_active:
                    self._right_anchor = list(self._right_actual)
                    self._right_latest = list(self._right_actual)
                    self._streamer.set_target(self._right_name, self._right_latest)

    # ------------------------------------------------------------ 阶段

    def _set_phase(self, phase: str, detail: str = "") -> None:
        with self._lock:
            if self._phase == phase:
                return
            self._phase = phase
        logger.info("[控制] 阶段 -> %s %s", phase, detail)
        if self._on_phase is not None:
            self._on_phase(phase, detail)

    def _fail(self, message: str) -> None:
        with self._lock:
            self._last_error = message
            self._failed = True
        logger.error("[控制] %s", message)
        if self._on_error is not None:
            self._on_error(message)
        self._set_phase(Phase.ERROR, message)

    @property
    def failed(self) -> bool:
        """入控/播放过程中是否已出错。"""
        with self._lock:
            return self._failed

    # ------------------------------------------------------------ 入控

    def active_arms(self) -> List[str]:
        with self._lock:
            arms = []
            if self._left_name:
                arms.append(self._left_name)
            if self._right_name:
                arms.append(self._right_name)
            return arms

    def has_actual_joints(self) -> bool:
        with self._lock:
            if self._left_name and len(self._left_actual) < JOINT_COUNT:
                return False
            if self._right_name and len(self._right_actual) < JOINT_COUNT:
                return False
        return True

    def request_actual_joints(self) -> None:
        """主动拉一次状态, 补齐实测关节角。"""
        for name in self.active_arms():
            self._conn.get_robot_status(name)

    # ------------------------------------------------------------ 使能

    def set_arm_enabled(self, name: str, enabled: bool) -> None:
        """记录某条臂的使能状态。"""
        with self._lock:
            if name == self._left_name:
                self._left_enabled = enabled
            elif name == self._right_name:
                self._right_enabled = enabled

    def disabled_arms(self) -> List[str]:
        """当前未上使能的臂。

        未上使能时入控会被控制器拒绝 (24077 「机器人 X 未上使能, 不能入控」),
        所以播放前要把它们使能上。
        """
        with self._lock:
            out = []
            if self._left_name and not self._left_enabled:
                out.append(self._left_name)
            if self._right_name and not self._right_enabled:
                out.append(self._right_name)
            return out

    def enable_arms(self) -> List[str]:
        """给未上使能的臂发 robot/enable。返回发了使能的臂名。"""
        arms = self.disabled_arms()
        for name in arms:
            self._conn.enable_robot(name)
        if arms:
            logger.info("[控制] 请求上使能: %s", ", ".join(arms))
        return arms

    def _control_config(self) -> Dict[str, Any]:
        """构造 realtime/startControl 的 config 段。

        会话参数在入控时冻结, 运行期改参数必须 stopControl 后重新 startControl。
        """
        return {
            "cartesianIkPath": "none",   # 关节流
            "timeMode": "none",
            "expectedFrequency": self._streamer.frequency,
            "unitType": "si",
            "interpolator": "linear",
            "postFilter": {"type": "lowPass", "cutoffFrequency": 2},
            # 停发保持会话: 中途卡顿不掉控制权, 收尾由 stopControl 显式交还。
            # 这也是 streamer 能在静止后停止下发的依据 —— 停发不会掉线,
            # 会话保持 active、通道仍在直达侧, 目标变化时再发包即继续跟随。
            "holdOnIdle": True,
            # 关节限幅是本会话唯一的关节级保护, 必须显式打开。
            # 协议缺省是 false (即无任何关节级限速)。
            "jointLimit": dict(self._joint_limit),
        }

    def enter_control(self) -> None:
        """入控: 起流 + 并行发 startControl。

        调用方需要保证实测关节角已经就绪 (has_actual_joints)。
        """
        arms = self.active_arms()
        if not arms:
            raise ControllerError("尚未识别到机器人, 请先连接并确认左右臂")

        if not self._conn.connected:
            raise ControllerError("未连接到机器人, 无法播放")

        with self._lock:
            if self._streaming:
                return
            self._left_anchor = list(self._left_actual)
            self._right_anchor = list(self._right_actual)
            self._left_latest = list(self._left_actual)
            self._right_latest = list(self._right_actual)
            # 每次入控前清掉上一轮的失败标记
            self._failed = False

        self._set_phase(Phase.ENTERING, "正在进入实时控制")

        # ---- 起流: 必须在发 startControl 之前 ----
        #
        # 协议要求并行发首包。发送线程先跑起来, 把实测锚点推过去,
        # 这样 startControl 的响应还没回来时, 首包已经在路上了。
        host = self._conn.host or "127.0.0.1"
        self._streamer.set_host(host)
        if self._left_name:
            self._streamer.set_target(self._left_name, self._left_latest)
        if self._right_name:
            self._streamer.set_target(self._right_name, self._right_latest)
        self._streamer.start()

        with self._lock:
            self._streaming = True
            self._playback_active = False
            self._lead_in_active = False

        # ---- 并行发 startControl ----
        #
        # 必须显式带上 robot: 控制器上有多台机器人时, 省略 config.robot
        # 会被直接拒绝 (24050 「存在多台机器人, 必须指定 robot」)。
        cmd_id = self._conn.start_control(self._control_config(), arms)
        if cmd_id < 0:
            self._streamer.stop()
            with self._lock:
                self._streaming = False
            raise ControllerError("发送 realtime/startControl 失败")

        logger.info("[控制] 已发 startControl (id=%d), 关节流并行下发中", cmd_id)

    # ------------------------------------------------------------ 帧

    def push_frame(self, state: JointState) -> None:
        """播放引擎送来的一帧目标关节角。"""
        with self._lock:
            if not self._streaming:
                return

            left = list(state.left[:JOINT_COUNT])
            right = list(state.right[:JOINT_COUNT])

            if not self._playback_active:
                if self._lead_in_active:
                    return  # 过渡中由对齐定时器推进
                # 收到轨迹起点: 从实测角平滑过渡过去。
                # 这是绕开 firstFrameJumpRejected 的关键 —— 首包必须贴近实测。
                if self._left_name:
                    self._lead_in_from = (list(self._left_anchor), list(self._right_anchor))
                else:
                    self._lead_in_from = ([], [])
                self._lead_in_to = (left, right)
                self._lead_in_start = time.monotonic()
                self._lead_in_active = True
                self._set_phase(Phase.ALIGNING, "正在平滑对齐轨迹起点")
                return

            # 正常播放: 更新目标即可, 发送线程按自己的频率发最新值
            if self._left_name:
                self._left_latest = left
                self._streamer.set_target(self._left_name, left)
            if self._right_name:
                self._right_latest = right
                self._streamer.set_target(self._right_name, right)

    def advance_lead_in(self) -> bool:
        """推进对齐过渡。返回 True 表示对齐已完成。

        对齐期间播放引擎的帧会被丢弃 (push_frame 里直接 return),
        所以时间轴必须在收到本信号后才真正开始推进 —— 否则过渡结束时
        轨迹已经跑到 700ms 之后, 机器人会一步跳到那个位置。
        """
        with self._lock:
            # 入控失败 (如 24050) 时不得再推进对齐, 否则会把失败掩盖成
            # 「已对齐起点, 开始播放」, 用户看到的现象是「没动也不报错」。
            if self._failed:
                self._lead_in_active = False
                return False

            if not self._lead_in_active or not self._streaming:
                self._lead_in_active = False
                return False

            u = (time.monotonic() - self._lead_in_start) * 1000.0 / max(1.0, self._lead_in_ms)
            u = min(1.0, max(0.0, u))
            s = u * u * (3.0 - 2.0 * u)   # S 曲线, 起止速度为 0

            from_left, from_right = self._lead_in_from
            to_left, to_right = self._lead_in_to

            if self._left_name and from_left and to_left:
                self._left_latest = _blend(from_left, to_left, s)
                self._streamer.set_target(self._left_name, self._left_latest)
            if self._right_name and from_right and to_right:
                self._right_latest = _blend(from_right, to_right, s)
                self._streamer.set_target(self._right_name, self._right_latest)

            if u >= 1.0:
                self._lead_in_active = False
                self._playback_active = True
                self._set_phase(Phase.PLAYING, "已对齐起点, 开始播放")
                return True
        return False

    # ------------------------------------------------------------ 停控

    def stop_control(self) -> None:
        """停控: 停流 + stopControl 交还通道。"""
        self._set_phase(Phase.STOPPING, "正在退出实时控制")

        self._streamer.stop()

        with self._lock:
            was_active = self._streaming
            self._streaming = False
            self._playback_active = False
            self._lead_in_active = False

        if not was_active:
            self._set_phase(Phase.IDLE)
            return

        if self._conn.connected:
            self._conn.stop_control(self.active_arms())
            logger.info("[控制] 已发 realtime/stopControl")
        self._set_phase(Phase.IDLE)

    def shutdown(self) -> None:
        """退出前必须交还通道, 否则会话会残留在控制器上占住该臂。"""
        try:
            self.stop_control()
        except Exception as exc:  # pragma: no cover - 退出路径尽力而为
            logger.warning("[控制] 退出时停控失败: %s", exc)
            self._streamer.stop()
