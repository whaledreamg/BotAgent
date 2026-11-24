#!/usr/bin/env python3
"""抓取姿态可达性预检 (只读, 不动机械臂)。

在真跑之前先回答: **这个目标点 + 这套抓取姿态, 逆解能不能解出来?**

历史教训: ``configs/arm.yaml`` 里示教的抓取姿态 (a=-1.485) 在瓶子
位置**根本不可达**, 机械臂每次都报 ``unreachable``。有工具提前查,
就不用一次次跑真机试。

用法:
    python3 tools/check_grasp_reachable.py              # 用配置里的姿态
    python3 tools/check_grasp_reachable.py --scan       # 顺便扫一遍可达姿态
    python3 tools/check_grasp_reachable.py --x 0.44 --y -0.11 --z -0.32
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "arm_service"))

import numpy as np  # noqa: E402
import pinocchio as pin  # noqa: E402
import yaml  # noqa: E402

from robot.kinematics import ArmKinematics, rpy_to_matrix  # noqa: E402

ARM_NAME = "RightArm"
SIDE = "right"


def load_cfg() -> dict:
    p = ROOT / "configs" / "arm.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def get_snapshot() -> dict:
    return json.load(urllib.request.urlopen(
        "http://127.0.0.1:8802/v1/kinematics?refresh=true", timeout=8))


def try_rpy(kin: ArmKinematics, pos, rpy, seed, tol_pos=1e-3, tol_rot=1e-2):
    T = pin.SE3(rpy_to_matrix(rpy), np.asarray(pos, dtype=float))
    return kin.ik_base(kin.T_world_base.inverse() * T, seed,
                       max_iters=250, tol_pos=tol_pos, tol_rot=tol_rot)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--x", type=float)
    ap.add_argument("--y", type=float)
    ap.add_argument("--z", type=float)
    ap.add_argument("--a", type=float)
    ap.add_argument("--b", type=float)
    ap.add_argument("--c", type=float)
    ap.add_argument("--above", type=float, default=None,
                    help="上方点相对目标的高度 (默认取配置)")
    ap.add_argument("--scan", action="store_true", help="扫一遍可达姿态")
    args = ap.parse_args()

    cfg = load_cfg()
    arm_cfg = cfg.get("arm") or {}
    grasp_cfg = arm_cfg.get("grasp") or {}
    flow_cfg = grasp_cfg.get("flow") or {}
    taught = (grasp_cfg.get(SIDE) or {})

    snap = get_snapshot()
    d = (snap.get("arms") or {}).get(ARM_NAME) or {}
    kin = ArmKinematics(ARM_NAME)
    kin.set_base_from_arm_frame_dict(d.get("base_frame") or {})
    q = list((d.get("joints") or [0] * 7)[:7])
    cart = d.get("cartesian") or {}

    print("=" * 74)
    print(f"{ARM_NAME} 可达性预检")
    print("=" * 74)
    print(f"  当前 TCP = ({cart.get('x',0):+.4f}, {cart.get('y',0):+.4f}, "
          f"{cart.get('z',0):+.4f})  abc = ({cart.get('a',0):+.4f}, "
          f"{cart.get('b',0):+.4f}, {cart.get('c',0):+.4f})")
    print(f"  当前关节 = {['%.3f' % v for v in q]}")

    # ---- 目标点 ----
    if args.x is not None and args.y is not None and args.z is not None:
        tgt = (args.x, args.y, args.z)
        print(f"  目标 (命令行指定) = ({tgt[0]:+.4f}, {tgt[1]:+.4f}, {tgt[2]:+.4f})")
    else:
        tgt = (0.4392, -0.1203, -0.3186)
        print(f"  目标 (历史瓶子)   = ({tgt[0]:+.4f}, {tgt[1]:+.4f}, {tgt[2]:+.4f})")

    # ---- 姿态 ----
    if args.a is not None or args.b is not None or args.c is not None:
        rpy = (args.a if args.a is not None else taught.get("a", 0.0),
               args.b if args.b is not None else taught.get("b", 0.0),
               args.c if args.c is not None else taught.get("c", 0.0))
        src = "命令行"
    elif taught:
        rpy = (float(taught["a"]), float(taught["b"]), float(taught["c"]))
        src = "配置 (configs/arm.yaml)"
    else:
        rpy = (float(cart.get("a", 0.0)), float(cart.get("b", 0.0)),
               float(cart.get("c", 0.0)))
        src = "保持当前 TCP 姿态"
    print(f"  姿态 {src} = a={rpy[0]:+.4f} b={rpy[1]:+.4f} c={rpy[2]:+.4f}")
    print()

    above_d = args.above if args.above is not None else \
        float(flow_cfg.get("above_distance", 0.05))
    print(f"  上方点偏移 = +{above_d:.3f} m (世界系 Z)")
    print()

    failed = 0
    for label, pos in (("above", (tgt[0], tgt[1], tgt[2] + above_d)),
                       ("grasp", tgt)):
        r = try_rpy(kin, pos, rpy, q)
        mark = "OK  " if r.ok else "FAIL"
        print(f"  [{mark}] {label:6s} ({pos[0]:+.4f}, {pos[1]:+.4f}, {pos[2]:+.4f})  "
              f"err={r.err_pos*1000:8.3f}mm rot={math.degrees(r.err_rot):7.3f}deg"
              + ("  [顶限位]" if r.at_limit else ""))
        if not r.ok:
            failed += 1
    print()

    if args.scan:
        print("  扫可达姿态 (a: -180..180/30, b: -90..90/15, c: -180..180/45):")
        pos = (tgt[0], tgt[1], tgt[2] + above_d)
        found = []
        for a_deg in range(-180, 180, 30):
            for b_deg in range(-90, 91, 15):
                for c_deg in range(-180, 180, 45):
                    rp = (math.radians(a_deg), math.radians(b_deg),
                          math.radians(c_deg))
                    r = try_rpy(kin, pos, rp, q)
                    if r.ok:
                        found.append((a_deg, b_deg, c_deg, r.err_pos * 1000))
        if found:
            print(f"    可达 {len(found)} 个。工具 Z 最朝下的几个:")
            def tz(rp):
                return rpy_to_matrix(rp)[:, 2][2]
            ranked = sorted(found, key=lambda t: tz((
                math.radians(t[0]), math.radians(t[1]), math.radians(t[2]))))
            for a_deg, b_deg, c_deg, e in ranked[:8]:
                print(f"      a={a_deg:4d} b={b_deg:4d} c={c_deg:4d}  err={e:.3f}mm")
        else:
            print("    ** 没有全姿态可达的解 **")

    print()
    if failed:
        print(f"结论: {failed} 个目标不可达 —— 改抓取姿态 (见上面扫描结果)")
    else:
        print("结论: 全部可达 ✓")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
