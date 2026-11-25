#!/usr/bin/env python3
"""细看「方向反转」发生在哪 —— 抖动到底在哪一段。

用法:
    python3 tools/analyze_jitter_detail.py
"""
from __future__ import annotations

import json
import math
import sys
import urllib.request

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


def main():
    post("/v1/stream_trace?enable=true&reset=true")
    res = post("/v1/grasp_sequence", GRASP)
    frames = (get("/v1/stream_trace").get("frames") or [])
    post("/v1/stream_trace?enable=false")
    print("抓取 ok=%s, %d 帧\n" % (res.get("ok"), len(frames)))
    if not frames:
        return 1

    # 流程的时间轴 (从合成结果拿)
    seq = res.get("sequence") or {}
    print("时间轴:")
    for s in seq.get("steps") or []:
        if "start" in s:
            print("  %-6s %-10s [%.2f ~ %.2f]" % (s["type"], s["label"],
                                                  s["start"], s["end"]))
        else:
            print("  %-6s %-10s @%.2f" % (s["type"], s["label"], s["time"]))
    print()

    # ---- 右臂各关节的「速度符号」随时间 ----
    print("=" * 72)
    print("右臂 7 个关节的逐帧步长 (毫度) —— 找反转")
    print("=" * 72)
    for j in range(7):
        signs = []
        for i in range(1, len(frames)):
            d = frames[i]["right"][j] - frames[i - 1]["right"][j]
            signs.append(1 if d > 1e-9 else (-1 if d < -1e-9 else 0))
        nz = [(frames[i]["t"], s) for i, s in enumerate(signs) if s != 0]
        # 找符号变化
        flips = []
        last = None
        for t, s in nz:
            if last is not None and s != last[1]:
                flips.append((last[0], t, last[1], s))
            last = (t, s)
        # 合并相邻的 (抖动会产生一串快速反转)
        merged = []
        for f in flips:
            if merged and f[0] - merged[-1][1] < 0.15:
                merged[-1] = (merged[-1][0], f[1], merged[-1][2], f[3])
            else:
                merged.append(list(f) if False else (f[0], f[1], f[2], f[3]))
        print("\n关节 %d: %d 次反转" % (j, len(flips)))
        for t0, t1, s0, s1 in merged[:6]:
            print("   t=%.3f ~ %.3f  方向 %+d -> %+d" % (t0, t1, s0, s1))

    # ---- 逐帧看右臂 j6 (反转最多的那个) 的原始值 ----
    print("\n" + "=" * 72)
    print("右臂 关节6 的原始目标值 (每 5 帧采样)")
    print("=" * 72)
    for i in range(0, len(frames), 5):
        f = frames[i]
        print("  t=%6.3f  j6=%+.5f  j5=%+.5f  j0=%+.5f"
              % (f["t"], f["right"][6], f["right"][5], f["right"][0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
