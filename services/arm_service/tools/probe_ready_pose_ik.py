#!/usr/bin/env python3
"""从「准备点」动作反推正确的抓取姿态 (只读)。

关键: 抓取流程的第 1 步是**播准备点动作**, 然后拍照, 然后从那个姿态
抓。所以「抓取姿态」应该就是**准备点的 TCP 姿态** —— 而不是一个
另外示教的、可能矛盾的 a/b/c。

历史故障: ``configs/arm.yaml`` 里示教的 ``right: a=-1.485 b=-0.331
c=0.260`` 在瓶子位置**不可达** (差 96~205mm), 机械臂每次都报
``unreachable``。

本脚本:
  1. 读准备点动作的关节角 -> FK -> 准备点的 TCP 位姿;
  2. 用它当抓取姿态, 试瓶子位置;
  3. 对比配置里的姿态, 看差多少。
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

from robot.kinematics import (  # noqa: E402
    ArmKinematics,
    matrix_to_rpy,
    rpy_to_matrix,
)

ARM_NAME = "RightArm"
ARM_KEY = "rightArm"
TARGETS = [
    (0.4392, -0.1203, -0.3186),
    (0.5013, -0.0986, -0.3887),
]
ABOVE_D = 0.05


def read_action_joints(name: str, key: str):
    p = ROOT / "services" / "arm_service" / "data" / f"{name}.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    segs = d.get("segments") or []
    if not segs:
        return None
    end = segs[-1].get("endJoints") or {}
    q = end.get(key)
    return [float(v) for v in q] if q else None


def main() -> int:
    snap = json.load(urllib.request.urlopen(
        "http://127.0.0.1:8802/v1/kinematics?refresh=true", timeout=8))
    d = snap["arms"][ARM_NAME]
    kin = ArmKinematics(ARM_NAME)
    kin.set_base_from_arm_frame_dict(d["base_frame"])
    cur_q = list((d.get("joints") or [0] * 7)[:7])

    ready_q = read_action_joints("准备点", ARM_KEY)
    if not ready_q:
        print("读不到准备点动作的关节角")
        return 1

    # 准备点的 TCP 位姿 (世界系)
    T = kin.fk_world(ready_q)
    ready_pos = tuple(float(v) for v in T.translation)
    ready_rpy = matrix_to_rpy(T.rotation)
    R = np.asarray(T.rotation, dtype=float)
    print("=" * 74)
    print("准备点 (抓取的出发姿态)")
    print("=" * 74)
    print(f"  关节 = {['%.4f' % v for v in ready_q]}")
    print(f"  TCP (世界系) = ({ready_pos[0]:+.4f}, {ready_pos[1]:+.4f}, "
          f"{ready_pos[2]:+.4f})")
    print(f"  abc          = ({ready_rpy[0]:+.4f}, {ready_rpy[1]:+.4f}, "
          f"{ready_rpy[2]:+.4f})")
    print(f"  工具 X = ({R[0,0]:+.3f}, {R[1,0]:+.3f}, {R[2,0]:+.3f})")
    print(f"  工具 Y = ({R[0,1]:+.3f}, {R[1,1]:+.3f}, {R[2,1]:+.3f})")
    print(f"  工具 Z = ({R[0,2]:+.3f}, {R[1,2]:+.3f}, {R[2,2]:+.3f})")
    print()

    # 配置里的姿态
    cfg_rpy = (-1.485, -0.331, 0.260)
    Rc = rpy_to_matrix(cfg_rpy)
    print("  配置的抓取姿态 abc = (-1.4850, -0.3310, +0.2600)")
    print(f"  工具 X = ({Rc[0,0]:+.3f}, {Rc[1,0]:+.3f}, {Rc[2,0]:+.3f})")
    print(f"  工具 Z = ({Rc[0,2]:+.3f}, {Rc[1,2]:+.3f}, {Rc[2,2]:+.3f})")
    print()

    def test(pos, rpy, seed, tag):
        Tt = pin.SE3(rpy_to_matrix(rpy), np.asarray(pos, dtype=float))
        r = kin.ik_base(kin.T_world_base.inverse() * Tt, seed,
                        max_iters=250, tol_pos=1e-3, tol_rot=1e-2)
        mark = "OK  " if r.ok else "FAIL"
        print(f"  [{mark}] {tag:34s} err={r.err_pos*1000:8.3f}mm "
              f"rot={math.degrees(r.err_rot):7.3f}deg")
        return r

    for tgt in TARGETS:
        print("=" * 74)
        print(f"瓶子目标 = ({tgt[0]:+.4f}, {tgt[1]:+.4f}, {tgt[2]:+.4f})")
        print("=" * 74)
        above = (tgt[0], tgt[1], tgt[2] + ABOVE_D)
        for label, pos in (("above", above), ("grasp", tgt)):
            # 种子用准备点的关节角 —— 这才是真实流程里的起点
            test(pos, ready_rpy, ready_q, f"{label} + 准备点姿态")
            test(pos, cfg_rpy, ready_q, f"{label} + 配置姿态")
            test(pos, ready_rpy, cur_q, f"{label} + 准备点姿态/当前种子")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
