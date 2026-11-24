#!/usr/bin/env python3
"""单轴旋转探针: 定位「坐标系约定」错在哪。

**背景**

手眼标定的刚体自检 (:mod:`tools/check_rigidity`) 发现偏差锁在**一个固定轴**
上、且与工具转角强相关 —— 这是坐标系约定的系统性错误, 不是晃动。

但「约定错了」有好几种可能:

- 控制器的 ``cartesian.a/b/c`` 不是标准 RPY (ZYX 内旋)
- 相机系和世界系的轴向定义不同 (相机 Z 前 / 基座 Z 上)
- 世界系本身多绕了一个轴

**怎么定位**

只动**一个**关节, 让 TCP 绕**一个**已知的世界系轴转固定角度, 然后看:

- 控制器给的 ``cartesian.a/b/c`` 怎么变
- 图像里板怎么转
- 两者能不能对上

一次只转一个轴, 转 30~40°, 采 3 帧就够。这样每种假设都能被单独证伪。

**用法**

需要机械臂和相机能配合。脚本会:

1. 读当前 TCP 位姿
2. 提示你把臂转到某个姿态 (每次转一点)
3. 每按一次回车采一帧, 记录 ``a/b/c`` + 板在相机系的姿态
4. 结束时打印分析: 各约定下的预测 vs 实测

用法::

    conda run --no-capture-output -n yolo python tools/probe_convention.py
    conda run --no-capture-output -n yolo python tools/probe_convention.py --count 5
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.board import BoardSpec, detect_charuco  # noqa: E402
from calibration.capture import (  # noqa: E402
    ArmKinematicsClient,
    CameraCapture,
    capture_sample,
    tool_pose_stable,
)
from calibration.paths import config_path  # noqa: E402


def _angle_deg(a: np.ndarray, b: np.ndarray) -> float:
    cos_theta = (np.trace(a.T @ b) - 1.0) / 2.0
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_theta))))


def _rot_axis(r: np.ndarray) -> Optional[np.ndarray]:
    w = np.array(
        [r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]],
        dtype=np.float64,
    )
    n = float(np.linalg.norm(w))
    return w / n if n > 1e-12 else None


def _rx(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float64)


def _ry(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float64)


def _rz(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)


AXES = {"x": _rx, "y": _ry, "z": _rz}

#: 各种 RPY 约定: R = AX[a](first) · AX[b](second) · AX[c](third)
CONVENTIONS = {
    "ZYX (当前用的, 标准RPY)": ("z", "y", "x"),
    "ZYX (abc倒序)": ("z", "y", "x"),
    "XYZ": ("x", "y", "z"),
    "XZY": ("x", "z", "y"),
    "YXZ": ("y", "x", "z"),
    "YZX": ("y", "z", "x"),
    "ZXZ": ("z", "x", "z"),
    "ZYZ": ("z", "y", "z"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="坐标系约定探针")
    parser.add_argument("--count", type=int, default=4, help="采几帧 (默认 4)")
    parser.add_argument("--arm", default="", help="用哪条臂 (显式指定更好)")
    parser.add_argument("--arm-url", default="http://127.0.0.1:8802")
    parser.add_argument("--out", default="/tmp/convention_probe.json")
    parser.add_argument(
        "--analyze",
        default="",
        help="只分析已保存的探针文件 (不连相机/机械臂)",
    )
    args = parser.parse_args()

    bar = "=" * 62
    print(f"{bar}\n坐标系约定探针\n{bar}\n")

    # ---- 只分析已存的文件 ----
    if args.analyze:
        data = json.loads(Path(args.analyze).read_text(encoding="utf-8"))
        return _analyze(data.get("records", []), data.get("arm", "?"))

    from calibration.interactive import require_interactive_stdin

    if not require_interactive_stdin("tools/probe_convention.py"):
        return 2

    # ---- 配置 ----
    from config import load_config

    cfg = load_config()

    spec = BoardSpec(
        rows=5, cols=7, square_length=0.040,
        marker_length=0.030, dictionary="DICT_4X4_50",
    )

    intr_path = config_path("intrinsics.json")
    K = D = None
    if intr_path.exists():
        d = json.loads(intr_path.read_text(encoding="utf-8"))
        K = np.array(d["camera_matrix"], dtype=np.float64)
        D = np.array(d["dist_coeffs"], dtype=np.float64).ravel()
        print(f"内参: fx={K[0, 0]:.2f} fy={K[1, 1]:.2f}")

    # ---- 相机 ----
    from camera import OrbbecCamera

    cam = OrbbecCamera(cfg.camera)
    try:
        cam.open()
    except Exception as exc:
        print(f"相机打不开: {exc}", file=sys.stderr)
        return 1
    print(f"相机: {cam.device_info.get('name', '?')}")

    capture = CameraCapture(cam)
    client = ArmKinematicsClient(base_url=args.arm_url)

    snap = client.snapshot(refresh=True)
    if not snap:
        print("连不上 arm_service", file=sys.stderr)
        cam.close()
        return 1

    arm = ArmKinematicsClient.pick_arm(snap, args.arm)
    if not arm:
        print("找不到可用的臂", file=sys.stderr)
        cam.close()
        return 1
    print(f"使用臂: {arm}")
    print()

    print("=" * 62)
    print("怎么用这个探针")
    print("=" * 62)
    print("""
只动**一个**关节, 让夹爪绕**一个**已知的世界系轴转 ~30~40°, 每转一点
按一次回车采一帧。板要始终在画面里清晰可见。

**关键**: 每次只转一个轴。比如只动 J1 (肩部), 板就该绕某个世界系轴转。
如果板转了 30° 但控制器 a/b/c 变化很小 -> 约定不对。

采完会打印: 各种 RPY 约定下, 预测的板转角 vs 实测的板转角,
最接近的那个约定就是对的。
""")

    records: List[dict] = []
    try:
        for i in range(args.count):
            print(f"\n[{i + 1}/{args.count}] 摆好新姿态 (只转一个轴), 按回车采样, "
                  f"输入 q 退出")
            try:
                key = input("> ")
            except EOFError:
                print("\n  stdin 结束, 中止", file=sys.stderr)
                break
            if key.strip().lower() in ("q", "quit", "exit"):
                break

            pose = tool_pose_stable(client, arm, tolerance_m=0.002, max_wait_s=15.0)
            if pose is None:
                print("  等停稳超时, 跳过", file=sys.stderr)
                continue

            sample = capture_sample(
                capture, spec, pose,
                camera_matrix=K, dist_coeffs=D,
                min_corners=8, label=arm,
            )
            if sample is None:
                print("  没采到有效样本, 跳过", file=sys.stderr)
                continue

            # 原始的 cartesian a/b/c
            snap2 = client.snapshot(refresh=True)
            raw_abc = None
            if snap2:
                cart = ((snap2.get("arms") or {}).get(arm) or {}).get("cartesian")
                if isinstance(cart, dict):
                    raw_abc = [cart.get("a"), cart.get("b"), cart.get("c")]

            records.append({
                "index": len(records),
                "raw_abc": raw_abc,
                "t_world_tool": np.asarray(pose).tolist(),
                "t_cam_board": np.asarray(sample.t_cam_board).tolist(),
                "corner_count": sample.corner_count,
            })
            print(f"  采到: {sample.corner_count} 个角点, "
                  f"a/b/c = {raw_abc}")

        # 存图
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {"arm": arm, "records": records},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n已保存: {out}")

    finally:
        cam.close()

    if len(records) < 2:
        print("\n采的帧不够, 无法分析", file=sys.stderr)
        return 1

    return _analyze(records, arm)


def _analyze(records: List[dict], arm: str = "?") -> int:
    """分析探针数据 (纯离线, 不需要相机/机械臂)。"""
    # ---------------- 分析 ----------------
    print()
    print("=" * 62)
    print(f"分析  (臂: {arm})")
    print("=" * 62)

    ref = records[0]
    R_wt0 = np.array(ref["t_world_tool"])[:3, :3]
    R_cb0 = np.array(ref["t_cam_board"])[:3, :3]
    abc0 = ref["raw_abc"]

    print(f"\n参考帧 (第 0 帧) 的 a/b/c = {abc0}")
    print()
    print("每帧相对第 0 帧的变化:")
    print(f"{'帧':>3} {'Δa':>8} {'Δb':>8} {'Δc':>8} {'工具转角':>9} {'板转角':>9}")
    tool_devs: List[float] = []
    board_devs: List[float] = []
    for rec in records:
        R_wt = np.array(rec["t_world_tool"])[:3, :3]
        R_cb = np.array(rec["t_cam_board"])[:3, :3]
        td = _angle_deg(R_wt0, R_wt)
        bd = _angle_deg(R_cb0, R_cb)
        tool_devs.append(td)
        board_devs.append(bd)
        abc = rec["raw_abc"]
        if abc and abc0:
            da = (abc[0] - abc0[0]) * 180 / math.pi
            db = (abc[1] - abc0[1]) * 180 / math.pi
            dc = (abc[2] - abc0[2]) * 180 / math.pi
            print(f"{rec['index']:>3} {da:>8.2f} {db:>8.2f} {dc:>8.2f} "
                  f"{td:>9.2f} {bd:>9.2f}")
        else:
            print(f"{rec['index']:>3} {'-':>8} {'-':>8} {'-':>8} "
                  f"{td:>9.2f} {bd:>9.2f}")

    # 板相对工具的偏差 (应该恒定)
    #
    # **乘法顺序很关键**: 刚体时 T_cam_board(i) · inv(T_world_tool(i)) 恒定。
    # 写成 t_cam_board @ t_world_tool (少了逆) 会得到完全错误的结论。
    print()
    print("[刚体条件] 板在相机系的位姿 x inv(工具在世界系的位姿) —— 应恒定")
    mats = []
    for r in records:
        t = np.array(r["t_world_tool"])
        r_mat = t[:3, :3]
        inv4 = np.eye(4)
        inv4[:3, :3] = r_mat.T
        inv4[:3, 3] = -r_mat.T @ t[:3, 3]
        mats.append(np.array(r["t_cam_board"]) @ inv4)
    devs = [_angle_deg(mats[0][:3, :3], m[:3, :3]) for m in mats[1:]]
    print("    " + ", ".join(f"{d:.2f}°" for d in devs))
    if devs and max(devs) < 0.5:
        print("    [OK] 恒定 —— 板刚性固定在工具上")
    elif devs and max(devs) < 2.0:
        print("    [~] 偏差不大 (小于 2°)")
    else:
        print("    [!!] 不恒定 —— 板没有刚性固定在工具上, 或者约定有问题")

    # ---- 装置诊断 (不需要外参) ----
    print()
    print("[装置诊断]  (这三个判据不需要外参)")
    from calibration.diagnostics import (
        displacement_ratio,
        relative_rotation,
        rotation_excitation,
    )

    tool_poses = [np.array(r["t_world_tool"]) for r in records]
    board_poses = [np.array(r["t_cam_board"]) for r in records]

    per_axis, min_exc = rotation_excitation(tool_poses)
    print(f"    旋转激励度: X={per_axis['axis_x']:.1f}° "
          f"Y={per_axis['axis_y']:.1f}° Z={per_axis['axis_z']:.1f}°"
          f"   最小 {min_exc:.1f}° (要求 >= 15°)")
    if min_exc < 15.0:
        print("      -> **旋转欠约束** —— 这批数据对旋转没有约束, "
              "解出来的外参会「看着对但其实是错的」。"
              "重采时要绕三个轴都转起来。")

    rel = relative_rotation(tool_poses, board_poses)
    if rel:
        print(f"    板相对工具转动: 最大 {max(rel):.3f}° (容差 0.5°)")
        if max(rel) > 0.5:
            print("      -> **板在工具上滑动/摆动** —— 重采前先把板锁死")

    ratios = displacement_ratio(tool_poses, board_poses)
    if ratios:
        med = float(np.median([r for _, r in ratios]))
        print(f"    位移尺度比 (板位移/工具位移): 中位 {med:.4f} "
              f"(应为 1.000, 容差 5%)")
        if abs(med - 1.0) > 0.05:
            print(f"      -> **位置通道有尺度偏差** —— 板实物尺寸可能是标称的 "
                  f"{med:.3f} 倍 (量一下相邻角点间距)")
            print(f"      -> 也可能是 TCP 配成了法兰而不是夹爪")
    else:
        print("    位移尺度比: 无数据 (工具转动太大, 无法用纯平移比较)")

    # 偏差轴
    if len(mats) > 1:
        axes = []
        for m in mats[1:]:
            ax = _rot_axis(mats[0][:3, :3].T @ m[:3, :3])
            if ax is not None:
                axes.append(ax)
        if axes:
            arr = np.array(axes)
            mean = arr.mean(0)
            n = np.linalg.norm(mean)
            if n > 1e-9:
                mean /= n
                print()
                print(f"[偏差轴] 平均 = ({mean[0]:+.3f}, {mean[1]:+.3f}, "
                      f"{mean[2]:+.3f})")
                print(f"          一致性 (点积均值) = {(arr @ mean).mean():.3f}")
                # 跟三个基轴比 —— 这是**向量夹角**, 直接点积就行
                #
                # 注意: 偏差轴是在**相机系**里表达的 (T_cam_board 天然在相机系),
                # 不是基座系。相机系: X 向右, Y 向下, Z 沿光轴向前。
                print("          (相机系: X 右 / Y 下 / Z 前)")
                for k, v in (("X", [1, 0, 0]), ("Y", [0, 1, 0]), ("Z", [0, 0, 1])):
                    va = np.asarray(v, dtype=np.float64)
                    ang = math.degrees(math.acos(
                        max(-1.0, min(1.0, float(mean @ va)))
                    ))
                    print(f"          与 {k} 轴夹角 = {ang:.1f}°")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
