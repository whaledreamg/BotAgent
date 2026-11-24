#!/usr/bin/env python3
"""找出配置抓取姿态差在哪, 并给出可用的替代姿态 (只读)。

结论已知: 第二个瓶子位置有 221 个可达姿态, 含「工具 Z 轴完全朝下」
(a=-180, b=0)。配置里的 (a=-1.485, b=-0.331, c=0.260) 不可达。

本脚本定位:
  1. a/b/c 各自偏离多少;
  2. 从配置姿态走到可达姿态的「最短路」;
  3. 给出几个**建议的**抓取姿态 (工具朝下 + 夹爪方向合理);
  4. 顺带看: 用「保持当前姿态」能不能到 (配置留空时的行为)。
"""
from __future__ import annotations

import json
import math
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "arm_service"))

import numpy as np  # noqa: E402
import pinocchio as pin  # noqa: E402

from robot.kinematics import ArmKinematics, rpy_to_matrix  # noqa: E402

TARGETS = [
    (0.5013, -0.0986, -0.3887),
    (0.4392, -0.1203, -0.3186),
]
GRASP_RPY = (-1.485, -0.331, 0.260)


def tool_axes(rpy):
    R = rpy_to_matrix(rpy)
    return R[:, 0], R[:, 1], R[:, 2]


def main() -> int:
    snap = json.load(urllib.request.urlopen(
        "http://127.0.0.1:8802/v1/kinematics?refresh=true", timeout=8))
    d = snap["arms"]["RightArm"]
    kin = ArmKinematics("RightArm")
    kin.set_base_from_arm_frame_dict(d["base_frame"])
    q = list(d["joints"][:7])
    cart = d["cartesian"]

    x_axis, y_axis, z_axis = tool_axes(GRASP_RPY)
    print("=" * 74)
    print("配置的抓取姿态 (a=-1.485, b=-0.331, c=0.260)")
    print("=" * 74)
    print(f"  工具 X (夹爪张开方向) = ({x_axis[0]:+.3f}, {x_axis[1]:+.3f}, {x_axis[2]:+.3f})")
    print(f"  工具 Y               = ({y_axis[0]:+.3f}, {y_axis[1]:+.3f}, {y_axis[2]:+.3f})")
    print(f"  工具 Z (夹爪指向)    = ({z_axis[0]:+.3f}, {z_axis[1]:+.3f}, {z_axis[2]:+.3f})")
    print(f"  当前 TCP 姿态 abc    = ({cart['a']:.4f}, {cart['b']:.4f}, {cart['c']:.4f})")
    cx, cy, cz = tool_axes((cart["a"], cart["b"], cart["c"]))
    print(f"  当前工具 Z           = ({cx[0]:+.3f}, {cy[0]:+.3f}, {cz[2]:+.3f})")
    print()

    for tgt in TARGETS:
        pos = (tgt[0], tgt[1], tgt[2] + 0.05)
        print("=" * 74)
        print(f"目标 above = ({pos[0]:.4f}, {pos[1]:.4f}, {pos[2]:.4f})")
        print("=" * 74)

        def try_rpy(rpy, seed=None, tag=""):
            T = pin.SE3(rpy_to_matrix(rpy), np.array(pos))
            r = kin.ik_base(kin.T_world_base.inverse() * T,
                            seed if seed is not None else q,
                            max_iters=250, tol_pos=1e-3, tol_rot=1e-2)
            print(f"    {tag:28s} {r.summary()}")
            return r

        print("  逐轴试探 (以可达的 a=-180,b=0,c=0 为参照):")
        base_rpy = (math.pi, 0.0, 0.0)
        try_rpy(base_rpy, tag="a=180 b=0 c=0 (工具朝下)")
        for a in (2.9, 3.0, 3.1, math.pi, 3.3):
            try_rpy((a, 0.0, 0.0), tag=f"a={math.degrees(a):.0f} b=0 c=0")
        for b in (0.0, -0.2, -0.331, 0.2, 0.4):
            try_rpy((math.pi, b, 0.0), tag=f"a=180 b={math.degrees(b):+.0f} c=0")
        for c in (0.0, 0.26, -0.5, 0.5, math.pi/2):
            try_rpy((math.pi, 0.0, c), tag=f"a=180 b=0 c={math.degrees(c):+.0f}")
        print()
        print("  配置的原姿态:")
        try_rpy(GRASP_RPY, tag="配置 a/b/c")
        print()
        print("  「保持当前 TCP 姿态」(配置留空时的行为):")
        try_rpy((cart["a"], cart["b"], cart["c"]), tag="当前 abc")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
