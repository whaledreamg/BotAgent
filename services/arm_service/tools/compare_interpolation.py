#!/usr/bin/env python3
"""离线对比: 逐段 S 曲线 (现状) vs 整条时间轴统一插值 (方案 B)。

用真实抓取数据算两种插值下的关节轨迹, 对比边界处的速度连续性。

**不碰硬件** —— 纯数值推演。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from timeline.interpolation import smooth_step_interpolate  # noqa: E402
from timeline.model import JOINT_COUNT, JointState, Segment  # noqa: E402


def build_segments() -> list:
    """用一次真实抓取的数值重建时间轴片段。"""
    return [
        # 准备点 (动作库终点)
        Segment(label="回准备点", start_time=0.0, duration=3.0,
                end_joints=JointState(
                    left=[0.5061, -0.9179, -1.8354, -1.6425, 1.0282, 0.4152, 1.8521],
                    right=[-0.6126, -0.7241, 1.8431, -1.6685, 2.4545, -0.3599, 0.3698])),
        # 上方点 (逆解)
        Segment(label="移到上方点", start_time=3.0, duration=3.0,
                end_joints=JointState(
                    left=[0.001, -0.6635, -1.2723, -1.699, 0.482, 0.2125, 1.1015],
                    right=[0.8175, -1.026, 0.4203, -1.5251, -0.1344, -0.6384, -3.0466])),
        # 抓取点 (逆解)
        Segment(label="移到抓取点", start_time=6.0, duration=2.0,
                end_joints=JointState(
                    left=[0.001, -0.6635, -1.2723, -1.699, 0.482, 0.2125, 1.1015],
                    right=[0.9476, -1.0423, 0.0783, -1.3714, -0.3932, -0.6, -2.5618])),
        # 安全点 (动作库终点)
        Segment(label="回安全点", start_time=8.0, duration=3.0,
                end_joints=JointState(
                    left=[0.001, -0.6635, -1.2723, -1.699, 0.482, 0.2125, 1.1015],
                    right=[0.4539, -0.2233, 0.7722, -1.4926, -0.3256, 0.2673, -0.9522])),
    ]


ORIGIN = JointState(
    left=[0.003, -0.662, -1.263, -1.698, 0.478, 0.213, 1.092],
    right=[0.456, -0.227, 0.769, -1.492, -0.326, 0.263, -0.96],
)


def eval_current(segs: list, t: float) -> JointState:
    """现状: 每段独立 S 曲线, 段起点 = 上段终点。"""
    for pos, seg in enumerate(segs):
        start = ORIGIN if pos <= 0 else segs[pos - 1].end_joints.copy()
        if seg.start_time <= t < seg.end_time:
            span = max(1e-6, seg.duration)
            return smooth_step_interpolate(start, seg.end_joints, (t - seg.start_time) / span)
    return segs[-1].end_joints.copy()


def eval_spline(segs: list, t: float) -> JointState:
    """方案 B: 整条时间轴做一次 Hermite 样条 (跨段速度连续)。"""
    # 关键帧: 每段的 (起点, 终点)
    keys, times = [], []
    for pos, seg in enumerate(segs):
        start = ORIGIN if pos <= 0 else segs[pos - 1].end_joints.copy()
        keys.append(start); times.append(seg.start_time)
        keys.append(seg.end_joints.copy()); times.append(seg.end_time)

    n = len(keys)
    if t <= times[0]:
        return keys[0].copy()
    if t >= times[-1]:
        return keys[-1].copy()

    i = 0
    while i < n - 2 and t > times[i + 1]:
        i += 1
    t0, t1 = times[i], times[i + 1]
    span = t1 - t0
    if span <= 1e-9:
        return keys[i + 1].copy()
    u = (t - t0) / span

    p1, p2 = keys[i], keys[i + 1]
    dt_in1 = (times[i + 1] - times[i - 1]) if i > 0 else span
    dt_in2 = (times[i + 2] - times[i]) if (i + 2 < n) else span
    p_prev = keys[i - 1] if i > 0 else p1
    p_next = keys[i + 2] if (i + 2 < n) else p2

    u2, u3 = u * u, u * u * u
    h00 = 2 * u3 - 3 * u2 + 1
    h10 = u3 - 2 * u2 + u
    h01 = -2 * u3 + 3 * u2
    h11 = u3 - u2

    left, right = [], []
    for k in range(JOINT_COUNT):
        m1 = (p2.left[k] - p_prev.left[k]) / dt_in1
        m2 = (p_next.left[k] - p1.left[k]) / dt_in2
        left.append(h00 * p1.left[k] + h10 * span * m1
                    + h01 * p2.left[k] + h11 * span * m2)
        m1 = (p2.right[k] - p_prev.right[k]) / dt_in1
        m2 = (p_next.right[k] - p1.right[k]) / dt_in2
        right.append(h00 * p1.right[k] + h10 * span * m1
                     + h01 * p2.right[k] + h11 * span * m2)
    return JointState(left=left, right=right)


def measure(fn, segs, dt=0.016) -> dict:
    total = max(s.end_time for s in segs)
    n = int(total / dt) + 1
    samples = [(i * dt, fn(segs, i * dt)) for i in range(n)]

    steps = []
    for i in range(1, len(samples)):
        a, b = samples[i - 1][1], samples[i][1]
        steps.append([abs(y - x) for x, y in zip(a.right, b.right)])

    # 方向反转
    revs = 0
    for j in range(JOINT_COUNT):
        sign = 0
        for i in range(1, len(samples)):
            d = samples[i][1].right[j] - samples[i - 1][1].right[j]
            if abs(d) < 1e-12:
                continue
            s = 1 if d > 0 else -1
            if sign and s != sign:
                revs += 1
            sign = s

    # 边界处的速度突变
    boundaries = [s.start_time for s in segs[1:]]
    jumps = []
    for bt in boundaries:
        idx = int(bt / dt)
        if idx < 2 or idx >= len(steps):
            continue
        prev = steps[idx - 2]
        cur = steps[idx - 1]
        jumps.append(math.degrees(max(abs(a - b) for a, b in zip(cur, prev))))

    return {
        "max_step_deg": round(math.degrees(max(max(s) for s in steps)), 3),
        "reversals": revs,
        "boundary_jumps_deg": [round(j, 3) for j in jumps],
        "max_boundary_jump": round(max(jumps), 3) if jumps else 0.0,
    }


def main() -> int:
    segs = build_segments()
    print("=" * 66)
    print("方案对比: 逐段 S 曲线 (现状) vs 整条时间轴统一样条 (方案 B)")
    print("=" * 66)

    a = measure(eval_current, segs)
    b = measure(eval_spline, segs)

    print("\n指标                        现状(逐段S)   方案B(统一样条)")
    print("-" * 66)
    print("帧间最大步长 (度)           %10.3f   %14.3f" % (a["max_step_deg"], b["max_step_deg"]))
    print("方向反转次数                %10d   %14d" % (a["reversals"], b["reversals"]))
    print("边界速度突变 (度)           %10s   %14s" % (a["boundary_jumps_deg"], b["boundary_jumps_deg"]))
    print("最大边界突变 (度)           %10.3f   %14.3f" % (a["max_boundary_jump"], b["max_boundary_jump"]))

    print("\n关键差异:")
    if b["reversals"] < a["reversals"]:
        print("  方向反转从 %d 次降到 %d 次" % (a["reversals"], b["reversals"]))
    if b["max_boundary_jump"] < a["max_boundary_jump"]:
        print("  边界速度突变从 %.3f° 降到 %.3f°"
              % (a["max_boundary_jump"], b["max_boundary_jump"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
