"""时间轴数据模型。

移植自 ActionPlanner 的 src/timeline/TimelineModel.h, 去掉 Qt 依赖。

核心概念:
- Segment: 一个动作片段。机械臂片段只记录"结束关节角", 片段首尾相连,
  本段的起点就是时间上紧邻的上一段的终点。这样改前一段终点时后一段起点自动跟着变。
- TimelineModel: 一串片段 + 出发姿态(origin pose)。

出发姿态不落盘: 每次播放都从机器人的当前实际位置出发, 避免轨迹把机器人
拽到某个固定姿态上去。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

#: 七轴关节数量
JOINT_COUNT = 7


@dataclass
class JointState:
    """双臂关节角 (弧度)。"""

    left: List[float] = field(default_factory=lambda: [0.0] * JOINT_COUNT)
    right: List[float] = field(default_factory=lambda: [0.0] * JOINT_COUNT)

    def copy(self) -> "JointState":
        return JointState(left=list(self.left), right=list(self.right))

    @staticmethod
    def zero() -> "JointState":
        return JointState()

    def is_finite(self) -> bool:
        """所有关节角是否为有限数 (NaN/Inf 会被控制器整包丢弃)。"""
        import math

        return all(
            math.isfinite(v) for v in (self.left + self.right)
        )


class SegmentType:
    ARM = "arm"
    HAND = "hand"


@dataclass
class Segment:
    """一个动作片段。"""

    type: str = SegmentType.ARM
    label: str = ""
    start_time: float = 0.0
    duration: float = 1.0
    #: 仅 ARM 片段有意义: 该片段结束时的双臂关节角
    end_joints: JointState = field(default_factory=JointState)
    #: "linear" | "cubic" | "spline"
    interpolation: str = "cubic"
    #: 仅 HAND 片段有意义: 灵巧手 6 自由度目标 (0~1000)
    left_hand: List[int] = field(default_factory=lambda: [0] * 6)
    right_hand: List[int] = field(default_factory=lambda: [0] * 6)

    @property
    def end_time(self) -> float:
        return self.start_time + self.duration

    @property
    def is_arm(self) -> bool:
        return self.type == SegmentType.ARM


class TimelineModel:
    """一串片段构成的轨迹。"""

    def __init__(self, segments: Optional[List[Segment]] = None) -> None:
        self._segments: List[Segment] = list(segments or [])
        self._origin_pose = JointState.zero()
        self._total_duration = 0.0
        self._recompute_duration()

    # ------------------------------------------------------------ 基本访问

    @property
    def segments(self) -> List[Segment]:
        return self._segments

    @property
    def segment_count(self) -> int:
        return len(self._segments)

    def get_segment(self, index: int) -> Segment:
        return self._segments[index]

    @property
    def total_duration(self) -> float:
        return self._total_duration

    @property
    def origin_pose(self) -> JointState:
        return self._origin_pose

    def set_origin_pose(self, pose: JointState) -> None:
        self._origin_pose = pose

    def add_segment(self, segment: Segment) -> None:
        self._segments.append(segment)
        self._recompute_duration()

    def clear(self) -> None:
        self._segments.clear()
        self._recompute_duration()

    def _recompute_duration(self) -> None:
        self._total_duration = 0.0
        for seg in self._segments:
            self._total_duration = max(self._total_duration, seg.end_time)

    # ------------------------------------------------------------ 前驱推导

    def arm_indices_by_time(self) -> List[int]:
        """按时间轴位置排序的机械臂片段下标。

        「上一段」指的是时间上紧邻的那一段, 不是数组里的前一项 —— 片段可以被
        拖到任意位置。排序规则必须与插值引擎完全一致, 否则两处对同一个片段
        会取到不同的前驱, 求值和关键帧就对不上了。

        稳定排序: 起始时间相同的保持原有先后, 行为可预期。
        """
        order = [i for i, s in enumerate(self._segments) if s.is_arm]
        order.sort(key=lambda i: self._segments[i].start_time)
        return order

    def previous_arm_end(self, index: int) -> JointState:
        """该片段之前(不含自己)最后一个机械臂片段的结束角。"""
        order = self.arm_indices_by_time()
        if index < 0 or index >= len(self._segments):
            return self._origin_pose
        if index not in order:
            # 自身是夹爪片段, 不产生关节起点
            return self._origin_pose
        pos = order.index(index)
        if pos <= 0:
            return self._origin_pose
        return self._segments[order[pos - 1]].end_joints

    def segment_start_joints(self, index: int) -> JointState:
        return self.previous_arm_end(index)

    def segment_index_at(self, time: float) -> int:
        """该时刻落在哪个片段, 没有返回 -1。"""
        for i, seg in enumerate(self._segments):
            if seg.start_time <= time < seg.end_time:
                return i
        return -1
