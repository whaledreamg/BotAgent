#!/usr/bin/env python3
"""离线验证: 用历史瓶子目标点试本地逆解 (不动机械臂)。

拿 ``grasp_trace.jsonl`` 里历史抓取瓶子的目标点, 逐个试三层回退的
逆解, 看能不能解出来 —— **不发任何控制指令**。
"""
from __future__ import annotations

import json
import math
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "arm_service"))

from robot.kinematics import ArmKinematics  # noqa: E402

TRACE = ROOT / "services" / "agent_service" / "logs" / "grasp_trace.jsonl"


def load_targets() -> list:
    """从历史抓取记录里提取瓶子的目标位姿 (世界系)。"""
    out = []
    if not TRACE.exists():
        return out
    for line in TRACE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("target") != "瓶子":
            continue
        for s in r.get("steps") or []:
            if s.get("name") == "detect":
                for o in s.get("objects") or []:
                    p = o.get("pose")
                    if p and p.get("depth_valid"):
                        out.append((r.get("ts"), p, r.get("outcome")))
                        break
                break
    return out


def main() -> int:
    snap = json.load(urllib.request.urlopen(
        "http://127.0.0.1:8802/v1/kinematics?refresh=true", timeout=8))
    d = snap["arms"]["RightArm"]
    kin = ArmKinematics("RightArm")
    kin.set_base_from_arm_frame_dict(d["base_frame"])
    q = list(d["joints"][:7])
    cart = d["cartesian"]

    print("=" * 74)
    print("当前右臂状态")
    print("=" * 74)
    print(f"  TCP  = ({cart['x']:.4f}, {cart['y']:.4f}, {cart['z']:.4f})")
    print(f"  abc  = ({cart['a']:.4f}, {cart['b']:.4f}, {cart['c']:.4f})")
    print(f"  关节 = {['%.3f' % v for v in q]}")
    print()

    # 抓取姿态 (configs/arm.yaml)
    GRASP_RPY = (-1.485, -0.331, 0.260)
    ABOVE_D = 0.05

    targets = load_targets()
    if not targets:
        print("没有历史瓶子目标")
        return 1
    # 去重 (同一个瓶子多次检测, 位置几乎一样)
    uniq = []
    for ts, p, outcome in targets:
        if not any(abs(p["x"] - u[1]["x"]) < 0.01 and abs(p["z"] - u[1]["z"]) < 0.01
                   for u in uniq):
            uniq.append((ts, p, outcome))
    print(f"历史瓶子目标 (去重后 {len(uniq)} 个):")
    for ts, p, outcome in uniq:
        print(f"  {ts}  xyz=({p['x']:+.4f}, {p['y']:+.4f}, {p['z']:+.4f})  "
              f"上次结果={outcome}")
    print()

    for ts, p, outcome in uniq:
        print("=" * 74)
        print(f"目标 {ts}  xyz=({p['x']:.4f}, {p['y']:.4f}, {p['z']:.4f})")
        print("=" * 74)
        for label, pos in (
            ("above (+0.05m)", (p["x"], p["y"], p["z"] + ABOVE_D)),
            ("grasp (原位)  ", (p["x"], p["y"], p["z"])),
        ):
            # 1) 全姿态
            r1 = kin.ik_world_rpy(pos, GRASP_RPY, q, max_iters=200,
                                   tol_pos=1e-4, tol_rot=1e-3)
            print(f"  [{label}] 全姿态     : {r1.summary()}")
            # 2) 带回退
            r2 = kin.ik_world_rpy_with_fallback(pos, GRASP_RPY, q,
                                                max_iters=200,
                                                tol_pos=1e-4, tol_rot=1e-3)
            tag = "位置优先" if (r2.ok and r2.err_rot > 0.05) else "全姿态"
            print(f"  [{label}] 带回退     : {r2.summary()}  <- {tag}")
            # 3) 只解位置 (姿态完全不管)
            r3 = kin.ik_world_rpy(pos, GRASP_RPY, q, max_iters=200,
                                   tol_pos=1e-4, tol_rot=1e-3, rot_weight=0.0)
            print(f"  [{label}] 纯位置     : {r3.summary()}")
            if r2.ok:
                print(f"       关节解 = {['%.3f' % v for v in r2.q]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
