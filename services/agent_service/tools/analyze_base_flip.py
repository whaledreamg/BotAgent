#!/usr/bin/env python3
"""只读分析: 由 target_base 反推基座旋转角, 定位基座系翻转的成因。

不依赖 numpy (arm_service 环境没有), 纯 Python 3x3 线性代数。
"""
import json
import math
from pathlib import Path

TRACE = Path(__file__).resolve().parents[1] / "logs" / "grasp_trace.jsonl"


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def norm(a):
    return math.sqrt(dot(a, a))


def scale(a, s):
    return tuple(x * s for x in a)


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def mv(m, v):
    return tuple(dot(m[i], v) for i in range(3))


def tr(m):
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))


def matmul(a, b):
    return tuple(tuple(dot(a[i], b[j]) for j in range(3)) for i in range(3))


def frame(v):
    """由一个方向构造右手正交基 (行向量存列)。"""
    v = scale(v, 1.0 / norm(v))
    up = (0.0, 0.0, 1.0)
    if abs(dot(v, up)) > 0.99:
        up = (1.0, 0.0, 0.0)
    e1 = v
    e2 = cross(up, e1)
    e2 = scale(e2, 1.0 / norm(e2))
    e3 = cross(e1, e2)
    return (e1, e2, e3)


def euler_from_r(r):
    """行向量存列的 R -> ZYX 欧拉角 (rad)。"""
    m = tr(r)
    sy = math.sqrt(m[0][0] ** 2 + m[1][0] ** 2)
    if sy > 1e-6:
        rx = math.atan2(m[2][1], m[2][2])
        ry = math.atan2(-m[2][0], sy)
        rz = math.atan2(m[1][0], m[0][0])
    else:
        rx = math.atan2(-m[1][2], m[1][1])
        ry = math.atan2(-m[2][0], sy)
        rz = 0.0
    return rx, ry, rz


def main():
    rows = [json.loads(l) for l in TRACE.read_text(encoding="utf-8").splitlines() if l.strip()]
    base = (0.0, -0.02, 0.0)

    print("=" * 100)
    print("基座系 (target_base) 时间线 + 反推基座旋转角")
    print("=" * 100)
    print("%-20s %-26s %-26s %-24s" % ("时间", "world 目标", "base 基座系", "反推基座欧拉角(deg)"))
    print("-" * 100)

    for r in rows:
        if not r.get("target"):
            continue
        steps = [s for s in r.get("steps", []) if s.get("name") == "above"]
        mv_ = (steps[0].get("move") or {}) if steps else {}
        tw = mv_.get("target_world") or {}
        tb = mv_.get("target_base") or {}
        if not tw or not tb:
            continue
        w = (tw["x"], tw["y"], tw["z"])
        tbv = (tb["x"], tb["y"], tb["z"])
        rel = sub(w, base)
        ra = frame(rel)
        rb = frame(tbv)
        rot = matmul(rb, tr(ra))
        rx, ry, rz = euler_from_r(rot)
        got = mv(rot, rel)
        err = norm(sub(got, tbv))
        print("%-20s (%+.3f,%+.3f,%+.3f) (%+.3f,%+.3f,%+.3f) a=%+7.1f b=%+7.1f c=%+7.1f  err=%.4f"
              % (r.get("ts"), w[0], w[1], w[2], tbv[0], tbv[1], tbv[2],
                 math.degrees(rx), math.degrees(ry), math.degrees(rz), err))

    print()
    print("=" * 100)
    print("tool_offset 时间线 (TCP 与法兰的偏置)")
    print("=" * 100)
    print("%-20s %14s %14s   %s" % ("时间", "pos_m", "rot_rad", "rot_deg"))
    print("-" * 100)
    for r in rows:
        if not r.get("target"):
            continue
        steps = [s for s in r.get("steps", []) if s.get("name") == "above"]
        mv_ = (steps[0].get("move") or {}) if steps else {}
        to = mv_.get("tool_offset") or {}
        if not to:
            continue
        print("%-20s %14s %14s   %+.2f"
              % (r.get("ts"), to.get("pos_m"), to.get("rot_rad"),
                 math.degrees(to.get("rot_rad") or 0.0)))

    print()
    print("=" * 100)
    print("tcp_before 时间线 (TCP 移动前的世界系位姿)")
    print("=" * 100)
    print("%-20s %-26s %-30s %s" % ("时间", "TCP xyz", "TCP abc", "e 臂型角"))
    print("-" * 100)
    for r in rows:
        if not r.get("target"):
            continue
        steps = [s for s in r.get("steps", []) if s.get("name") == "above"]
        mv_ = (steps[0].get("move") or {}) if steps else {}
        tb_ = mv_.get("tcp_before")
        if not tb_:
            continue
        print("%-20s (%+.4f,%+.4f,%+.4f) (%+.4f,%+.4f,%+.4f)   %s"
              % (r.get("ts"), tb_["x"], tb_["y"], tb_["z"],
                 tb_["a"], tb_["b"], tb_["c"], tb_.get("e")))

    print()
    print("=" * 100)
    print("ik.stage_counts 时间线 (held 多 = 控制器跟不上/解不出)")
    print("=" * 100)
    print("%-20s %s" % ("时间", "stage_counts"))
    print("-" * 100)
    for r in rows:
        if not r.get("target"):
            continue
        steps = [s for s in r.get("steps", []) if s.get("name") == "above"]
        mv_ = (steps[0].get("move") or {}) if steps else {}
        ik = mv_.get("ik")
        if not ik:
            continue
        c = ik.get("stage_counts") or {}
        tot = sum(c.values()) or 1
        print("%-20s %s   held=%.0f%%  err=%s"
              % (r.get("ts"), c, 100.0 * c.get("held", 0) / tot, ik.get("error")))


if __name__ == "__main__":
    main()
