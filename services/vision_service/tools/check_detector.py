#!/usr/bin/env python3
"""检测器验证 —— 采一帧跑 YOLO, 看框画得对不对。

会把彩色图和标注结果存到 logs/ 供肉眼确认。

用法::

    conda run -n yolo python tools/check_detector.py
    conda run -n yolo python tools/check_detector.py --image logs/vision_color.png
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

LOG_DIR = Path(__file__).resolve().parents[3] / "logs"


def main() -> int:
    ap = argparse.ArgumentParser(description="检测器验证")
    ap.add_argument("--image", default="", help="用现成的图而不是现场采")
    ap.add_argument("--conf", type=float, default=0.25)
    args = ap.parse_args()

    cfg = load_config()
    cfg.detector.conf = args.conf

    image = None
    cam = None

    if args.image:
        import cv2

        image = cv2.imread(args.image)
        if image is None:
            print(f"读不了图: {args.image}")
            return 1
        print(f"用现成的图: {args.image} ({image.shape})")
    else:
        cam = OrbbecCamera(cfg.camera)
        try:
            cam.open()
        except CameraError as exc:
            print(f"[FAIL] 打开相机失败: {exc}")
            return 1
        frame = cam.read()
        if frame is None or frame.color is None:
            print("[FAIL] 没采到彩色帧")
            cam.close()
            return 1
        image = frame.color
        print(f"现场采帧: {image.shape}")

    print("\n[1] 加载模型")
    det = Detector(cfg.detector)
    try:
        det.load()
    except DetectorError as exc:
        print(f"[FAIL] {exc}")
        if cam:
            cam.close()
        return 1

    print(f"       设备: {det.device}")
    print(f"       类别数: {len(det.class_names)}")
    print(f"       类别: {', '.join(list(det.class_names.values())[:15])}")

    print("\n[2] 推理")
    try:
        results = det.infer(image)
    except DetectorError as exc:
        print(f"[FAIL] {exc}")
        if cam:
            cam.close()
        return 1

    print(f"       检测到 {len(results)} 个目标")
    for r in results:
        print(f"         {r.label:20s} conf={r.confidence:.3f} bbox={tuple(round(v) for v in r.bbox)}")

    # ---- 画图 ----
    try:
        import cv2

        out = LOG_DIR / "vision_detect.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas = image.copy()
        for r in results:
            x1, y1, x2, y2 = [int(round(v)) for v in r.bbox]
            cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(
                canvas,
                f"{r.label} {r.confidence:.2f}",
                (x1, max(15, y1 - 5)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 0),
                2,
            )
        cv2.imwrite(str(out), canvas)
        print(f"       标注图已存: {out}")
    except Exception as exc:
        print(f"       (画图失败: {exc})")

    if cam:
        cam.close()

    print("\n完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
