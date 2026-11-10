"""关节角插值。

移植自 ActionPlanner 的 src/timeline/InterpolationEngine.cpp。

三种插值对应三种运动品质:
- linear: 匀速直线, 速度在片段边界突变 (会有抖动/冲击)
- cubic : 片段内 S 形加减速, 起止速度为零, 适合点位动作
- spline: 跨片段的三次样条, 关键帧处速度连续, 适合连续轨迹
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

from timeline.model import JOINT_COUNT, JointState


def _lerp(a: JointState, b: JointState, t: float) -> JointState:
    return JointState(
        left=[a.left[i] + t * (b.left[i] - a.left[i]) for i in range(JOINT_COUNT)],
        right=[a.right[i] + t * (b.right[i] - a.right[i]) for i in range(JOINT_COUNT)],
    )


def linear_interpolate(start: JointState, end: JointState, t: float) -> JointState:
    """线性插值。"""
    return _lerp(start, end, min(1.0, max(0.0, t)))


def smooth_step_interpolate(start: JointState, end: JointState, t: float) -> JointState:
    """片段内三次 S 曲线 (零起止速度)。

    归一化后做 3t²-2t³: 起止速度为 0, 中间加速再减速。
    相比线性插值, 点位动作不会在两端"顿一下"。
    """
    u = min(1.0, max(0.0, t))
    s = u * u * (3.0 - 2.0 * u)
    return _lerp(start, end, s)


def cubic_spline_interpolate(
    keyframes: Sequence[JointState],
    times: Sequence[float],
    t: float,
) -> JointState:
    """跨关键帧的样条插值。

    内部用按时间尺度的 Hermite 形式 (等价于非均匀 Catmull-Rom), 因此关键帧
    间隔不等时, 交界处速度依然连续。

    为什么不用教科书的均匀 Catmull-Rom:
    均匀版假定相邻控制点等间隔, 在 u=0 处的速度是 0.5*(p2-p0)/span。
    片段时长不一样时 (一段 1s、一段 3s), 同一个交界点两侧的 span 不同,
    算出的速度差好几倍 —— 本该连续的位置出现速度跳变, 手臂会"抽"一下。

    把切线直接定义成时间域的速度 (对时间求导), 再代入 Hermite 基函数,
    无论时长怎么分布, 交界处两侧速度都严格相等。
    """
    n = min(len(keyframes), len(times))
    if n == 0:
        return JointState.zero()
    if n == 1:
        return keyframes[0].copy()
    if t <= times[0]:
        return keyframes[0].copy()
    if t >= times[n - 1]:
        return keyframes[n - 1].copy()

    # 定位所在的时间区间
    i = 0
    while i < n - 2 and t > times[i + 1]:
        i += 1

    t0 = times[i]
    t1 = times[i + 1]
    span = t1 - t0
    if span <= 1e-9:
        return keyframes[i + 1].copy()
    u = (t - t0) / span

    p1 = keyframes[i]
    p2 = keyframes[i + 1]

    # 端点没有一侧邻居, 退化成单边差分, 仍是该区间内的正确速度
    dt_in1 = (times[i + 1] - times[i - 1]) if i > 0 else span
    dt_in2 = (times[i + 2] - times[i]) if (i + 2 < n) else span
    p_prev = keyframes[i - 1] if i > 0 else p1
    p_next = keyframes[i + 2] if (i + 2 < n) else p2

    # Hermite 基函数
    u2 = u * u
    u3 = u2 * u
    h00 = 2.0 * u3 - 3.0 * u2 + 1.0
    h10 = u3 - 2.0 * u2 + u
    h01 = -2.0 * u3 + 3.0 * u2
    h11 = u3 - u2

    left: List[float] = []
    right: List[float] = []
    for k in range(JOINT_COUNT):
        # 左右臂分别算: 切线是该关节角对时间的变化率 (rad/s)
        m1j = (p2.left[k] - p_prev.left[k]) / dt_in1
        m2j = (p_next.left[k] - p1.left[k]) / dt_in2
        left.append(h00 * p1.left[k] + h10 * span * m1j + h01 * p2.left[k] + h11 * span * m2j)

        m1r = (p2.right[k] - p_prev.right[k]) / dt_in1
        m2r = (p_next.right[k] - p1.right[k]) / dt_in2
        right.append(h00 * p1.right[k] + h10 * span * m1r + h01 * p2.right[k] + h11 * span * m2r)

    return JointState(left=left, right=right)


def build_spline_keyframes(
    order: Sequence[int],
    segments: Sequence,
    origin_pose: JointState,
) -> Tuple[List[JointState], List[float]]:
    """按时间轴顺序收集 spline 用的全局关键帧。

    - 本段起点取「时间上前一段的终点」, 首段用出发姿态。
    - 顺序反了会让 spline 的区间定位失效。
    - 首尾相连的两段会在交界处产生一对重合的关键帧, 必须去掉重复点:
      遇到重复点时交界处的切线会退化, 幅值偏小, 滑动到交界处能看出轻微顿挫。
    - 同一时刻但姿态不同 (重叠片段各自的目标冲突) 不能合并, 那是真实的不连续。
    """
    keys: List[Tuple[float, JointState]] = []

    for pos, idx in enumerate(order):
        seg = segments[idx]
        # 首段起点取出发姿态; 其余取时间上紧邻的上一段终点
        if pos <= 0:
            seg_start = origin_pose.copy()
        else:
            seg_start = segments[order[pos - 1]].end_joints.copy()
        keys.append((seg.start_time, seg_start))
        keys.append((seg.end_time, seg.end_joints.copy()))

    # 片段可以在时间轴上互相拖动重叠, 收集出来的顺序不一定按时间递增。
    # Catmull-Rom 的区间定位假设时间单调递增, 乱序会让它定位到错误的区间。
    keys.sort(key=lambda item: item[0])

    merged: List[Tuple[float, JointState]] = []
    for time, state in keys:
        if merged:
            prev_time, prev_state = merged[-1]
            same_time = abs(time - prev_time) < 1e-6
            same_pose = same_time
            if same_time:
                for d in range(JOINT_COUNT):
                    if (
                        abs(state.left[d] - prev_state.left[d]) > 1e-9
                        or abs(state.right[d] - prev_state.right[d]) > 1e-9
                    ):
                        same_pose = False
                        break
            if same_pose:
                continue
        merged.append((time, state))

    return [k[1] for k in merged], [k[0] for k in merged]
