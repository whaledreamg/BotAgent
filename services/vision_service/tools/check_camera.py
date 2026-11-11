#!/usr/bin/env python3
"""相机采集验证 —— 不需要 YOLO, 只验证能不能拿到帧。

用法::

    conda run -n yolo python tools/check_camera.py

会依次检查:
1. 能否枚举到 Orbbec 设备
2. 能否打开数据流
3. 能否拿到彩色帧 + 深度帧
4. 内参是否有效
5. 深度数值是否合理 (非 0, 在量程内)

会存一张彩色图到 logs/ 供肉眼确认。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from camera import CameraError, OrbbecCamera  # noqa: E402
from config import load_config  # noqa: E402

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


def fail(desc: str, detail: str = "") -> None:
    """直接记一次失败 (不经过 check)。"""
    global FAILED
    FAILED += 1
    print(f"  [FAIL] {desc}  {detail}")


def main() -> int:
    print("\n[1] 相机枚举与采集")

    cfg = load_config()
    cam = OrbbecCamera(cfg.camera)

    try:
        cam.open()
    except CameraError as exc:
        print(f"  [FAIL] 打开相机失败: {exc}")
        print()
        print("  排查:")
        print("    1. pip 装的 pyorbbecsdk 没有 USB 后端 -> 用源码编译的库")
        print("       (见 docs/vision-service.md)")
        print("    2. 检查 LD_LIBRARY_PATH 是否指向编译产物")
        return 1

    check("相机已打开", cam.opened)
    info = cam.device_info
    print(f"       设备: {info.get('name')} SN={info.get('serial_number')} "
          f"pid={info.get('pid')}")
    check("拿到设备信息", bool(info.get("name")), str(info))
    check("枚举到 >=1 个设备", int(info.get("device_count", 0)) >= 1)

    # ---- 内参 ----
    intr = cam.intrinsics
    check("内参有效", intr.valid, intr.to_dict())
    check("fx/fy 为正", intr.fx > 0 and intr.fy > 0, f"fx={intr.fx} fy={intr.fy}")
    check("主点在图像内",
          0 < intr.cx < intr.width and 0 < intr.cy < intr.height,
          f"cx={intr.cx} cy={intr.cy} size={intr.width}x{intr.height}")

    # ---- 采帧 ----
    frame = None
    for _ in range(5):
        try:
            frame = cam.read()
        except CameraError as exc:
            print(f"  [FAIL] 采帧出错: {exc}")
            fail("采帧出错", str(exc))
            break
        if frame is not None:
            break
    check("采到帧", frame is not None)
    if frame is None:
        cam.close()
        return 1

    if frame.color is not None:
        check("彩色帧有数据", frame.color.size > 0,
              f"{frame.color.shape}")
        check("彩色帧是 BGR 三通道",
              frame.color.ndim == 3 and frame.color.shape[2] == 3,
              str(frame.color.shape))
        check("彩色帧不是全黑",
              int(frame.color.max()) > 10,
              f"max={int(frame.color.max())}")
    else:
        check("拿到彩色帧", False)

    if frame.depth_raw is not None:
        check("深度帧有数据", frame.depth_raw.size > 0,
              f"{frame.depth_raw.shape}")
        # 65535 是 SDK 的「无效深度」标记, 要排除掉
        valid = frame.depth_raw[(frame.depth_raw > 0) & (frame.depth_raw < 65535)]
        check("深度有非零值", valid.size > 0,
              f"有效像素 {valid.size}/{frame.depth_raw.size}")
        if valid.size:
            meters = valid.astype(np.float32) * frame.depth_scale
            print(f"       深度范围: {meters.min():.3f} ~ {meters.max():.3f} 米")
            check("深度在合理范围 (0.05~5m)",
                  0.05 <= float(meters.min()) and float(meters.max()) <= 5.0,
                  f"{meters.min():.3f}~{meters.max():.3f}")
        # depth_scale 是「原始值 -> 米」的系数
        check("depth_scale 合理 (0.0001~0.01)",
              0.0001 < frame.depth_scale < 0.01,
              str(frame.depth_scale))
    else:
        check("拿到深度帧", False)

    print(f"       对齐模式: {cam.align_mode}")

    # ---- 存图 ----
    if frame.color is not None:
        try:
            import cv2

            out = Path(__file__).resolve().parents[3] / "logs" / "vision_color.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(out), frame.color)
            print(f"       彩色图已存: {out}")
        except Exception as exc:
            print(f"       (存图失败: {exc})")

    cam.close()
    check("相机已关闭", not cam.opened)

    print(f"\n通过 {PASSED}, 失败 {FAILED}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
