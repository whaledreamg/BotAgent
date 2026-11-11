#!/usr/bin/env python3
"""手眼标定 (eye-to-hand): 板挂工具, 用控制器正解当量尺。

用法::

    # 采 20 个位姿并解外参, 结果写到 configs/handeye.json
    conda run -n yolo python tools/calib_handeye.py --capture

    # 换个臂 (双胞胎要分别标)
    conda run -n yolo python tools/calib_handeye.py --capture --arm right

    # 从已采的样本重解 (不重新采)
    conda run -n yolo python tools/calib_handeye.py --samples /tmp/handeye_samples.json

**前置条件**:
1. 内参已标定 (tools/calib_intrinsics.py) —— 不然精度上不去
2. ChArUco 板**固定在工具上** (用打印模板对齐, 或销钉定位)
3. 工具坐标系的**物理尖点**与板原点重合 (或用 --tool-offset 填偏置)
4. arm_service 在跑 (它提供控制器的世界系位姿)

**采样要点**:
- 20~30 个位姿, 在工作空间里**姿态多样化** (三个轴各 ±30° 以上)
- 不要都在一个平面附近 —— 旋转不够多样, 解不出的旋转轴
- 避开: 7 轴冗余导致的肘部翻转、奇异位形、关节软限位附近
- 板与相机的距离要在深度相机有效范围内 (Gemini 336L 约 0.2 m 起)
- 每个位姿要**停稳**再采 (脚本会等)

**重要**: 相机装在机器人**头部**时, 如果头部可以转动, 静态外参不成立。
先确认头部是固定的再往下做。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.board import BoardSpec  # noqa: E402
from calibration.capture import (  # noqa: E402
    ArmKinematicsClient,
    CameraCapture,
    capture_sample,
)
from calibration.handeye import PoseSample, solve_handeye  # noqa: E402
from calibration.paths import config_path  # noqa: E402


def load_spec() -> BoardSpec:
    path = config_path("calibration.yaml")
    if not path.exists():
        return BoardSpec()
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        section = data.get("vision_calibration", data)
        return BoardSpec.from_dict(section.get("board", {}))
    except Exception:
        return BoardSpec()


def load_intrinsics() -> Optional[Dict[str, Any]]:
    """读标定好的内参 (configs/intrinsics.json)。"""
    path = config_path("intrinsics.json")
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def load_tool_offset(path: Optional[str]) -> np.ndarray:
    """读板相对工具的偏置 (4x4)。

    如果板原点与工具物理尖点重合, 就是单位阵。
    """
    if not path:
        return np.eye(4, dtype=np.float64)
    p = Path(path)
    if not p.exists():
        print(f"找不到 {p}, 用单位阵", file=sys.stderr)
        return np.eye(4, dtype=np.float64)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        m = np.array(data["matrix"], dtype=np.float64)
        if m.shape != (4, 4):
            raise ValueError("不是 4x4")
        return m
    except Exception as exc:
        print(f"读 tool offset 失败 ({exc}), 用单位阵", file=sys.stderr)
        return np.eye(4, dtype=np.float64)


def save_samples(samples: List[PoseSample], path: Path) -> None:
    """把采样存成 JSON (可复现、可重新解算)。"""
    data = [
        {
            "t_cam_board": s.t_cam_board.tolist(),
            "t_world_tool": s.t_world_tool.tolist(),
            "t_tool_board": s.t_tool_board.tolist(),
            "obj_points": None if s.obj_points is None else s.obj_points.tolist(),
            "img_points": None if s.img_points is None else s.img_points.tolist(),
            "corner_count": s.corner_count,
            "label": s.label,
        }
        for s in samples
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print(f"采样已存到 {path}")


def load_samples(path: Path) -> List[PoseSample]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    out: List[PoseSample] = []
    for item in data:
        obj = item.get("obj_points")
        img = item.get("img_points")
        out.append(
            PoseSample(
                t_cam_board=np.array(item["t_cam_board"], dtype=np.float64),
                t_world_tool=np.array(item["t_world_tool"], dtype=np.float64),
                t_tool_board=np.array(item["t_tool_board"], dtype=np.float64),
                obj_points=None if obj is None else np.array(obj, dtype=np.float64),
                img_points=None if img is None else np.array(img, dtype=np.float64),
                corner_count=int(item.get("corner_count", 0)),
                label=str(item.get("label", "")),
            )
        )
    return out


def capture_samples(
    count: int,
    arm: str,
    arm_url: str,
    tool_offset: np.ndarray,
    dist_coeffs: Optional[np.ndarray],
) -> List[PoseSample]:
    """交互式采样。"""
    from camera import OrbbecCamera
    from config import load_config

    cfg = load_config()
    cam = OrbbecCamera(cfg.camera)
    print("正在打开相机...", file=sys.stderr)
    try:
        cam.open()
    except Exception as exc:
        print(f"相机打不开: {exc}", file=sys.stderr)
        return []

    capture = CameraCapture(cam)
    client = ArmKinematicsClient(arm_url)

    # 用标定好的内参 (如果有)
    camera_matrix = capture.camera_matrix()
    intr = load_intrinsics()
    if intr:
        try:
            camera_matrix = np.array(intr["camera_matrix"], dtype=np.float64)
            if dist_coeffs is None:
                dist_coeffs = np.array(intr["dist_coeffs"], dtype=np.float64)
            print(
                f"用标定内参: fx={camera_matrix[0, 0]:.2f} "
                f"fy={camera_matrix[1, 1]:.2f}",
                file=sys.stderr,
            )
        except Exception as exc:
            print(f"读标定内参失败 ({exc}), 用出厂内参", file=sys.stderr)

    # 先确认臂名
    snap = client.snapshot(refresh=True)
    if not snap:
        print("连不上 arm_service, 退出", file=sys.stderr)
        cam.close()
        return []
    chosen = client.pick_arm(snap, arm)
    if not chosen:
        print("arm_service 里没有机器人", file=sys.stderr)
        cam.close()
        return []
    print(f"使用臂: {chosen}", file=sys.stderr)

    samples: List[PoseSample] = []
    try:
        for i in range(count):
            print(
                f"\n[{i + 1}/{count}] 把机械臂摆到新位姿 (姿态要多样), "
                f"按回车采样, 输入 q 退出",
                file=sys.stderr,
            )
            try:
                key = input("> ")
            except EOFError:
                break
            if key.strip().lower() in ("q", "quit", "exit"):
                break

            print("  等机械臂停稳...", file=sys.stderr)
            tool_pose = _wait_stable(client, chosen)
            if tool_pose is None:
                print("  没等到停稳, 跳过", file=sys.stderr)
                continue

            sample = capture_sample(
                capture,
                load_spec(),
                tool_pose,
                camera_matrix=camera_matrix,
                dist_coeffs=dist_coeffs,
                label=chosen,
            )
            if sample is None:
                continue
            sample.t_tool_board = tool_offset
            samples.append(sample)
    finally:
        cam.close()

    return samples


def _wait_stable(client: ArmKinematicsClient, arm: str):
    from calibration.capture import tool_pose_stable

    return tool_pose_stable(client, arm, tolerance_m=0.002, max_wait_s=15.0)


def report(result: Any) -> None:
    """打印标定质量报告。"""
    print()
    print("=" * 62)
    print("手眼标定结果")
    print("=" * 62)
    print(f"参与解算的采样: {result.sample_count}")
    print(
        f"位置残差: 平均 {result.mean_position_error * 1000:.1f} mm, "
        f"最大 {result.max_position_error * 1000:.1f} mm"
    )
    print(
        f"位置 RMS: {result.rms_position * 1000:.2f} mm "
        f"(精化前 {result.rms_position_before_refine * 1000:.2f} mm)"
    )
    print(f"姿态残差: 平均 {math.degrees(result.mean_rotation_error):.3f}°")
    if result.rejected:
        print(f"剔除的野值采样: {result.rejected}")
    print()
    print("外参矩阵 (相机系 -> 目标系):")
    for row in result.matrix:
        print("   [" + ", ".join(f"{v: .6f}" for v in row) + "]")

    print()
    print("验收标准 (1.5 m 工作距离):")
    pos_ok = result.rms_position < 0.010
    rot_ok = result.mean_rotation_error < math.radians(1.0)
    print(
        f"  [{'PASS' if pos_ok else 'FAIL'}] 位置 RMS < 10 mm "
        f"(实测 {result.rms_position * 1000:.2f} mm)"
    )
    print(
        f"  [{'PASS' if rot_ok else 'FAIL'}] 姿态残差 < 1.0° "
        f"(实测 {math.degrees(result.mean_rotation_error):.3f}°)"
    )
    print()
    print("下一步:")
    print("  1. 跑 tools/calib_verify.py 做**左右手性检查** (最常见的翻车点)")
    print("  2. 把矩阵写进 configs/vision.yaml 的 extrinsic.matrix")
    print("  3. 把 extrinsic.enabled 设为 true, flip_axes 设为 false")
    print("=" * 62)


def main() -> int:
    parser = argparse.ArgumentParser(description="手眼标定")
    parser.add_argument("--capture", action="store_true", help="交互式采样并解算")
    parser.add_argument("--samples", type=str, default="", help="从已采样本解算")
    parser.add_argument("--count", type=int, default=20, help="采样数 (默认 20)")
    parser.add_argument(
        "--arm", default="",
        help="用哪条臂: left / right / 控制器里的机器人名。留空 = 自动选",
    )
    parser.add_argument(
        "--arm-url", default="http://127.0.0.1:8802", help="arm_service 地址",
    )
    parser.add_argument(
        "--tool-offset", type=str, default="",
        help="板相对工具的 4x4 偏置 JSON (默认单位阵)",
    )
    parser.add_argument(
        "--samples-out", default="/tmp/handeye_samples.json", help="采样保存路径",
    )
    parser.add_argument(
        "--out", default="", help="标定结果 JSON (默认 configs/handeye.json)",
    )
    args = parser.parse_args()

    tool_offset = load_tool_offset(args.tool_offset or None)

    samples: List[PoseSample] = []
    if args.capture:
        samples = capture_samples(
            args.count,
            args.arm,
            args.arm_url,
            tool_offset,
            None,
        )
        if samples:
            save_samples(samples, Path(args.samples_out))
    elif args.samples:
        p = Path(args.samples)
        if not p.exists():
            print(f"找不到 {p}", file=sys.stderr)
            return 1
        samples = load_samples(p)
        print(f"从 {p} 读到 {len(samples)} 个采样")
    else:
        print("要么用 --capture 采样, 要么用 --samples 读已有采样", file=sys.stderr)
        return 1

    if len(samples) < 3:
        print(f"只有 {len(samples)} 个采样, 至少要 3 个", file=sys.stderr)
        return 1

    print(f"\n解算外参 ({len(samples)} 个采样)...")

    # 精化要用**固定**的内参 —— 先单独标内参, 再拿它解外参。
    # 边解边估会让内外参互相补偿, 外参就不准了。
    camera_matrix = None
    dist_coeffs = None
    intr = load_intrinsics()
    if intr:
        try:
            camera_matrix = np.array(intr["camera_matrix"], dtype=np.float64)
            dist_coeffs = np.array(intr["dist_coeffs"], dtype=np.float64)
            print(
                f"用标定内参做精化: fx={camera_matrix[0, 0]:.2f} "
                f"fy={camera_matrix[1, 1]:.2f}",
                file=sys.stderr,
            )
        except Exception as exc:
            print(f"读标定内参失败 ({exc}), 只做稳健平均", file=sys.stderr)

    result = solve_handeye(
        samples,
        refine=camera_matrix is not None,
        camera_matrix=camera_matrix,
        dist_coeffs=dist_coeffs,
    )
    if result is None:
        print("解算失败 —— 检查采样数量和多样性", file=sys.stderr)
        return 1

    report(result)

    out = Path(args.out) if args.out else config_path("handeye.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, ensure_ascii=False, indent=2)
    print(f"\n结果已保存到 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
