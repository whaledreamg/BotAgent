#!/usr/bin/env python3
"""抓取流程的轨迹抖动分析 (多次试验)。

**只读 + 一次抓取** —— 开诊断记录 -> 跑 N 次抓取 -> 取回每一帧实际下发的
关节目标 -> 算抖动的来源。

抖动的可能来源, 逐个排除:

1. **上游目标本身在抖** —— 时间轴求值出来的关节角就有台阶/回退
2. **分段拼接处跳变** —— 片段交界处前后帧不连续
3. **插值锚点抖动** —— streamer 的锚点更新时机不稳
4. **实测角回传抖动** —— 控制器报的实测角本身在跳

用法:
    python3 tools/analyze_grasp_jitter.py [次数]
"""
from __future__ import annotations

import json
import math
import statistics
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


def post(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=200) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(path: str) -> dict:
    req = urllib.request.Request(BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def analyze(frames: list, label: str) -> dict:
    """算出一帧帧之间的抖动指标。"""
    if len(frames) < 3:
        return {"error": "帧数太少"}

    out = {}
    for side in ("left", "right"):
        # 逐关节的「帧间步长」
        steps = []
        for i in range(1, len(frames)):
            a = frames[i - 1][side]
            b = frames[i][side]
            steps.append([abs(y - x) for x, y in zip(a, b)])

        max_step = [max(s[j] for s in steps) for j in range(7)]
        # 速度: 一阶差分有噪声, 用窗口差分看「整体是否匀速」
        dt = [frames[i]["t"] - frames[i - 1]["t"] for i in range(1, len(frames))]
        mean_dt = statistics.mean(dt) if dt else 0.0
        # 三阶差分 ≈ 加速度变化, 抖动在这里最明显
        acc = []
        for i in range(1, len(steps) - 1):
            acc.append([steps[i + 1][j] - steps[i][j] for j in range(7)])

        out[side] = {
            "max_step_deg": [round(math.degrees(v), 2) for v in max_step],
            "max_step_overall_deg": round(math.degrees(max(max_step)), 2),
            "mean_dt_ms": round(mean_dt * 1000, 2),
            "dt_jitter_ms": round(
                (max(dt) - min(dt)) * 1000, 2) if dt else 0.0,
            "acc_max_deg": round(
                math.degrees(max(max(abs(a[j]) for a in acc) for j in range(7))), 3
            ) if acc else 0.0,
            "steps_mean_deg": [
                round(math.degrees(statistics.mean(s[j] for s in steps)), 3)
                for j in range(7)
            ],
        }

    # ---- 分段拼接处的跳变 ----
    # 片段边界 = 相邻两帧的「速度突变」最大的地方
    gaps = []
    for i in range(2, len(frames)):
        for s in ("left", "right"):
            prev = [abs(frames[i - 1][s][j] - frames[i - 2][s][j])
                    for j in range(7)]
            cur = [abs(frames[i][s][j] - frames[i - 1][s][j])
                   for j in range(7)]
            d = [abs(a - b) for a, b in zip(cur, prev)]
            gaps.append((math.degrees(max(d)), frames[i]["t"], s))
    gaps.sort(reverse=True)
    out["top_speed_jumps"] = [
        {"deg": round(g[0], 2), "t": g[1], "arm": g[2]} for g in gaps[:8]
    ]

    # ---- 目标是否单调 (回退 = 抖) ----
    for side in ("right",):
        reversals = 0
        for j in range(7):
            sign = 0
            for i in range(1, len(frames)):
                d = frames[i][side][j] - frames[i - 1][side][j]
                if abs(d) < 1e-9:
                    continue
                s = 1 if d > 0 else -1
                if sign and s != sign:
                    reversals += 1
                sign = s
        out[side]["direction_reversals"] = reversals

    return out


def main() -> int:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    print("=" * 70)
    print("抓取轨迹抖动分析 (%d 次试验)" % runs)
    print("=" * 70)

    all_results = []
    for run in range(1, runs + 1):
        print("\n--- 第 %d 次 ---" % run)
        post("/v1/stream_trace?enable=true&reset=true")
        try:
            res = post("/v1/grasp_sequence", GRASP)
        except Exception as exc:
            print("  抓取失败: %s" % exc)
            continue
        data = get("/v1/stream_trace")
        frames = data.get("frames") or []
        post("/v1/stream_trace?enable=false")
        print("  抓取 ok=%s  记录 %d 帧" % (res.get("ok"), len(frames)))
        if not frames:
            continue
        r = analyze(frames, "run%d" % run)
        all_results.append(r)

        for side in ("left", "right"):
            s = r.get(side) or {}
            print("  [%s]" % side)
            print("    帧间最大步长 = %s 度" % s.get("max_step_deg"))
            print("    平均帧间隔   = %s ms  (抖动 %s ms)"
                  % (s.get("mean_dt_ms"), s.get("dt_jitter_ms")))
            print("    加速度变化   = %s 度" % s.get("acc_max_deg"))
            if side == "right":
                print("    方向反转次数 = %s" % s.get("direction_reversals"))
        print("    速度突变 Top3: %s" % [
            "t=%.2fs %.2f°(%s)" % (g["t"], g["deg"], g["arm"])
            for g in (r.get("top_speed_jumps") or [])[:3]
        ])

    # ---- 汇总 ----
    if all_results:
        print("\n" + "=" * 70)
        print("汇总 (各次之间的波动)")
        print("=" * 70)
        for side in ("left", "right"):
            vals = [r[side]["max_step_overall_deg"] for r in all_results
                    if side in r]
            dts = [r[side]["mean_dt_ms"] for r in all_results if side in r]
            revs = [r["right"]["direction_reversals"] for r in all_results
                    if "right" in r]
            print("\n[%s]" % side)
            print("  帧间最大步长: %s 度" % vals)
            print("  平均帧间隔  : %s ms" % dts)
            if side == "right":
                print("  方向反转    : %s 次" % revs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
