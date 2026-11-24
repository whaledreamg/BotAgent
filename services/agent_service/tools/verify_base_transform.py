#!/usr/bin/env python3
"""只读验证: 用当前 base_frame 复现历史 target_base, 定位翻转的来源。

思路:
  world_to_base 的**位置**部分只依赖 base_pos / base_q / 目标位置,
  **不含** TCP 姿态或目标姿态。所以 target_base 的位置一旦翻转,
  只可能是 base_pos / base_q 在那一刻变了 —— 或者目标位置变了。
"""
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "arm_service"))

from robot.geometry import euler_to_quat, quat_rotate, quat_conj, quat_normalize, quat_mul  # noqa: E402

TRACE = ROOT / "services" / "agent_service" / "logs" / "grasp_trace.jsonl"


def base_pos_of(base_frame):
    return (base_frame["x"], base_frame["y"], base_frame["z"])


def base_quat_of(base_frame):
    return euler_to_quat(base_frame["a"], base_frame["b"], base_frame["c"])


def to_base(base_frame, pos):
    """复刻 world_to_base 的位置部分。"""
    bp = base_pos_of(base_frame)
    bq = base_quat_of(base_frame)
    rel = (pos[0] - bp[0], pos[1] - bp[1], pos[2] - bp[2])
    return quat_rotate(quat_conj(bq), rel)


def main():
    rows = [json.loads(l) for l in TRACE.read_text(encoding="utf-8").splitlines() if l.strip()]

    # 当前实测的 base_frame (刚拉的)
    cur_base = {"x": 0.0, "y": -0.02, "z": 0.0, "a": 1.5708, "b": 1.5708, "c": 0.0}
    print("当前实测 base_frame (RightArm):", cur_base)
    bq = base_quat_of(cur_base)
    print("  base 四元数 (w,x,y,z) =", tuple(round(v, 5) for v in bq))
    print()
    print("=" * 96)
    print("用**当前** base_frame 复现历史 target_base")
    print("=" * 96)
    print("%-20s %-26s %-26s %s" % ("时间", "历史 target_base", "复算 target_base", "差"))
    print("-" * 96)

    for r in rows:
        if not r.get("target"):
            continue
        steps = [s for s in r.get("steps", []) if s.get("name") == "above"]
        mv_ = (steps[0].get("move") or {}) if steps else {}
        tw = mv_.get("target_world") or {}
        tb = mv_.get("target_base") or {}
        if not tw or not tb:
            continue
        got = to_base(cur_base, (tw["x"], tw["y"], tw["z"]))
        d = max(abs(got[0] - tb["x"]), abs(got[1] - tb["y"]), abs(got[2] - tb["z"]))
        flag = "  <<< 翻转!" if d > 0.02 else ""
        print("%-20s (%+.3f,%+.3f,%+.3f) (%+.3f,%+.3f,%+.3f) %.4f%s"
              % (r.get("ts"), tb["x"], tb["y"], tb["z"],
                 got[0], got[1], got[2], d, flag))

    print()
    print("=" * 96)
    print("若基座 base_q 的 c 角不同, target_base 会怎样 (目标固定为 17:23:30 的)")
    print("=" * 96)
    tw = (0.4364, -0.1196, -0.2152)
    for c_deg in (0, 30, 60, 90, 120, 150, 180, -30, -90):
        bf = dict(cur_base)
        bf["c"] = math.radians(c_deg)
        got = to_base(bf, tw)
        print("  base.c = %+5.1f deg -> target_base = (%+.4f, %+.4f, %+.4f)"
              % (c_deg, got[0], got[1], got[2]))
    print()
    print("  历史正常值 target_base = (+0.2152, +0.4364, +0.0996)")
    print("  历史翻转值 target_base = (+0.2211, -0.4416, -0.1302)")


if __name__ == "__main__":
    main()
