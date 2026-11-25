#!/usr/bin/env python3
"""逆解解支一致性检查 (纯离线, 不碰硬件)。

**核心怀疑**: 上方点和抓取点**各自独立**用同一个种子 (实测姿态) 解 IK。
两次解可能落在**不同的解支**上 —— 于是「上方点 -> 抓取点」这一段
手臂要绕一大圈, 而这段的绕路大小**取决于目标点在哪儿**。

这就解释了「瓶子换个位置就看不出问题」。

用法:
    python3 tools/check_ik_branch_consistency.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

SVC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SVC))

from robot.kinematics import ArmKinematics  # noqa: E402

BASE = dict(x=0.0, y=-0.02, z=0.0, a=1.571, b=1.571, c=0.0)
MEASURED = [0.4539, -0.2233, 0.7722, -1.4926, -0.3256, 0.2673, -0.9522]
RPY = (-1.935, 0.4417, -1.4622)     # configs/arm.yaml 示教的右臂抓取姿态
ABOVE_DIST = 0.05

TARGETS = [
    ("当前瓶子位置", 0.504, -0.116, -0.285),
    ("历史成功A",   0.441, -0.117, -0.320),
    ("历史成功B",   0.603, -0.128, -0.291),
    ("历史成功C",   0.476, -0.047, -0.292),
    ("历史成功D",   0.354,  0.294, -0.145),
    ("很靠左",      0.150, -0.100, -0.280),
    ("正前方",      0.450,  0.150, -0.250),
    ("很靠右",      0.700, -0.100, -0.280),
]


def jdist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def main() -> int:
    kin = ArmKinematics("RightArm")
    kin.set_base_from_arm_frame_dict(BASE)

    print("=" * 92)
    print("上方点/抓取点 独立逆解的解支一致性")
    print("=" * 92)
    print("\n种子 = 实测姿态 (两段用的是同一个种子)")
    print("测得: |above - grasp| 相对 |measured - grasp| 的倍数")
    print("倍数大 = 两次解落在不同解支 = 中间那段要绕大圈\n")

    print("%-14s %9s %9s %9s %7s %6s %6s"
          % ("目标点", "|测->上|", "|测->抓|", "|上->抓|", "倍数", "上atL", "抓atL"))
    print("-" * 92)

    for label, x, y, z in TARGETS:
        above = (x, y, z + ABOVE_DIST)
        grasp = (x, y, z)

        ik_a = kin.ik_world_rpy_with_fallback(above, RPY, MEASURED,
                                              max_iters=120, tol_pos=1e-4,
                                              tol_rot=1e-3)
        ik_g = kin.ik_world_rpy_with_fallback(grasp, RPY, MEASURED,
                                              max_iters=120, tol_pos=1e-4,
                                              tol_rot=1e-3)
        if ik_a.q is None or ik_g.q is None:
            print("%-14s 逆解失败" % label)
            continue

        d_ma = jdist(MEASURED, ik_a.q)
        d_mg = jdist(MEASURED, ik_g.q)
        d_ag = jdist(ik_a.q, ik_g.q)
        ratio = d_ag / max(d_mg, 1e-9)

        print("%-14s %8.1f° %8.1f° %8.1f° %7.2f %6s %6s"
              % (label, math.degrees(d_ma), math.degrees(d_mg),
                 math.degrees(d_ag), ratio,
                 "是" if ik_a.at_limit else "否",
                 "是" if ik_g.at_limit else "否"))

    print("\n" + "=" * 92)
    print("对照: 若两次逆解**串行** (用上方的解作为抓取点的种子)")
    print("=" * 92)
    print("%-14s %9s %9s %7s" % ("目标点", "串行|上->抓|", "并行|上->抓|", "省下"))
    print("-" * 92)

    for label, x, y, z in TARGETS:
        above = (x, y, z + ABOVE_DIST)
        grasp = (x, y, z)
        ik_a = kin.ik_world_rpy_with_fallback(above, RPY, MEASURED,
                                              max_iters=120, tol_pos=1e-4,
                                              tol_rot=1e-3)
        if ik_a.q is None:
            continue
        # 串行: 用上方的解做种子
        ik_s = kin.ik_world_rpy_with_fallback(grasp, RPY, ik_a.q,
                                              max_iters=120, tol_pos=1e-4,
                                              tol_rot=1e-3)
        # 并行: 用实测做种子 (现状)
        ik_p = kin.ik_world_rpy_with_fallback(grasp, RPY, MEASURED,
                                              max_iters=120, tol_pos=1e-4,
                                              tol_rot=1e-3)
        if ik_s.q is None or ik_p.q is None:
            print("%-14s 逆解失败" % label)
            continue
        d_serial = math.degrees(jdist(ik_a.q, ik_s.q))
        d_par = math.degrees(jdist(ik_a.q, ik_p.q))
        print("%-14s %8.1f° %8.1f° %6.1f°"
              % (label, d_serial, d_par, d_par - d_serial))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
