#!/usr/bin/env python3
"""端到端验证: 已知距离的物体 -> 3D 位置误差。

做法:
1. 采一帧深度图
2. 找一块**足够大且连续**的区域 (比如墙面/桌面)
3. 拿这一块的中位深度当「真值」
4. 同一个区域用 YOLO 框出来 (或者手工指定一个框)
5. 比较 solver 算出来的 Z 与真值

用法::

    conda run -n yolo python tools/check_e2e.py
    conda run -n yolo python tools/check_e2e.py --bbox 400,300,900,600
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from camera import CameraError, OrbbecCamera  # noqa: E402
from config import load_config  # noqa: E402
from detector import Detector, DetectorError  # noqa: E402
from solver import PoseSolver  # noqa: E402

PASSED = 0
FAILED = 0


def check(desc: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {desc}" + (f"  {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {desc}  {detail}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", default="", help="手工指定框 x1,y1,x2,y2 (彩色图坐标)")
    args = ap.parse_args()

    cfg = load_config()
    cam = OrbbecCamera(cfg.camera)
    try:
        cam.open()
    except CameraError as exc:
        print(f"[FAIL] 打开相机失败: {exc}")
        return 1

    print("\n[1] 采集")
    frame = None
    for _ in range(5):
        frame = cam.read()
        if frame is not None:
            break
    if frame is None or frame.depth_raw is None:
        print("[FAIL] 没采到帧")
        cam.close()
        return 1

    depth_m = frame.depth_meters
    print(f"       彩色 {frame.color.shape}, 深度 {depth_raw_shape(frame)}")
    print(f"       彩色内参 {frame.intrinsics.to_dict()}")
    print(f"       深度内参 {frame.depth_intrinsics.to_dict()}")
    print(f"       对齐模式 {cam.align_mode}")

    valid = depth_m[(depth_m > 0.1) & (depth_m < 5.0)]
    print(f"       有效深度像素 {valid.size}/{depth_m.size}, "
          f"中位 {np.median(valid):.3f}m" if valid.size else "       无有效深度")

    # ---- 选定测试区域 ----
    #
    # 真值必须是**深度一致**的区域 —— 真实场景里框里往往前景背景混着,
    # 那样算出来的「真值」本身就没有意义。
    #
    # 做法: 在图上扫若干个小窗口, 挑**直方图最集中**的那个 (一块平整
    # 表面), 拿它的众数当真值。
    h, w = depth_m.shape[:2]
    best = None
    for frac in (0.12, 0.16, 0.20, 0.25):
        bw, bh = int(w * frac), int(h * frac)
        for gy in range(0, h - bh, bh):
            for gx in range(0, w - bw, bw):
                cell = depth_m[gy:gy + bh, gx:gx + bw]
                m = (cell > 0.1) & (cell < 5.0)
                n = int(m.sum())
                if n < cell.size * 0.5:
                    continue
                vals = cell[m]
                mode = float(np.median(vals))
                near = np.abs(vals - mode) <= max(0.02, mode * 0.05)
                focus = float(near.mean())
                if best is None or focus > best[0]:
                    best = (focus, mode, (gx, gy, gx + bw, gy + bh), n)

    if best is None:
        print("[FAIL] 画面里找不到深度足够平整的区域")
        cam.close()
        return 1

    focus, true_z, depth_bbox, n = best
    color_bbox = depth_bbox   # 已经对齐了, 彩色坐标 == 深度坐标
    print(f"\n[2] 测试区域: {depth_bbox}")
    print(f"       深度集中度 {focus * 100:.0f}%, {n} 个有效像素")
    print(f"       真值深度 (该区域众数): {true_z:.3f} m")

    print("\n[3] solver 解算")
    solver = PoseSolver(cfg.solver, cfg.extrinsic)
    target = solver.solve(
        depth_m,
        depth_bbox,
        frame.intrinsics,
        depth_intrinsics=frame.depth_intrinsics,
        depth_to_color=frame.depth_to_color,
    )
    print(f"       算得 Z = {target.z:.3f} m, 有效={target.depth_valid}, "
          f"占比={target.valid_ratio:.2f}")

    if true_z > 0:
        err = abs(target.z - true_z)
        check("Z 误差 < 5cm", err < 0.05, f"误差 {err * 100:.1f} cm "
              f"(算得 {target.z:.3f} vs 真值 {true_z:.3f})")
        check("深度标记为有效", target.depth_valid is True)
    else:
        print("       (跳过精度检查, 没有真值)")

    # ---- X/Y 一致性 ----
    #
    # solver 输出的是**彩色相机坐标系**下的点 (已经过深度->彩色外参)。
    # 所以期望值要用**彩色**像素和**彩色**内参算, 不能用深度的。
    intr = frame.intrinsics
    color_bbox = color_bbox
    cx_px = (color_bbox[0] + color_bbox[2]) / 2.0
    cy_px = (color_bbox[1] + color_bbox[3]) / 2.0
    exp_x = (cx_px - intr.cx) * target.z / intr.fx
    exp_y = (cy_px - intr.cy) * target.z / intr.fy
    check("X 与彩色像素位置一致", abs(target.x - exp_x) < 2e-3,
          f"{target.x:.4f} vs {exp_x:.4f}")
    check("Y 与彩色像素位置一致", abs(target.y - exp_y) < 2e-3,
          f"{target.y:.4f} vs {exp_y:.4f}")

    print("\n[4] YOLO 检测链路")
    try:
        det = Detector(cfg.detector)
        det.load()
        results = det.infer(frame.color, conf=0.05)
        print(f"       检测到 {len(results)} 个目标")
        for r in results[:5]:
            print(f"         {r.label:16s} conf={r.confidence:.3f} "
                  f"bbox={tuple(round(v) for v in r.bbox)}")
        check("检测器能跑通", True)
    except DetectorError as exc:
        check("检测器能跑通", False, str(exc))

    cam.close()
    print(f"\n通过 {PASSED}, 失败 {FAILED}")
    return 0 if FAILED == 0 else 1


def depth_raw_shape(frame):
    if frame.depth_raw is None:
        return None
    return tuple(frame.depth_raw.shape)


if __name__ == "__main__":
    raise SystemExit(main())
