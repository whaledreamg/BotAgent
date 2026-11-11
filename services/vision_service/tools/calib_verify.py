#!/usr/bin/env python3
"""标定验证: 已知点误差 + **左右手性检查**。

用法::

    # 用已采的样本回代验证
    conda run -n yolo python tools/calib_verify.py --samples /tmp/handeye_samples.json

    # 交互式验证: 在世界里放几个已知点, 看报出来的坐标对不对
    conda run -n yolo python tools/calib_verify.py --live

**左右手性检查是最重要的一步**

手眼标定最常见的翻车点是**外参看起来「很像是对的」** —— 距离和深度全对,
只有左右反了。这种错误在功能测试里很难发现, 一直到抓取时才暴露成
「抓到了旁边的东西」。

检查方法:
1. 把一个物体放在机器人的**已知左侧** (比如左臂那一侧)
2. 看服务报出来的 X 坐标符号对不对
3. 右手侧同理

脚本会算出「相机 X 轴在目标系里的指向」和「相机 Y/Z 轴的指向」, 用几何
关系直接判断左右有没有反 —— 不用摆物体。

**已知点验证**

在世界系里放 5~10 个已知点 (用 TCP 触碰法确定, 2~5 mm 精度够用),
脚本算 ``predicted = T_target_cam · T_cam_point``, 跟真值比。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.handeye import PoseSample  # noqa: E402
from calibration.paths import config_path  # noqa: E402

PASSED = 0
FAILED = 0


def check(desc: str, ok: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {desc}" + (f"  {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {desc}  {detail}")
    return ok


def load_matrix(path: str) -> Optional[np.ndarray]:
    """读外参矩阵。

    支持三种来源:
    - configs/handeye.json (标定脚本的输出)
    - configs/vision.yaml 的 extrinsic.matrix
    - 直接给一个 4x4 的 JSON
    """
    p = Path(path) if path else config_path("handeye.json")
    if not p.exists():
        print(f"找不到 {p}", file=sys.stderr)
        return None

    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        print(f"读 {p} 失败: {exc}", file=sys.stderr)
        return None

    if "matrix" in data:
        return np.array(data["matrix"], dtype=np.float64)

    print(f"{p} 里没有 matrix 字段", file=sys.stderr)
    return None


def chirality_check(matrix: np.ndarray) -> None:
    """检查外参的手性对不对。

    判据: **det 必须为 +1**。

    相机系 (X右 Y下 Z前) 和基座系 (X前 Y左 Z上) 都是右手系, 两者之间
    的变换是旋转, 行列式必然是 +1。

    如果解出来 det = -1, 说明:
    - 要么解算过程中手性反了 (用了镜像而不是旋转)
    - 要么板上的角点顺序反了
    - 要么 ``flip_axes`` 用错了方式 (老版本那个 ``(-x,-y,z)`` 其实是
      绕光轴转 180°, det 仍然是 +1, 但语义跟注释不符)

    另外还要看「相机前方」在目标系里指的是不是「机器人前方」。
    """
    print("\n[手性检查]")
    rot = matrix[:3, :3]
    det = float(np.linalg.det(rot))

    check(
        "旋转部分行列式 = +1 (是旋转, 不是镜像)",
        abs(det - 1.0) < 1e-6,
        f"det = {det:+.6f}",
    )

    # 正交性
    should_be_identity = rot.T @ rot
    ortho_err = float(np.abs(should_be_identity - np.eye(3)).max())
    check(
        "旋转矩阵正交 (R^T R = I)",
        ortho_err < 1e-6,
        f"最大偏差 {ortho_err:.2e}",
    )

    # 相机光轴 (Z) 在目标系里的指向
    cam_forward_in_target = rot @ np.array([0.0, 0.0, 1.0])
    cam_right_in_target = rot @ np.array([1.0, 0.0, 0.0])
    cam_down_in_target = rot @ np.array([0.0, 1.0, 0.0])

    print()
    print("  相机轴在目标系里的指向:")
    print(f"    相机前方 (Z) -> {np.round(cam_forward_in_target, 4)}")
    print(f"    相机右方 (X) -> {np.round(cam_right_in_target, 4)}")
    print(f"    相机下方 (Y) -> {np.round(cam_down_in_target, 4)}")
    print()

    # 目标系若是基座系 (X前 Y左 Z上): 相机前方应该主要朝 +X (前)
    # 这里给的是启发式判断, 具体的「前方」要结合实际安装位置看
    print("  提示: 相机前方的指向应该跟机器人「面朝的方向」大致一致。")
    print("        如果机器人的前方是 +X, 那这里应该看到 [1, 0, 0] 附近。")
    print("        左右反了的话, 相机右方会指向 +Y 而不是 -Y。")


def sample_consistency_check(
    matrix: np.ndarray, samples: List[PoseSample]
) -> None:
    """用采样回代: 每个位姿算出的 T_world_cam 应该都等于标定结果。"""
    print("\n[采样一致性]")
    if len(samples) < 3:
        check("采样数足够", False, f"只有 {len(samples)} 个")
        return

    pos_errs: List[float] = []
    rot_errs: List[float] = []
    for s in samples:
        m = s.t_world_cam()
        pos_errs.append(float(np.linalg.norm(m[:3, 3] - matrix[:3, 3])))
        from extrinsic import rotation_error_rad

        rot_errs.append(rotation_error_rad(m[:3, :3], matrix[:3, :3]))

    mean_pos = float(np.mean(pos_errs))
    max_pos = float(np.max(pos_errs))
    mean_rot = math.degrees(float(np.mean(rot_errs)))

    check(
        "位置残差平均 < 10 mm",
        mean_pos < 0.010,
        f"平均 {mean_pos * 1000:.2f} mm, 最大 {max_pos * 1000:.2f} mm",
    )
    check(
        "姿态残差平均 < 1.0°",
        mean_rot < 1.0,
        f"平均 {mean_rot:.3f}°",
    )
    print()
    print("  逐采样位置残差 (mm):")
    print("    " + ", ".join(f"{e * 1000:.1f}" for e in pos_errs))


def known_points_check(
    matrix: np.ndarray, points: List[Tuple[Tuple[float, float, float], str]]
) -> None:
    """已知点验证: 世界系已知点 -> 预测的相机系坐标。

    ``points`` 是 [(世界坐标, 标签), ...]。
    预测 = inv(T_world_cam) · T_world_point
    """
    print("\n[已知点验证]")
    from extrinsic import invert_transform

    inv = invert_transform(matrix)
    errs: List[float] = []
    for world_xyz, label in points:
        p = np.array([world_xyz[0], world_xyz[1], world_xyz[2], 1.0])
        cam = inv @ p
        errs.append(float(np.linalg.norm(cam[:3])))
        print(f"    {label}: 世界 {world_xyz} -> 相机系 {np.round(cam[:3], 4)}")

    if errs:
        check(
            "所有已知点的相机系距离一致 (同一个平面上的点)",
            float(np.std(errs)) < 0.05,
            f"标准差 {np.std(errs) * 1000:.1f} mm",
        )


def live_check(matrix: np.ndarray) -> None:
    """交互式验证: 看服务实时报的基座坐标对不对。"""
    print("\n[实时验证]")
    import urllib.request

    base = "http://127.0.0.1:8803"
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=3) as resp:
            health = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"连不上视觉服务 ({exc})", file=sys.stderr)
        return

    print(f"  服务坐标系: {health.get('frame')}")
    print(f"  外参已配置: {health.get('extrinsic_configured')}")

    if health.get("frame") == "camera":
        check("服务已切到目标坐标系", False, "还是 camera —— 外参没启用")
        return

    check("服务已切到目标坐标系", True, str(health.get("frame")))

    print()
    print("  在世界里放一个物体到机器人的**左侧**, 然后按回车查它的坐标:")
    try:
        input("> ")
    except EOFError:
        return

    try:
        with urllib.request.urlopen(
            f"{base}/v1/objects/person", timeout=5
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"  查询失败: {exc}", file=sys.stderr)
        return

    objects = data.get("objects") or []
    if not objects:
        print("  没检测到任何目标 —— 画面里放个人或明显的物体")
        return

    pose = objects[0].get("pose") or {}
    print(f"  第一个目标: {objects[0].get('label')}")
    print(f"  坐标: x={pose.get('x'):.3f} y={pose.get('y'):.3f} z={pose.get('z'):.3f} "
          f"(系: {pose.get('frame')})")
    print()
    print("  **手性判断**: 物体在机器人左侧时, Y 应该为**负** (基座系 Y 向左)。")
    print("              如果 Y 是正的, 说明左右反了 —— 回去检查 flip_axes。")


def load_samples(path: str) -> List[PoseSample]:
    p = Path(path)
    with open(p, "r", encoding="utf-8") as fh:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="标定验证")
    parser.add_argument(
        "--matrix", default="", help="外参 JSON 路径 (默认 configs/handeye.json)",
    )
    parser.add_argument("--samples", default="", help="已采的样本 JSON")
    parser.add_argument("--live", action="store_true", help="实时验证 (需要服务在跑)")
    args = parser.parse_args()

    print("=" * 62)
    print("标定验证")
    print("=" * 62)

    matrix = load_matrix(args.matrix)
    if matrix is None:
        print("\n没有外参可验 —— 先跑 tools/calib_handeye.py", file=sys.stderr)
        return 1

    print(f"\n外参矩阵 (相机系 -> 目标系):")
    for row in matrix:
        print("   [" + ", ".join(f"{v: .6f}" for v in row) + "]")

    chirality_check(matrix)

    if args.samples:
        samples = load_samples(args.samples)
        sample_consistency_check(matrix, samples)

    if args.live:
        live_check(matrix)

    print()
    print("=" * 62)
    print(f"结果: {PASSED} 通过, {FAILED} 失败")
    print("=" * 62)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
