#!/usr/bin/env python3
"""只读采样: 持续拉机械臂运动学快照, 画出真实轨迹。

**不碰硬件** —— 只 GET /v1/kinematics, 记录 TCP 在世界系下的轨迹。
用来判断「抓取轨迹诡异」到底是:
  - 机械臂真的走了奇怪的路径 (控制层问题)
  - 还是**读到的位姿本身就在乱跳** (数据层问题)

用法:
    python3 tools/trace_arm_motion.py [秒数] [间隔秒]
    python3 tools/trace_arm_motion.py 30 0.05
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8802"


def get(path: str, timeout: float = 5.0):
    req = urllib.request.Request(BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 0.1
    print(f"采样 {seconds:.0f} 秒, 间隔 {interval*1000:.0f}ms")
    print("**现在触发一次抓取**")
    print("-" * 112)
    print("%-8s %-26s %-34s %-10s %s"
          % ("时刻", "TCP xyz", "TCP abc", "e 臂型角", "相对上一帧"))
    print("-" * 112)

    samples = []
    prev = None
    deadline = time.time() + seconds
    t0 = time.time()
    while time.time() < deadline:
        try:
            d = get("/v1/kinematics?refresh=true")
            for name, a in (d.get("arms") or {}).items():
                c = a.get("cartesian")
                if not c:
                    continue
                xyz = (c.get("x", 0.0), c.get("y", 0.0), c.get("z", 0.0))
                abc = (c.get("a", 0.0), c.get("b", 0.0), c.get("c", 0.0))
                e = c.get("e")
                delta = "-" if prev is None else f"{dist(xyz, prev)*1000:.1f}mm"
                samples.append({
                    "t": time.time() - t0, "arm": name, "xyz": xyz,
                    "abc": abc, "e": e,
                    "bf": a.get("base_frame"),
                })
                print("%-8.2f (%+.4f,%+.4f,%+.4f) (%+.3f,%+.3f,%+.3f) %-10s %s"
                      % (time.time() - t0, xyz[0], xyz[1], xyz[2],
                         abc[0], abc[1], abc[2],
                         f"{e:.4f}" if e is not None else "-", delta))
                prev = xyz
        except Exception as exc:
            print(f"  采样失败: {exc}")
        time.sleep(interval)

    print("-" * 112)
    print("汇总")
    for name in {s["arm"] for s in samples}:
        ss = [s for s in samples if s["arm"] == name]
        if len(ss) < 2:
            continue
        print(f"\n[{name}] 采样 {len(ss)} 点")
        # 每一步的位移
        steps = []
        for i in range(1, len(ss)):
            d = dist(ss[i]["xyz"], ss[i - 1]["xyz"])
            if d > 1e-5:
                steps.append((ss[i]["t"], d))
        if steps:
            print(f"  移动次数 = {len(steps)}")
            print(f"  单步位移: 最大 {max(s[1] for s in steps)*1000:.1f}mm  "
                  f"最小 {min(s[1] for s in steps)*1000:.1f}mm  "
                  f"平均 {sum(s[1] for s in steps)/len(steps)*1000:.1f}mm")
            print("  最大的 5 步:")
            for t, d in sorted(steps, key=lambda x: -x[1])[:5]:
                print(f"    t={t:6.2f}s  {d*1000:8.1f}mm")
        # 姿态范围
        for i, k in enumerate("abc"):
            vals = [s["abc"][i] for s in ss]
            print(f"  {k}: {min(vals):+.4f} ~ {max(vals):+.4f}  跨度 {math.degrees(max(vals)-min(vals)):.1f}deg")
        es = [s["e"] for s in ss if s.get("e") is not None]
        if es:
            print(f"  e: {min(es):+.4f} ~ {max(es):+.4f}  跨度 {math.degrees(max(es)-min(es)):.1f}deg")
        # base_frame 是否稳定
        bfs = [s["bf"] for s in ss if s.get("bf")]
        if bfs:
            keys = "xyzabc"
            for i, k in enumerate(keys):
                vals = [bf.get(k, 0.0) for bf in bfs]
                span = max(vals) - min(vals)
                if span > 1e-6:
                    print(f"  [!!] base_frame.{k} 抖动了: {min(vals):+.4f} ~ {max(vals):+.4f} "
                          f"(跨度 {span:.6f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
