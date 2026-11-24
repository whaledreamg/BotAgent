#!/usr/bin/env python3
"""只读监控: 盯着正在运行的视觉服务, 记录拔相机前后它的真实行为。

用法:
    python3 tools/watch_camera_drop.py [秒数]

它只做 GET /v1/detect (不碰硬件), 通过 /health 的字段和返回结果
判断「相机还在不在」, 并对比**检测结果的图像哈希** —— 画面变了就是
新帧, 没变就是陈旧帧。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8803"


def get(path: str, timeout: float = 8.0):
    req = urllib.request.Request(BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post(path: str, timeout: float = 15.0):
    req = urllib.request.Request(
        BASE + path, data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    print(f"盯着视觉服务 {seconds:.0f} 秒 —— **现在拔掉相机**")
    print("-" * 100)
    print("%-6s %-9s %-7s %-8s %-7s %-22s %s"
          % ("时刻", "cam_conn", "错误数", "重连次", "det_ms", "detect 结果", "画面"))
    print("-" * 100)

    deadline = time.time() + seconds
    prev_hash = None
    n = 0
    while time.time() < deadline:
        n += 1
        try:
            h = get("/health")
            conn = h.get("camera_connected")
            errs = h.get("errors") or []
            rc = h.get("reconnect_failures")
            note = ""
            try:
                d = post("/v1/detect")
                objs = d.get("objects") or []
                fn = objs[0].get("image_filename") if objs else d.get("annotated_image")
                note = f"200, {len(objs)} 个物体"
                if fn:
                    p = (Path("/home/whale/workspace/project/BotAgent/services/vision_service"
                              "/traces/sessions") / fn.rsplit("/", 1)[0] / "_annotated.jpg")
                    if p.is_file():
                        hh = hashlib.md5(p.read_bytes()).hexdigest()[:10]
                        note += ("  画面**未变**=陈旧帧!" if hh == prev_hash
                                 else "  画面变了=新帧")
                        prev_hash = hh
            except Exception as exc:
                note = f"失败: {exc}"
            print("%-6.0f %-9s %-7s %-8s %-7s %-22s %s"
                  % (n * 2.0, conn, len(errs), rc, h.get("last_detection_ms"),
                     note, (errs[-1][:60] if errs else "")))
        except Exception as exc:
            print(f"health 失败: {exc}", flush=True)
        time.sleep(2.0)
    print("-" * 100)
    print("结束。关键看三件事:")
    print("  1) 拔线后 cam_conn 变 False 了吗?")
    print("  2) detect 是 503 还是继续 200?")
    print("  3) 如果 200, 画面哈希变了吗 (没变=SDK 在回放最后一帧)?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
