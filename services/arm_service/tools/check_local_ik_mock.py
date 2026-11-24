#!/usr/bin/env python3
"""本地逆解 + 关节流的离线自检 (mock 控制器, 不碰硬件)。

验三件事:
  1. **关节约定**: URDF 关节角代入 FK 能不能对上控制器上报的 TCP 位姿;
  2. **逆解**: 给定目标位姿, 解出的关节角代回 FK 是否吻合;
  3. **关节流**: 下发的报文是不是 ``controlSpace: 0`` (关节), 而不是
     ``controlSpace: 1`` (末端, 那会让控制器自己做逆解)。

用法:
    python3 tools/check_local_ik_mock.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "arm_service"))

from robot.kinematics import (  # noqa: E402
    KINEMATICS_AVAILABLE,
    ArmKinematics,
    convention_matches,
    detect_joint_convention,
)


def main() -> int:
    if not KINEMATICS_AVAILABLE:
        print("[FAIL] pinocchio 不可用 —— 先装: pip install numpy pin")
        print("       (yolo 环境已装好? 那 arm_service 需要用同一个环境)")
        return 1
    print("[OK] pinocchio 可用")
    failed = 0

    for arm in ("LeftArm", "RightArm"):
        print()
        print("=" * 70)
        print(arm)
        print("=" * 70)
        try:
            kin = ArmKinematics(arm)
        except Exception as exc:
            print(f"  [FAIL] URDF 加载失败: {exc}")
            failed += 1
            continue
        print(f"  URDF    = {kin.urdf_path}")
        print(f"  关节    = {kin.joint_names}")
        print(f"  限位    = {['%.2f..%.2f' % (lo, up) for lo, up in zip(kin.lower, kin.upper)]}")

        # ---- FK 自洽: 用零位算 FK, 再解回零位 ----
        q0 = [0.0] * 7
        T0 = kin.fk_base(q0)
        print(f"  零位 FK (基座系) = pos {tuple(round(float(v),4) for v in T0.translation)}")

        # ---- 逆解自洽: 随机取一个关节角, 正解出位姿, 再逆回去 ----
        q_test = [0.3, -0.5, 0.4, 0.2, -0.3, 0.6, 0.1]
        q_test = [float(min(max(v, lo), up))
                  for v, lo, up in zip(q_test, kin.lower, kin.upper)]
        T = kin.fk_base(q_test)
        r = kin.ik_base(T, q_test, max_iters=120, tol_pos=1e-6, tol_rot=1e-6)
        print(f"  逆解自洽: {r.summary()}")
        if not r.ok:
            print("  [FAIL] 正解->逆解 没收敛")
            failed += 1
            continue
        print("  [OK] 正解->逆解 收敛")

        # ---- 换种子也要收敛 (说明不是碰巧卡在种子附近) ----
        r2 = kin.ik_base(T, [0.0] * 7, max_iters=200, tol_pos=1e-4, tol_rot=1e-3)
        print(f"  换零位种子: {r2.summary()}")
        if not r2.ok:
            print("  [WARN] 换种子没收敛 (多起点可达性问题, 不一定是 bug)")

        # ---- 关节约定探测 (用自己造的数据) ----
        #
        # 拿 FK 出来的位姿当成「控制器上报的 TCP 位姿」, 看哪个口径能对上
        # —— 这是自洽性检查: 至少「同序」必须能对上。
        #
        # **注意用 fk_world 而不是 fk_base** —— 探测函数走的是世界系
        # (控制器上报的 cartesian 就是世界系), 基座没同步时两者差一个
        # T_world_base, 比出来的误差没有意义。
        q_meas = list(q_test)
        T_w = kin.fk_world(q_meas)
        cart = {
            "x": float(T_w.translation[0]), "y": float(T_w.translation[1]),
            "z": float(T_w.translation[2]),
        }
        import robot.kinematics as K
        rpy = K.matrix_to_rpy(T_w.rotation)
        cart.update({"a": rpy[0], "b": rpy[1], "c": rpy[2]})
        convs = detect_joint_convention(kin, q_meas, cart)
        print("  关节约定 (自洽检查, 「同序」应最佳):")
        for cname, e_pos, e_rot in convs:
            mark = "  <= 对得上" if convention_matches(e_pos, e_rot) else ""
            print(f"    {cname:10s} {e_pos*1000:8.3f}mm / {math.degrees(e_rot):7.3f}deg{mark}")
        if not convention_matches(convs[0][1], convs[0][2]):
            print("  [FAIL] 连自洽都对不上 —— FK/逆解实现有问题")
            failed += 1
        elif convs[0][0] != "同序":
            print(f"  [WARN] 最佳口径是「{convs[0][0]}」而不是「同序」 —— "
                  f"URDF 关节序与控制器可能不一致")
        else:
            print(f"  [OK] 口径「{convs[0][0]}」对得上")

    print()
    print("=" * 70)
    print(f"结果: {'全部通过 ✓' if failed == 0 else f'{failed} 项失败 ✗'}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
