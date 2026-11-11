#!/usr/bin/env python3
"""把标定结果写回配置文件。

用法::

    # 内参 -> configs/vision.yaml 的 camera.calibrated_intrinsics
    conda run -n yolo python tools/apply_calibration.py --intrinsics

    # 外参 -> configs/vision.yaml 的 extrinsic
    conda run -n yolo python tools/apply_calibration.py --handeye

    # 两个都写
    conda run -n yolo python tools/apply_calibration.py --all

    # 只打印, 不改文件 (先看看会改成什么)
    conda run -n yolo python tools/apply_calibration.py --all --dry-run

**为什么用脚本写回而不是手抄**: 手抄 4x4 矩阵容易看错行、漏小数点, 而且
改完不知道改的是哪一版。脚本写回会留一份备份, 并且在写完后自动跑一次
自检确认服务能正常加载。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.paths import config_path  # noqa: E402

VISION_YAML = config_path("vision.yaml")
INTRINSICS_JSON = config_path("intrinsics.json")
HANDEYE_JSON = config_path("handeye.json")


def load_yaml(path: Path) -> Dict[str, Any]:
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def dump_yaml(data: Dict[str, Any], path: Path, dry_run: bool) -> None:
    import yaml

    text = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    if dry_run:
        print(text)
        return

    # 备份
    backup = path.with_suffix(path.suffix + f".bak.{int(time.time())}")
    shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"已写入 {path} (备份: {backup.name})")


def apply_intrinsics(dry_run: bool) -> bool:
    """把标定内参写进 vision.yaml。"""
    if not INTRINSICS_JSON.exists():
        print(f"找不到 {INTRINSICS_JSON} —— 先跑 tools/calib_intrinsics.py",
              file=sys.stderr)
        return False

    with open(INTRINSICS_JSON, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    k = data.get("camera_matrix") or []
    if len(k) != 3 or len(k[0]) != 3:
        print("intrinsics.json 里的 camera_matrix 不是 3x3", file=sys.stderr)
        return False

    config = load_yaml(VISION_YAML)
    vision = config.setdefault("vision", {})
    camera = vision.setdefault("camera", {})

    width, height = data.get("image_size", [0, 0])

    camera["calibrated_intrinsics"] = {
        "fx": round(float(k[0][0]), 6),
        "fy": round(float(k[1][1]), 6),
        "cx": round(float(k[0][2]), 6),
        "cy": round(float(k[1][2]), 6),
        "width": int(width or 0),
        "height": int(height or 0),
    }

    print("标定内参:")
    print(f"  fx={camera['calibrated_intrinsics']['fx']:.4f} "
          f"fy={camera['calibrated_intrinsics']['fy']:.4f}")
    print(f"  cx={camera['calibrated_intrinsics']['cx']:.4f} "
          f"cy={camera['calibrated_intrinsics']['cy']:.4f}")
    print(f"  分辨率 {width}x{height}")

    dump_yaml(config, VISION_YAML, dry_run)
    return True


def apply_handeye(dry_run: bool) -> bool:
    """把标定外参写进 vision.yaml。"""
    if not HANDEYE_JSON.exists():
        print(f"找不到 {HANDEYE_JSON} —— 先跑 tools/calib_handeye.py",
              file=sys.stderr)
        return False

    with open(HANDEYE_JSON, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    matrix = data.get("matrix") or []
    if len(matrix) != 4:
        print("handeye.json 里的 matrix 不是 4x4", file=sys.stderr)
        return False

    config = load_yaml(VISION_YAML)
    vision = config.setdefault("vision", {})
    extrinsic = vision.setdefault("extrinsic", {})

    extrinsic["enabled"] = True
    extrinsic["matrix"] = [[round(float(v), 8) for v in row] for row in matrix]
    # 轴置换已经烘进 matrix 了 —— 不要再额外翻转一次
    extrinsic["flip_axes"] = False

    # 标定出来的是「相机 -> 世界系」 (眼在手外, 板挂工具) ——
    # 所以 matrix_frame 一定是 world。
    # **注意**: 如果头以后会动, 应该改成 matrix_frame=head 并重新标
    # T_head_cam (见 docs/calibration.md)。
    extrinsic["matrix_frame"] = "world"
    # 默认输出到世界系 (矩阵本来描述的就是相机->世界)
    extrinsic["target_frame"] = "world"
    extrinsic.setdefault("arm", "auto")
    # 需要动态补偿 (拿臂基座/头位姿) 时置 true
    extrinsic["dynamic"] = bool(extrinsic.get("dynamic", False))

    print("标定外参 (相机系 -> 目标系):")
    for row in extrinsic["matrix"]:
        print("   [" + ", ".join(f"{v: .6f}" for v in row) + "]")
    print()
    print(f"标定坐标系 (matrix_frame): {extrinsic['matrix_frame']}")
    print(f"目标坐标系 (target_frame): {extrinsic['target_frame']}")
    print(f"位置 RMS: {data.get('rms_position_m', 0) * 1000:.2f} mm")
    print(f"姿态 RMS: {data.get('rms_rotation_rad', 0):.4f} rad")

    if extrinsic["target_frame"] in ("left_base", "right_base", "arm_base"):
        print()
        print("提示: 目标是臂基座系 —— 需要 dynamic: true, "
              "运行时从 arm_service 拿基座位姿。")
        if not extrinsic["dynamic"]:
            print("      当前 dynamic=false, 请手动改成 true。")

    dump_yaml(config, VISION_YAML, dry_run)
    return True


def verify() -> None:
    """写回之后确认服务能正常加载配置。"""
    try:
        from config import load_config

        cfg = load_config()
        print()
        print("配置加载 OK:")
        print(f"  标定内参: {'有' if cfg.camera.calibrated_intrinsics else '无'}")
        print(f"  外参启用: {cfg.extrinsic.enabled}")
        print(f"  标定坐标系: {cfg.extrinsic.matrix_frame}")
        print(f"  目标坐标系: {cfg.extrinsic.target_frame}")
        print(f"  动态补偿: {cfg.extrinsic.dynamic}")
    except Exception as exc:
        print(f"配置加载失败: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="把标定结果写回配置")
    parser.add_argument("--intrinsics", action="store_true", help="写回内参")
    parser.add_argument("--handeye", action="store_true", help="写回外参")
    parser.add_argument("--all", action="store_true", help="两个都写")
    parser.add_argument("--dry-run", action="store_true", help="只打印, 不改文件")
    args = parser.parse_args()

    do_intr = args.intrinsics or args.all
    do_hand = args.handeye or args.all

    if not do_intr and not do_hand:
        print("要么 --intrinsics, 要么 --handeye, 要么 --all", file=sys.stderr)
        return 1

    print("=" * 62)
    print("写回标定结果")
    print("=" * 62)
    print()

    ok = True
    if do_intr:
        print("[内参]")
        ok = apply_intrinsics(args.dry_run) and ok
        print()
    if do_hand:
        print("[外参]")
        ok = apply_handeye(args.dry_run) and ok
        print()

    if not args.dry_run and ok:
        verify()
        print()
        print("下一步: 重启视觉服务, 然后跑 tools/calib_verify.py --live 做手性检查")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
