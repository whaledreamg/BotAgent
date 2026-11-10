"""时间轴播放引擎。

移植自 ActionPlanner 的 src/timeline/PlaybackEngine.cpp, 去掉 Qt 依赖。

职责: 只做「时间推进 + 插值求值」, 每帧产出一个目标关节角。
调用方 (controller) 决定怎么把它送到机器人。

关键点:
- 起点冻结 (prime_start_frame): 整条轨迹的插值基准必须是播放那一刻的实测姿态,
  后续实测角再变化也不会影响已经算好的轨迹。
- 时间轴必须在对齐完成之后才推进 (start), 否则机器人会在对齐结束时
  一步跳到 700ms 后的位置。
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

from timeline.interpolation import (
    build_spline_keyframes,
    cubic_spline_interpolate,
    linear_interpolate,
    smooth_step_interpolate,
)
from timeline.model import JOINT_COUNT, JointState, TimelineModel


class PlaybackState:
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"


class PlaybackEngine:
    """按真实时间推进的时间轴。"""

    def __init__(
        self,
        model: TimelineModel,
        on_frame: Optional[Callable[[float, JointState], None]] = None,
        on_finished: Optional[Callable[[], None]] = None,
        on_segment: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._model = model
        self._on_frame = on_frame
        self._on_finished = on_finished
        self._on_segment = on_segment

        self._state = PlaybackState.IDLE
        self._accumulated_ms = 0.0
        self._last_emitted_time = -1.0
        self._current_segment = -1
        self._last_tick = time.monotonic()

        self._origin_snapshot = JointState.zero()
        self._origin_frozen = False
        self._primed = False

        self._spline_keys: List[JointState] = []
        self._spline_times: List[float] = []

    # ------------------------------------------------------------ 状态

    @property
    def state(self) -> str:
        return self._state

    def is_active(self) -> bool:
        return self._state != PlaybackState.IDLE

    def is_playing(self) -> bool:
        return self._state == PlaybackState.PLAYING

    def is_paused(self) -> bool:
        return self._state == PlaybackState.PAUSED

    @property
    def current_time(self) -> float:
        return self._accumulated_ms / 1000.0

    @property
    def total_duration(self) -> float:
        return self._model.total_duration

    def set_model(self, model: TimelineModel) -> None:
        self._model = model
        self._build_keyframes()

    # ------------------------------------------------------------ 控制

    def prime_start_frame(self) -> JointState:
        """冻结起点: 发出时间轴 0 时刻的那一帧, 但不推进时间轴。

        真机入控时需要先拿到轨迹起点, 才能用约 700ms 把机器人从实测角平滑
        过渡过去; 过渡期间收到的帧都会被丢弃。所以这里只算不发。

        返回冻结后的起点姿态。
        """
        if self._model.segment_count == 0:
            return JointState.zero()

        self._origin_snapshot = self._model.origin_pose.copy()
        self._origin_frozen = True
        self._build_keyframes()

        self._accumulated_ms = 0.0
        self._last_emitted_time = -1.0
        self._current_segment = -1
        self._primed = True
        return self._origin_snapshot.copy()

    def start(self) -> None:
        """开始推进时间轴。

        已经有预先对齐好的快照 (prime_start_frame) 就沿用它 —— 重新取一次实时角
        会把对齐结果覆盖掉, 那 700ms 里机器人已经被带到轨迹起点了, 再拿新实测值
        当基准会把轨迹整体平移, 产生跳变。
        """
        if self._model.segment_count == 0:
            return

        if not self._primed:
            self._origin_snapshot = self._model.origin_pose.copy()
        self._origin_frozen = True
        self._primed = False

        self._build_keyframes()

        self._state = PlaybackState.PLAYING
        self._accumulated_ms = 0.0
        self._last_emitted_time = -1.0
        self._current_segment = -1
        self._last_tick = time.monotonic()

        # 立即发出第 0 帧, 避免首帧延迟
        self._emit_frame_at(0.0)

    def pause(self) -> None:
        if self._state != PlaybackState.PLAYING:
            return
        self._state = PlaybackState.PAUSED

    def resume(self) -> None:
        if self._state != PlaybackState.PAUSED:
            return
        self._last_tick = time.monotonic()
        self._state = PlaybackState.PLAYING

    def stop(self) -> None:
        was_active = self._state != PlaybackState.IDLE
        self._state = PlaybackState.IDLE
        self._accumulated_ms = 0.0
        self._last_emitted_time = -1.0
        self._current_segment = -1
        self._origin_frozen = False
        self._primed = False
        if was_active and self._on_finished is not None:
            self._on_finished()

    # ------------------------------------------------------------ 推进

    def tick(self) -> bool:
        """由外部定时器驱动, 推进时间轴。

        返回 True 表示本次 tick 播放结束了。
        """
        if self._state != PlaybackState.PLAYING:
            return False

        total = self.total_duration
        if total <= 0.0:
            self.stop()
            return True

        # 按真实经过时间推进, 而不是假定每帧正好 16ms,
        # 这样即使某一帧卡住, 播放进度也不会落后。
        now = time.monotonic()
        delta_ms = (now - self._last_tick) * 1000.0
        self._last_tick = now
        self._accumulated_ms += delta_ms

        t = self.current_time
        if t >= total:
            self._emit_frame_at(total)
            self._state = PlaybackState.IDLE
            self._accumulated_ms = total * 1000.0
            self._last_emitted_time = -1.0
            self._origin_frozen = False
            self._primed = False
            if self._on_finished is not None:
                self._on_finished()
            return True

        self._emit_frame_at(t)
        return False

    def _emit_frame_at(self, t: float) -> None:
        # 同一时刻不重复发, 省掉无意义的 UDP 包
        if abs(t - self._last_emitted_time) < 1e-9:
            return
        self._last_emitted_time = t

        idx = self._model.segment_index_at(t)
        if idx != self._current_segment:
            self._current_segment = idx
            if self._on_segment is not None:
                self._on_segment(idx)

        if self._on_frame is not None:
            self._on_frame(t, self.evaluate_at(t))

    # ------------------------------------------------------------ 求值

    def _build_keyframes(self) -> None:
        self._spline_keys = []
        self._spline_times = []

        # 只有 spline 的机械臂片段才需要全局关键帧; 其余情况走逐段插值。
        # 夹爪片段不参与关节插值, 混进来会让 Catmull-Rom 把手臂拽向零位。
        needs_spline = any(
            s.is_arm and s.interpolation == "spline" for s in self._model.segments
        )
        if not needs_spline:
            return

        order = self._model.arm_indices_by_time()
        self._spline_keys, self._spline_times = build_spline_keyframes(
            order, self._model.segments, self.origin_pose
        )

    def origin_pose(self) -> JointState:
        if self._origin_frozen:
            return self._origin_snapshot.copy()
        return self._model.origin_pose.copy()

    def evaluate_at(self, t: float) -> JointState:
        """求某个时刻的双臂关节角。"""
        if self._model.segment_count == 0:
            return JointState.zero()
        if t <= 0.0:
            return self.origin_pose()

        # 夹爪片段不需要单独处理: 下面的循环只看机械臂片段。
        # 若此刻落在夹爪片段里 (没有机械臂片段覆盖), 循环会走到「空隙」分支,
        # 返回上一个机械臂片段的终点 —— 正是「保持不动」的语义。

        order = self._model.arm_indices_by_time()
        if not order:
            return self.origin_pose()

        for pos, idx in enumerate(order):
            seg = self._model.get_segment(idx)
            seg_end = seg.end_time
            # 本段的起点是「时间上位于它之前的机械臂片段的终点」,
            # 首段则用冻结的出发姿态, 播放中不会跟着实测角漂移
            seg_start = (
                self.origin_pose()
                if pos <= 0
                else self._model.get_segment(order[pos - 1]).end_joints.copy()
            )

            if seg.start_time <= t < seg_end:
                span = max(1e-6, seg.duration)
                local_t = (t - seg.start_time) / span

                if seg.interpolation == "linear":
                    return linear_interpolate(seg_start, seg.end_joints, local_t)
                if seg.interpolation == "spline":
                    # 用整条轨迹的关键帧求跨段连续的速度
                    if len(self._spline_keys) >= 2:
                        return cubic_spline_interpolate(
                            self._spline_keys, self._spline_times, t
                        )
                    return smooth_step_interpolate(seg_start, seg.end_joints, local_t)
                # 默认 cubic
                return smooth_step_interpolate(seg_start, seg.end_joints, local_t)

            # 落在两个机械臂片段之间的空隙 (可能夹着夹爪片段): 保持上一段的结束姿态。
            if t < seg.start_time:
                return seg_start

        # 超出时长: 停在时间上最后一个机械臂片段的结束姿态。
        return self._model.get_segment(order[-1]).end_joints.copy()
