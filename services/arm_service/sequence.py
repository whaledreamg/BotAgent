"""动作序列合成 —— 把「关节片段」和「夹爪片段」拼成**一条**时间轴。

**为什么要有这一层**:

抓取流程天然是**两段式**的:

1. **先识别** —— 拍照, 算出目标在哪 (这一步必须先做完, 后面才知道
   要往哪走);
2. **再执行** —— 回准备点 -> 移到上方点 -> 移到抓取点 -> 闭手 -> 回安全点。

但「执行」这一步里的动作是**混合**的:

- 关节片段 (回准备点、移到上方点) 走 UDP 关节流;
- 夹爪片段 (闭手) 走控制器 ``rs485/*`` 透传 (Modbus RTU)。

**这两条通道没法塞进同一个 UDP 包** —— 实时指令包里根本没有手部字段
(控制器文档 §21.9: 字段只有 ``robot`` / ``controlSpace`` /
``commandMode`` / ``poseType`` / ``value``)。多塞字段会被整包判
``invalidDatagram``, 那时候连手臂都不动。

所以「合成」的含义是:

- **关节片段** -> 拼进一条时间轴, 一次入控, 连续播放 (省掉每次重新
  入控的 700ms S 曲线对齐);
- **夹爪片段** -> 记下它在时间轴上的**时间点**, 播放时到点了再单独下发。

这样既保住了「关节运动连贯 + 只入控一次」, 又能让夹爪在正确的时刻
开合。语义与 ActionPlanner 的 ``segmentEntered`` 一致: 片段进入时
下发一次, 到位即结束。

**时间轴合成**的关键是**时间平移**: 每个动作的片段时间都是从 0 开始
的, 直接拼起来会全部重叠。合成时给每个动作加一个时间偏移, 让它们
首尾相接。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from timeline.model import JOINT_COUNT, JointState, Segment, TimelineModel

logger = logging.getLogger("arm.sequence")


@dataclass
class HandCue:
    """一个夹爪指令的「时间点」。

    Attributes:
        time:       在合成时间轴上的触发时刻 (秒)
        left:       左手目标 (6 个 0~1000); None = 不动左手
        right:      右手目标 (6 个 0~1000); None = 不动右手
        label:      来源片段的标签 (追溯用)
        source:     来源动作名 (追溯用)
    """

    time: float
    left: Optional[List[int]] = None
    right: Optional[List[int]] = None
    label: str = ""
    source: str = ""


@dataclass
class ComposedSequence:
    """合成出来的动作序列。

    Attributes:
        timeline: 关节轨迹 (一条时间轴, 一次入控播完)
        cues:     夹爪指令的时间点 (按时间排序)
        duration: 总时长 (秒)
        sources:  参与合成的动作名 (按顺序) —— 追溯用
        steps:    每个来源动作的明细 (起止时间、片段数)
    """

    timeline: TimelineModel
    cues: List[HandCue] = field(default_factory=list)
    duration: float = 0.0
    sources: List[str] = field(default_factory=list)
    steps: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转成可以 JSON 化的字典 (给 HTTP 接口用)。"""
        return {
            "duration": round(self.duration, 3),
            "sources": list(self.sources),
            "steps": list(self.steps),
            "segments": self.timeline.segment_count,
            "hand_cues": [
                {
                    "time": round(c.time, 3),
                    "label": c.label,
                    "source": c.source,
                    "left": list(c.left) if c.left is not None else None,
                    "right": list(c.right) if c.right is not None else None,
                }
                for c in self.cues
            ],
        }


def compose(
    units: Sequence[Any],
    origin_pose: Optional[JointState] = None,
    *,
    gap: float = 0.0,
) -> ComposedSequence:
    """把几个动作拼成一条时间轴。

    Args:
        units:      动作列表 (``ActionUnit``), 按执行顺序
        origin_pose: 出发姿态 (第一条关节片段的起点)
        gap:        相邻两个动作之间的停顿 (秒)

    Returns:
        :class:`ComposedSequence`

    Raises:
        ValueError: 动作列表为空
    """
    if not units:
        raise ValueError("没有可合成的动作")

    segments: List[Segment] = []
    cues: List[HandCue] = []
    sources: List[str] = []
    steps: List[Dict[str, Any]] = []

    #: 当前时间轴已经走到哪了
    cursor = 0.0

    for unit in units:
        unit_segments = list(unit.segments)
        if not unit_segments:
            logger.warning("[序列] 动作 %r 没有片段, 跳过", unit.label)
            continue

        # 每个动作的片段时间从 0 开始 —— 平移到 cursor
        offset = cursor - float(unit.origin_time)
        action_start = cursor
        arm_count = 0
        hand_count = 0

        for seg in unit_segments:
            start = float(seg.start_time) + offset
            duration = float(seg.duration)
            new_seg = Segment(
                type=seg.type,
                label=seg.label,
                start_time=start,
                duration=duration,
                end_joints=seg.end_joints.copy(),
                interpolation=seg.interpolation,
                left_hand=list(seg.left_hand),
                right_hand=list(seg.right_hand),
                hand_sides=list(seg.hand_sides),
            )
            segments.append(new_seg)

            if new_seg.is_arm:
                arm_count += 1
            else:
                hand_count += 1
                # 夹爪片段 -> 一个时间点上的指令
                left = new_seg.hand_for("left")
                right = new_seg.hand_for("right")
                if left is not None or right is not None:
                    cues.append(
                        HandCue(
                            time=start,
                            left=left,
                            right=right,
                            label=new_seg.label,
                            source=unit.label,
                        )
                    )

        action_end = max(
            float(s.start_time) + float(s.duration) + offset
            for s in unit_segments
        )
        cursor = action_end + gap
        sources.append(unit.label)
        steps.append(
            {
                "action": unit.label,
                "start": round(action_start, 3),
                "end": round(action_end, 3),
                "arm_segments": arm_count,
                "hand_segments": hand_count,
            }
        )

    model = TimelineModel(segments)
    if origin_pose is not None:
        model.set_origin_pose(origin_pose.copy())

    cues.sort(key=lambda c: c.time)
    logger.info(
        "[序列] 合成 %d 个动作 -> %d 段 (关节 %d / 夹爪 %d), 时长 %.2fs, "
        "夹爪指令 %d 个",
        len(sources), len(segments),
        sum(1 for s in segments if s.is_arm),
        sum(1 for s in segments if not s.is_arm),
        model.total_duration, len(cues),
    )
    return ComposedSequence(
        timeline=model,
        cues=cues,
        duration=model.total_duration,
        sources=sources,
        steps=steps,
    )


def compose_from_names(
    library: Any,
    names: Sequence[str],
    origin_pose: Optional[JointState] = None,
    *,
    gap: float = 0.0,
) -> ComposedSequence:
    """按动作名合成 (从动作库里取)。

    Raises:
        LookupError: 有动作名在库里找不到
    """
    units = []
    for name in names:
        unit = library.get(str(name).strip())
        if unit is None:
            raise LookupError(f"动作 {name!r} 不存在")
        units.append(unit)
    return compose(units, origin_pose, gap=gap)


# ======================================================================
# 流程 -> 序列
# ======================================================================

@dataclass
class PlanStep:
    """流程里的一个片段, 已经解析成「要做什么」。

    Attributes:
        id:       片段 id
        type:     片段类型 ("arm" / "hand" / "detect" / "target")
        label:    片段说法
        action:   要播的动作名 ("arm" / "hand")
        pose:     显式位姿 ("arm": 关节角; "hand": 6 个 0~1000)
        preset:   手势名 ("hand")
        direction: 夹爪方向 ("hand") —— close / open
        point:    引用的具名点 id ("arm")
        source:   取值来源 ("target") —— detect
        base:     基准具名点 id ("target")
        offset:   偏移 (米) ("arm" / "target")
        pos:      目标位置 (世界系) —— 解算出来的 ("arm" 引用具名点时)
        duration: 片段时长 (秒)
        orientation: 拍照姿态 (各臂 a/b/c) —— 来自 ``detect`` 片段。
            它规定了「拍照时手是什么姿态」, 也就等于**从那个姿态出发
            去抓**时的抓取姿态 (两者必须一致, 否则目标姿态不可达)。
    """

    id: str
    type: str
    label: str = ""
    action: str = ""
    pose: Any = None
    preset: str = ""
    direction: str = ""
    point: str = ""
    source: str = ""
    base: str = ""
    offset: Dict[str, float] = field(default_factory=dict)
    pos: Optional[tuple] = None
    duration: Optional[float] = None
    orientation: Dict[str, Dict[str, float]] = field(default_factory=dict)


def resolve_named_points(
    flow: Any,
    target_pos: Optional[Sequence[float]] = None,
    *,
    approach_distance: float = 0.10,
) -> Dict[str, Tuple[float, float, float]]:
    """解算流程里声明的**全部具名点**。

    具名点的值有三种来源:

    - ``source: detect`` -> 识别结果的位置
    - ``base: <id>`` + ``offset`` -> 基准点 + 偏移
    - ``pose: {x,y,z}`` -> 写死的坐标

    **按流程顺序解算** —— ``base`` 指向的点必须先算出来 (加载时已校验过
    顺序, 这里再解一遍)。

    Args:
        flow:             :class:`arm.flow.FlowDef`
        target_pos:       识别出的物体位置 (世界系, 米)
        approach_distance: 上方点的高度 (米) —— 给 ``distance: approach``
            的具名点用

    Returns:
        点名 -> (x, y, z)

    Raises:
        ValueError: 缺识别结果, 或引用了不存在的点
    """
    points = flow.named_points()
    resolved: Dict[str, Tuple[float, float, float]] = {}

    for point_id, point in points.items():
        if point.source == "detect":
            if target_pos is None:
                raise ValueError(
                    f"具名点 {point_id!r} 取自识别结果, 但还没有识别出的目标位置"
                )
            base = (
                float(target_pos[0]),
                float(target_pos[1]),
                float(target_pos[2]),
            )
        elif point.base:
            if point.base not in resolved:
                raise ValueError(
                    f"具名点 {point_id!r} 基于 {point.base!r}, "
                    f"但那个点还没解算出来"
                )
            base = resolved[point.base]
        elif point.pose is not None:
            base = (
                float(point.pose.get("x", 0.0)),
                float(point.pose.get("y", 0.0)),
                float(point.pose.get("z", 0.0)),
            )
        else:
            raise ValueError(f"具名点 {point_id!r} 没有可用的取值方式")

        off = point.offset or {}
        # ``distance: approach`` —— 高度取流程级的 approach_distance
        # (这样「上方点的高度」是流程的一个设置, 调用参数可以覆盖它)
        dz = float(off.get("z", 0.0))
        if point.distance == "approach":
            dz += float(approach_distance)
        resolved[point_id] = (
            base[0] + float(off.get("x", 0.0)),
            base[1] + float(off.get("y", 0.0)),
            base[2] + dz,
        )
    return resolved


def plan_from_flow(
    flow: Any,
    target_pos: Optional[Sequence[float]] = None,
    *,
    approach_distance: float = 0.10,
    duration: Optional[float] = None,
    defaults: Optional[Dict[str, Any]] = None,
) -> List[PlanStep]:
    """把流程定义解析成「片段清单」。

    **识别结果在这一步之前就已经拿到了** (``target_pos``) —— 所以
    「先识别, 再合成序列」的自然顺序在这里体现为: 解析时就能算出每个
    具名点的值。

    Args:
        flow:              :class:`arm.flow.FlowDef`
        target_pos:        识别出的物体位置 (世界系, 米)
        approach_distance: 上方点的高度 (米) —— 兜底值
        duration:          统一覆盖所有移动片段的时长
        defaults:          服务端兜底值 (close_action / open_action)

    Returns:
        :class:`PlanStep` 列表
    """
    defaults = defaults or {}
    steps: List[PlanStep] = []
    named = resolve_named_points(
        flow, target_pos, approach_distance=approach_distance
    )

    for step in flow.steps:
        stype = step.type
        label = step.label or step.id

        if stype == "arm":
            plan_step = PlanStep(
                id=step.id, type="arm", label=label,
                action=step.action,
                pose=step.pose,
                point=step.point,
                base=step.base,
                offset=step.offset,
            )
            # 终点来自具名点 -> 现在就把坐标算出来
            if step.point:
                if step.point not in named:
                    raise ValueError(
                        f"步骤 {step.id!r} 引用了不存在的具名点 {step.point!r}"
                    )
                pos = named[step.point]
                off = step.offset
                plan_step.pos = (
                    pos[0] + off.get("x", 0.0),
                    pos[1] + off.get("y", 0.0),
                    pos[2] + off.get("z", 0.0),
                )
            elif step.base:
                if step.base not in named:
                    raise ValueError(
                        f"步骤 {step.id!r} 引用了不存在的具名点 {step.base!r}"
                    )
                pos = named[step.base]
                off = step.offset
                plan_step.pos = (
                    pos[0] + off.get("x", 0.0),
                    pos[1] + off.get("y", 0.0),
                    pos[2] + off.get("z", 0.0),
                )
            plan_step.duration = duration if duration is not None else step.duration
            steps.append(plan_step)

        elif stype == "hand":
            direction = step.direction
            action = step.action
            if not action and not step.preset and direction:
                # 纯方向 -> 从配置里取对应的动作名
                action = str(defaults.get(f"{direction}_action") or "")
            steps.append(
                PlanStep(
                    id=step.id, type="hand", label=label,
                    action=action,
                    pose=step.pose,
                    preset=step.preset,
                    direction=direction,
                    duration=step.duration,
                )
            )

        elif stype == "detect":
            # 识别片段在时间轴上占一段「保持不动」的时长
            steps.append(
                PlanStep(
                    id=step.id, type="hold", label=label,
                    duration=step.duration,
                    orientation=step.orientation,
                )
            )

        elif stype == "target":
            # 具名点声明 —— **纯声明**, 不产生运动
            steps.append(
                PlanStep(
                    id=step.id, type="target", label=label,
                    source=step.source,
                    base=step.base,
                    offset=step.offset,
                )
            )

    return steps


#: 「开/合」的手位 —— 由 service 注入 (配置里的 open_pose / close_pose)
_POSE_TABLE: Dict[str, List[int]] = {}


def set_preset_poses(open_pose: Sequence[int], close_pose: Sequence[int]) -> None:
    """注入开/合手位 (供 :func:`build_segments_for_plan` 用)。"""
    _POSE_TABLE["open"] = [int(v) for v in open_pose]
    _POSE_TABLE["close"] = [int(v) for v in close_pose]


def _pose_for(step: PlanStep) -> Optional[List[int]]:
    """取这一步该用的手位。

    优先用片段里写死的手位 (``pose``), 否则按方向取配置的默认手位。
    """
    if isinstance(step.pose, (list, tuple)):
        return [int(v) for v in step.pose]
    if step.direction:
        return _POSE_TABLE.get(step.direction)
    return None


__all__ = [
    "ComposedSequence",
    "HandCue",
    "PlanStep",
    "compose",
    "compose_from_names",
    "plan_from_flow",
    "resolve_named_points",
    "set_preset_poses",
]
