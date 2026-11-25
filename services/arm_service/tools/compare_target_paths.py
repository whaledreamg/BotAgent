#!/usr/bin/env python3
"""不同目标点下的抓取轨迹对比。

**只读诊断** —— 不需要真的移动瓶子, 直接给 ``/v1/grasp_sequence``
传不同的目标坐标, 看轨迹形态差异。

要回答的问题: 为什么瓶子换个位置就看不出轨迹异常?

用法:
    python3 tools/compare_target_paths.py
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

# 准备点 / 安全点的 FK 位置 (本地运动学算的, 与动作库一致)
READY_TCP = (0.2919, -0.4262, -0.3034)   # 准备点.json 的右臂关节角
SAFE_TCP = (0.4228, -0.4240, -0.2069)    # 安全点.json (= 当前实测姿态)

# 历史上成功抓取过的目标点 + 几个「放到别处」的假设位置
TARGETS = [
    ("历史成功(近)", 0.504, -0.116, -0.285),
    ("历史成功(远)", 0.603, -0.128, -0.291),
    ("靠左",        0.354,  0.294, -0.145),
    ("很靠右",      0.700, -0.100, -0.280),
    ("很靠左",      0.150, -0.100, -0.280),
    ("正前方",      0.450,  0.150, -0.250),
]


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


def dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def main() -> int:
    print("=" * 78)
    print("不同目标点下的轨迹对比")
    print("=" * 78)
    print("\n准备点 TCP = (%.4f, %.4f, %.4f)" % READY_TCP)
    print("安全点 TCP = (%.4f, %.4f, %.4f)" % SAFE_TCP)
    print("\n几何预测 (绕路 = |安全->准备| + |准备->目标| - |安全->目标|):")
    print("%-14s %10s %10s %10s" % ("目标点", "准备->目标", "直线路程", "绕路"))
    for label, x, y, z in TARGETS:
        g = (x, y, z)
        d_rp = dist(READY_TCP, g)
        d_sg = dist(SAFE_TCP, g)
        detour = dist(SAFE_TCP, READY_TCP) + d_rp - d_sg
        print("%-14s %9.1fcm %9.1fcm %9.1fcm"
              % (label, d_rp * 100, d_sg * 100, detour * 100))

    print("\n" + "=" * 78)
    print("实测 (真跑一遍, 记录关节流)")
    print("=" * 78)

    for label, x, y, z in TARGETS:
        post("/v1/stream_trace?enable=true&reset=true")
        try:
            res = post("/v1/grasp_sequence", {
                "x": x, "y": y, "z": z, "side": "right", "flow": "grasp"})
        except Exception as exc:
            print("\n%-14s 抓取失败: %s" % (label, exc))
            post("/v1/stream_trace?enable=false")
            continue
        frames = get("/v1/stream_trace").get("frames") or []
        post("/v1/stream_trace?enable=false")

        ok = res.get("ok")
        iks = res.get("ik") or []
        ik_ok = all(i.get("ok") for i in iks) if iks else False

        # 方向反转次数 (右臂)
        revs = 0
        for j in range(7):
            sign = 0
            for i in range(1, len(frames)):
                d = frames[i]["right"][j] - frames[i - 1]["right"][j]
                if abs(d) < 1e-12:
                    continue
                s = 1 if d > 0 else -1
                if sign and s != sign:
                    revs += 1
                sign = s

        # 关节总行程
        travel = 0.0
        for i in range(1, len(frames)):
            travel += sum(abs(a - b) for a, b in
                          zip(frames[i]["right"], frames[i - 1]["right"]))

        print("\n%-14s ok=%-5s 逆解ok=%-5s  %d帧" % (label, ok, ik_ok, len(frames)))
        print("               反转=%d 次  关节总行程=%.1f°" % (revs, math.degrees(travel)))
        for i in iks:
            print("               逆解 %-8s %s  误差 %.3fmm  at_limit=%s"
                  % (i["step"], ["%+.3f" % v for v in i["pose"]],
                     i["err_pos_mm"], i.get("at_limit")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
