#!/usr/bin/env python3
"""相机内参标定 (重标出厂内参)。

用法::

    # 采集 + 标定 + 写结果
    conda run -n yolo python tools/calib_intrinsics.py --capture

    # 只从已有的图片标定
    conda run -n yolo python tools/calib_intrinsics.py --images /tmp/calib_imgs

    # 只看当前内参与出厂内参的差异
    conda run -n yolo python tools/calib_intrinsics.py --compare

**拍摄要点** (决定了标定质量):
- 在**实际工作距离** (0.5~2.0 m) 拍, 不要在太近或太远处凑数
- 板要覆盖画面**不同区域**: 中心、四角、上下左右边缘都来几张
- 板要有**不同倾角**: 正面、左右倾 20~45°、上下倾 20~45°
- 至少 20 张, 建议 25~30 张
- 板要**完整可见** (至少 6 个角点, 最好 15+)
- 光照均匀, 不要有强反光

**为什么必须做**: SDK 给的是标称内参, 1% 误差在 1.5 m 处就是 15 mm ——
跟手眼外参的误差同量级。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.board import BoardSpec  # noqa: E402
from calibration.intrinsics import calibrate  # noqa: E402
from calibration.paths import config_path  # noqa: E402


def load_spec(path: Optional[Path] = None) -> BoardSpec:
    """从 configs/calibration.yaml 读板子规格。

    顶层是 ``board:`` 或者包在 ``vision_calibration:`` 里都认。
    """
    if path is None:
        path = config_path("calibration.yaml")
    if not path.exists():
        print(f"找不到 {path}, 用默认规格", file=sys.stderr)
        return BoardSpec()

    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        section = data.get("vision_calibration", data)
        return BoardSpec.from_dict(section.get("board", {}))
    except Exception as exc:
        print(f"读规格失败 ({exc}), 用默认", file=sys.stderr)
        return BoardSpec()


def capture_images(
    count: int,
    out_dir: Path,
    board: BoardSpec,
    min_corners: int = 8,
) -> List[np.ndarray]:
    """交互式采集: 每按一次回车拍一张。"""
    from camera import OrbbecCamera
    from calibration.board import detect_charuco
    from config import load_config

    cfg = load_config()
    cam = OrbbecCamera(cfg.camera)

    print("正在打开相机...", file=sys.stderr)
    try:
        cam.open()
    except Exception as exc:
        print(f"相机打不开: {exc}", file=sys.stderr)
        return []

    print(f"相机: {cam.device_info.get('name', '?')}", file=sys.stderr)
    print(f"当前内参: fx={cam.intrinsics.fx:.2f} fy={cam.intrinsics.fy:.2f} "
          f"cx={cam.intrinsics.cx:.2f} cy={cam.intrinsics.cy:.2f}", file=sys.stderr)

    out_dir.mkdir(parents=True, exist_ok=True)
    images: List[np.ndarray] = []

    try:
        import cv2

        for i in range(count):
            print(
                f"\n[{i + 1}/{count}] 摆好板子 (换位置/换角度), 按回车拍摄, "
                f"输入 q 退出",
                file=sys.stderr,
            )
            try:
                key = input("> ")
            except EOFError:
                break
            if key.strip().lower() in ("q", "quit", "exit"):
                break

            frame = cam.read()
            if frame is None or frame.color is None:
                print("  没采到帧, 跳过", file=sys.stderr)
                continue

            img_pts, _ = detect_charuco(frame.color, board)
            if len(img_pts) < min_corners:
                print(
                    f"  只检出 {len(img_pts)} 个角点 (要求 >= {min_corners}), 跳过",
                    file=sys.stderr,
                )
                continue

            path = out_dir / f"img_{len(images):03d}.png"
            cv2.imwrite(str(path), frame.color)
            print(f"  已保存 {path.name} ({len(img_pts)} 个角点)", file=sys.stderr)
            images.append(frame.color)
    finally:
        cam.close()

    return images


def load_images(folder: Path) -> List[np.ndarray]:
    import cv2

    images: List[np.ndarray] = []
    for path in sorted(folder.glob("*.png")):
        img = cv2.imread(str(path))
        if img is not None:
            images.append(img)
    return images


def compare_with_factory() -> None:
    """对比出厂内参与实测内参 —— 看差多少。"""
    from camera import OrbbecCamera
    from config import load_config

    cfg = load_config()
    cam = OrbbecCamera(cfg.camera)
    try:
        cam.open()
    except Exception as exc:
        print(f"相机打不开: {exc}", file=sys.stderr)
        return

    intr = cam.intrinsics
    cam.close()

    print("\n当前 (出厂标称) 内参:")
    print(f"  fx={intr.fx:.2f} fy={intr.fy:.2f} cx={intr.cx:.2f} cy={intr.cy:.2f}")
    print(f"  分辨率 {intr.width}x{intr.height}")
    print()
    print("标定后重跑这个命令, 就能看到差异。")


def main() -> int:
    parser = argparse.ArgumentParser(description="相机内参标定")
    parser.add_argument("--capture", action="store_true", help="交互式采集并标定")
    parser.add_argument("--images", type=str, default="", help="从图片目录标定")
    parser.add_argument("--count", type=int, default=25, help="采集张数 (默认 25)")
    parser.add_argument(
        "--out", default="", help="标定结果 JSON 路径 (默认存 configs/)",
    )
    parser.add_argument("--compare", action="store_true", help="只打印当前内参")
    args = parser.parse_args()

    if args.compare:
        compare_with_factory()
        return 0

    spec = load_spec(None)
    print(f"标定板规格: {spec.to_dict()}")

    images: List[np.ndarray] = []
    if args.capture:
        out_dir = Path("/tmp/calib_intrinsics")
        images = capture_images(args.count, out_dir, spec)
    elif args.images:
        folder = Path(args.images)
        if not folder.exists():
            print(f"图片目录不存在: {folder}", file=sys.stderr)
            return 1
        images = load_images(folder)
        print(f"从 {folder} 读到 {len(images)} 张图")
    else:
        print("要么用 --capture 采集, 要么用 --images 指定图片目录", file=sys.stderr)
        return 1

    if len(images) < 3:
        print(f"只有 {len(images)} 张可用图, 至少要 3 张", file=sys.stderr)
        return 1

    print(f"\n开始标定 ({len(images)} 张图)...")
    result = calibrate(images, spec)
    if result is None:
        print("标定失败 —— 检查: 图片数够不够 / 板子有没有被挡住 / 分辨率一不一致",
              file=sys.stderr)
        return 1

    print("\n标定结果:")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))

    out = Path(args.out) if args.out else config_path("intrinsics.json")
    result.save(out)
    print(f"\n已保存到 {out}")
    print()
    print("下一步: 把这个文件里的 camera_matrix / dist_coeffs 写进")
    print("  configs/vision.yaml 的 camera.intrinsics 段")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
