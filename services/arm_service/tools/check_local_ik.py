#!/usr/bin/env python3
"""本地逆解自检 —— 不碰硬件, 只验运动学本身。

验三件事:
  1. URDF 能加载, 7 个关节;
  2. **关节约定探测**: 实测关节角代入 FK, 能不能对上控制器上报的 TCP
     位姿 (对不上 = URDF 关节序/转向与控制器不一致, 机械臂会朝反方向动
     而且**不报任何错**);
  3. 逆解能收敛: 给定一个可达目标, 解出来的关节角代回 FK 是否吻合。

用法:
    python3 tools/check_local_ik.py                 # 用快照里的实测数据
    python3 tools/check_local_ik.py --solve         # 额外做一次逆解
"""
from __future__ import annotations

import json
import math
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "arm_service"))

from robot.kinematics import (  # noqa: E402
    KINEMATICS_AVAILABLE,
    ArmKinematics,
    convention_matches,
    detect_joint_convention,
)

ARM_BASE = "http://127.0.0.1:8802"


def get(path: str):
    req = urllib.request.Request(ARM_BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    if not KINEMATICS_AVAILABLE:
        print("[FAIL] pinocchio 不可用 —— 先装: pip install numpy pin")
        return 1
    print("[OK] pinocchio 可用")

    try:
        snap = get("/v1/kinematics?refresh=true")
    except Exception as exc:
        print(f"[跳过] 拿不到 arm_service 快照 ({exc})")
        return 0
    arms = snap.get("arms") or {}
    if not arms:
        print("[跳过] 快照里没有臂")
        return 0

    failed = 0
    for name, data in arms.items():
        print()
        print("=" * 70)
        print(f"{name}")
        print("=" * 70)
        joints = data.get("joints")
        cart = data.get("cartesian")
        base = data.get("base_frame")
        if not joints or not cart or not base:
            print("  [跳过] 数据不全 (joints/cartesian/base_frame)")
            continue

        try:
            kin = ArmKinematics(name)
        except Exception as exc:
            print(f"  [FAIL] URDF 加载失败: {exc}")
            failed += 1
            continue
        print(f"  URDF = {kin.urdf_path}")
        print(f"  关节 = {kin.joint_names}")
        print(f"  限位 = {['%.2f/%.2f' % (lo, up) for lo, up in zip(kin.lower, kin.upper)]}")

        # 基座必须用控制器实测的
        ok = kin.set_base_from_arm_frame_dict(base)
        print(f"  同步基座 (baseFrame) = {ok}  pos={kin.base_pos} rpy={kin.base_rpy}")

        # ---- 关节约定 ----
        convs = detect_joint_convention(kin, joints[:7], cart)
        print("  关节约定探测 (位置误差 mm / 姿态误差 deg):")
        for cname, e_pos, e_rot in convs:
            mark = "  <= 对得上" if convention_matches(e_pos, e_rot) else ""
            print(f"    {cname:10s} {e_pos*1000:8.2f}mm / {math.degrees(e_rot):6.2f}deg{mark}")
        best = convs[0]
        if not convention_matches(best[1], best[2]):
            print(f"  [FAIL] 没有口径对得上 —— URDF 关节序/转向可能与控制器不一致")
            failed += 1
        else:
            print(f"  [OK] 口径「{best[0]}」对得上")

        # ---- 逆解 ----
        print("  逆解自检 (目标 = 当前 TCP 位姿, 应立刻收敛):")
        r = kin.ik_world_rpy(
            (cart["x"], cart["y"], cart["z"]),
            (cart["a"], cart["b"], cart["c"]),
            joints[:7],
            max_iters=120, tol_pos=1e-4, tol_rot=1e-3,
        )
        print(f"    {r.summary()}")
        if not r.ok:
            print("  [FAIL] 逆解没收敛 (目标是当前位姿, 应该秒解)")
            failed += 1
        else:
            print("  [OK] 逆解收敛")

    print()
    print("=" * 70)
    print(f"结果: {'全部通过 ✓' if failed == 0 else f'{failed} 项失败 ✗'}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
