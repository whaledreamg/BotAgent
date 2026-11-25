#!/usr/bin/env python3
"""抓取轨迹的 FK 分析 —— 末端到底走了什么路径。

**只读诊断** —— 用控制器实测的关节角 + 本地 pinocchio 做 FK, 把 TCP
在世界系下的轨迹画出来, 找出「轨迹明显不对」的地方。

关键检查:
1. 每个片段的**终点 TCP** 是否等于它该去的位置 (逆解目标)
2. 片段之间是否有**回退** (走过去了又退回来)
3. FK 算出的轨迹 vs 逆解目标点是否一致

用法:
    python3 tools/analyze_grasp_path.py
"""
from __future__ import annotations

import json
import math
import sys
import urllib.request
from pathlib import Path

SVC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SVC))

BASE = "http://127.0.0.1:8802"
GRASP = {
    "x": 0.5041187233897153,
    "y": -0.1164381805172395,
    "z": -0.2848797199324501,
    "side": "right",
    "flow": "grasp",
}


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=200) as r:
        return json.loads(r.read().decode())


def get(path):
    req = urllib.request.Request(BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fk_tcp(arm: str, joints, base_frame: dict):
    """用本地运动学算 TCP 在世界系下的位姿。"""
    from robot.kinematics import ArmKinematics

    kin = ArmKinematics(arm)
    kin.set_base_from_arm_frame_dict(base_frame)
    T = kin.fk_world(joints)
    return T.translation


def main() -> int:
    kin = get("/v1/kinematics")
    arms = kin.get("arms") or {}
    print("实测关节角:")
    for n, a in arms.items():
        print("  %-9s %s" % (n, [round(v, 4) for v in (a.get("joints") or [])]))

    post("/v1/stream_trace?enable=true&reset=true")
    res = post("/v1/grasp_sequence", GRASP)
    frames = get("/v1/stream_trace").get("frames") or []
    post("/v1/stream_trace?enable=false")
    print("\n抓取 ok=%s, %d 帧\n" % (res.get("ok"), len(frames)))

    seq = res.get("sequence") or {}
    print("时间轴:")
    for s in seq.get("steps") or []:
        if "start" in s and "end" in s:
            print("  %-6s %-10s [%.2f ~ %.2f]" % (s["type"], s["label"],
                                                  s["start"], s["end"]))
        elif "time" in s:
            print("  %-6s %-10s @%.2f" % (s["type"], s["label"], s["time"]))
        else:
            print("  %-6s %-10s" % (s["type"], s.get("label", "")))

    # ---- 每个片段起点的 FK ----
    print("\n" + "=" * 70)
    print("关键位置的 FK (右臂 TCP 在世界系下)")
    print("=" * 70)
    right_arm = "RightArm"
    bf = (arms.get(right_arm) or {}).get("base_frame") or {}

    # 片段边界时刻对应的帧
    bounds = []
    for s in (seq.get("steps") or []):
        if "start" in s and "end" in s:
            bounds.append(s["start"])
    bounds.append(seq.get("duration") or 0.0)

    print("\n片段边界的 TCP:")
    for bt in sorted(set(bounds)):
        # 找最接近该时刻的帧
        if not frames:
            break
        best = min(frames, key=lambda f: abs(f["t"] - bt))
        tcp = fk_tcp(right_arm, best["right"], bf)
        print("  t=%5.2f  TCP=(%+.4f, %+.4f, %+.4f)"
              % (best["t"], tcp[0], tcp[1], tcp[2]))

    # ---- 逆解目标 vs FK ----
    print("\n逆解目标 (服务端算的):")
    for ik in res.get("ik") or []:
        p = ik["pose"]
        print("  %-8s (%+.4f, %+.4f, %+.4f)  误差 %.3fmm"
              % (ik["step"], p[0], p[1], p[2], ik["err_pos_mm"]))

    # ---- 末端轨迹采样 ----
    print("\n" + "=" * 70)
    print("右臂 TCP 轨迹 (每 0.25s)")
    print("=" * 70)
    prev = None
    last_t = -1.0
    for f in frames:
        tcp = fk_tcp(right_arm, f["right"], bf)
        # 每 0.25s 采样一次
        if f["t"] - last_t < 0.25 and f["t"] < (seq.get("duration") or 0.0) - 0.05:
            continue
        last_t = f["t"]
        d = "-" if prev is None else "%.1fmm" % (math.dist(tcp, prev) * 1000)
        print("  t=%5.2f  (%+.4f, %+.4f, %+.4f)  Δ=%s"
              % (f["t"], tcp[0], tcp[1], tcp[2], d))
        prev = tcp
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
