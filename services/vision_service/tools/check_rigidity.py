#!/usr/bin/env python3
"""刚体自检: 一眼判断标定数据是「晃动」还是「系统性错误」。

**为什么要有这个工具**

手眼标定失败时, 最耗时间的不是解算, 是**判断该不该重采**。而这两件事
的代价差好几倍:

- 数据只是**晃动** (板没锁死)  -> 多采几帧, 或者接受现有精度
- 数据是**系统性错误** (坐标系约定错了 / 位姿读数错) -> 重采 20 个姿态
  一样会失败, 必须先查清楚

这两种情况在「残差很大」这个现象上**长得一模一样**。本工具用三个统计量
把它们区分开。

**判据**

手眼标定的核心假设是**板刚性固定在工具上**:

    T_world_cam = T_world_tool · T_tool_board · T_board_cam

所以在每帧都成立:

    T_cam_board · T_world_tool = T_cam_world · T_tool_board = (常数)

也就是「板在相机系里的位姿」乘上「工具在世界系里的位姿」应该**每帧都一样**。
实测偏差就是这个乘积的帧间差异。

三个统计量:

1. **偏差幅度** —— 相对姿态差多少度
2. **偏差轴一致性** —— 偏差绕的是不是**同一个轴**
3. **偏差与工具转角的相关性** —— 偏差是不是跟着工具转角走

**怎么读结果**

| 现象 | 含义 | 该怎么办 |
|------|------|---------|
| 偏差小 (< 2°), 轴一致 | 正常 | 继续 |
| 偏差大, **轴高度一致**, 与工具转角强相关 | **系统性错误** | 别重采! 查坐标系约定 |
| 偏差大, 轴随机, 与工具转角无关 | 晃动 / 坏帧 | 重采, 或挑好帧 |

第 2 行是重点: **偏差锁在一个固定轴上, 且跟着工具转角走** —— 这是
「坐标系之间缺了一个变换」的典型特征, 摆多少次姿态都不会变好。

用法::

    conda run -n yolo python tools/check_rigidity.py --samples /tmp/handeye_samples.json
    conda run -n yolo python tools/check_rigidity.py --samples /tmp/handeye_samples.json --matrix configs/handeye.json
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

from calibration.handeye import PoseSample  # noqa: E402
from calibration.paths import config_path  # noqa: E402

#: 相对姿态偏差的「可接受」上限 (度)。超过就认为装夹不可靠。
TOLERANCE_DEG = 2.0

#: 偏差轴一致性的判定阈值 (各帧偏差轴与平均轴的平均点积)
AXIS_CONSISTENCY = 0.7

#: 与工具转角的相关性阈值
CORRELATION = 0.6


def _angle_deg(a: np.ndarray, b: np.ndarray) -> float:
    """两个旋转矩阵之间的夹角 (度)。"""
    cos_theta = (np.trace(a.T @ b) - 1.0) / 2.0
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_theta))))


def _rotation_axis(r: np.ndarray) -> Optional[np.ndarray]:
    """从旋转矩阵里取旋转轴 (单位向量)。"""
    w = np.array(
        [
            r[2, 1] - r[1, 2],
            r[0, 2] - r[2, 0],
            r[1, 0] - r[0, 1],
        ],
        dtype=np.float64,
    )
    n = float(np.linalg.norm(w))
    if n < 1e-12:
        return None
    return w / n


def _load_samples(path: str) -> Tuple[List[PoseSample], Optional[np.ndarray]]:
    """读采样, 顺便把工具偏置取出来。

    支持两种格式:
    - **session 格式** (``{"samples": [...]}`` 或裸数组) —— 用
      :func:`calibration.session.load_session`
    - **探针格式** (``{"records": [{"t_world_tool":..., "t_cam_board":...}]}``)
      —— :mod:`tools.probe_convention` 存的
    """
    raw_path = Path(path)
    try:
        with open(raw_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as exc:
        print(f"读 {raw_path} 失败: {exc}", file=sys.stderr)
        return [], None

    # ---- 探针格式 ----
    if isinstance(raw, dict) and "records" in raw:
        from calibration.handeye import PoseSample

        samples: List[PoseSample] = []
        for rec in raw["records"]:
            try:
                samples.append(
                    PoseSample(
                        t_cam_board=np.asarray(rec["t_cam_board"], dtype=np.float64),
                        t_world_tool=np.asarray(rec["t_world_tool"], dtype=np.float64),
                    )
                )
            except (KeyError, ValueError):
                continue
        return samples, None

    # ---- session 格式 ----
    from calibration.session import load_session

    session = load_session(raw_path)
    offset = None
    for s in session.samples:
        tb = np.asarray(s.t_tool_board, dtype=np.float64)
        if tb.shape == (4, 4) and not np.allclose(tb, np.eye(4)):
            offset = tb
            break
    return session.samples, offset


def analyze(
    samples: List[PoseSample],
) -> Optional[dict]:
    """算刚体一致性统计量。"""
    if len(samples) < 3:
        return None

    # P_i = T_cam_board · inv(T_world_tool)  (刚体条件下应恒定)
    mats = []
    for s in samples:
        mats.append(np.asarray(s.t_cam_board) @ _invert(np.asarray(s.t_world_tool)))

    ref = mats[0]
    rot_devs: List[float] = []
    axes: List[np.ndarray] = []
    for m in mats[1:]:
        rel = ref[:3, :3].T @ m[:3, :3]
        rot_devs.append(_angle_deg(ref[:3, :3], m[:3, :3]))
        ax = _rotation_axis(rel)
        if ax is not None:
            axes.append(ax)

    # **偏差轴要在同一个坐标系里比**。
    #
    # 上面算的轴是「在工具系里」的 —— 但每一帧的工具朝向不同, 所以同一个
    # 物理误差在不同帧会显示出不同的轴 (看起来很随机, 其实是一个轴)。
    # 要把它换算到**共同的参考系** (这里用第 0 帧的相机系) 才能比。
    #
    # 换算: rel_i = R_ref^T · R_i, 它的轴在 R_ref 表达。把轴向量也乘上
    # R_ref 就到了共同系里。
    ref_R = mats[0][:3, :3]
    axes_common: List[np.ndarray] = []
    for m in mats[1:]:
        rel = ref_R.T @ m[:3, :3]
        ax = _rotation_axis(rel)
        if ax is not None:
            axes_common.append(ref_R @ ax)

    # 平移部分
    trans = np.array([m[:3, 3] for m in mats])

    # 工具自身的转角变化 (用来算相关性)
    r0 = np.asarray(samples[0].t_world_tool)[:3, :3]
    tool_devs = np.array(
        [_angle_deg(r0, np.asarray(s.t_world_tool)[:3, :3]) for s in samples]
    )

    axis_consistency = 0.0
    mean_axis = np.zeros(3)
    if axes_common:
        arr = np.array(axes_common)
        m = arr.mean(0)
        n = float(np.linalg.norm(m))
        if n > 1e-9:
            mean_axis = m / n
            axis_consistency = float((arr @ mean_axis).mean())

    corr = 0.0
    if len(rot_devs) > 2:
        a = np.asarray(rot_devs)
        b = tool_devs[1:]
        if a.std() > 1e-9 and b.std() > 1e-9:
            corr = float(np.corrcoef(a, b)[0, 1])

    # **旋转自洽性** —— 与平移分开看, 因为两者可能只有一个有问题
    #
    # AX=ZB 的旋转部分: R_ai · R_x = R_c · R_ci
    # 用闭式解把 R_c / R_x 求出来, 算每帧的旋转残差。
    # 这一项独立于平移, 所以能区分「旋转错了」还是「平移错了」。
    rot_resid: List[float] = []
    rot_ok = None
    try:
        from calibration.handeye import closed_form_axzb

        c_est, x_est = closed_form_axzb(samples)
        for s in samples:
            lhs = np.asarray(s.t_world_tool)[:3, :3] @ x_est[:3, :3]
            rhs = c_est[:3, :3] @ np.asarray(s.t_cam_board)[:3, :3]
            rot_resid.append(_angle_deg(lhs, rhs))
        rot_ok = float(np.mean(rot_resid))
    except Exception:
        pass

    return {
        "rot_devs": rot_devs,
        "tool_devs": tool_devs,
        "trans": trans,
        "axis_consistency": axis_consistency,
        "mean_axis": mean_axis,
        "corr": corr,
        "max_rot_dev": max(rot_devs) if rot_devs else 0.0,
        "mean_rot_dev": float(np.mean(rot_devs)) if rot_devs else 0.0,
        "trans_std": trans.std(0),
        "trans_span": trans.max(0) - trans.min(0),
        "rot_resid": rot_resid,
        "rot_resid_mean": rot_ok,
    }


def _invert(m: np.ndarray) -> np.ndarray:
    """4x4 刚体变换求逆。"""
    r = np.asarray(m, dtype=np.float64)[:3, :3]
    t = np.asarray(m, dtype=np.float64)[:3, 3]
    out = np.eye(4)
    out[:3, :3] = r.T
    out[:3, 3] = -r.T @ t
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="刚体自检 —— 判断标定数据是晃动还是系统性错误",
    )
    parser.add_argument("--samples", required=True, help="session JSON 路径")
    parser.add_argument(
        "--matrix",
        default="",
        help="外参 JSON (可选, 用于对照解出的 tool_board 是否恒定)",
    )
    args = parser.parse_args()

    bar = "=" * 62
    print(f"{bar}\n刚体自检\n{bar}\n")

    samples, offset = _load_samples(args.samples)
    print(f"采样数: {len(samples)}")
    if offset is not None:
        print(f"session 里的工具偏置 (非单位阵): {np.round(offset[:3, 3], 4)}")
    else:
        print("session 里的工具偏置: 单位阵 (占位值, 不是解出来的真偏置)")

    stats = analyze(samples)
    if stats is None:
        print("\n采样太少, 至少要 3 个", file=sys.stderr)
        return 1

    print()
    print("[1] 相对姿态偏差  (T_cam_board · inv(T_world_tool), 应恒定)")
    print("    " + ", ".join(f"{d:.1f}" for d in stats["rot_devs"]))
    print(f"    平均 {stats['mean_rot_dev']:.2f}°   最大 {stats['max_rot_dev']:.2f}°"
          f"   (容差 {TOLERANCE_DEG}°)")

    print()
    print("[2] 平移一致性")
    print(f"    标准差 {np.round(stats['trans_std'], 4)} m")
    print(f"    跨度   {np.round(stats['trans_span'], 4)} m")

    print()
    print("[3] 偏差轴一致性  (系统性错误 -> 偏差锁在同一个轴上)")
    ax = stats["mean_axis"]
    print(f"    平均偏差轴 = ({ax[0]:+.3f}, {ax[1]:+.3f}, {ax[2]:+.3f})")
    print("    (在**相机系**里 —— 相机系: X 向右 / Y 向下 / Z 沿光轴向前)")
    print(f"    各帧与平均轴的点积均值 = {stats['axis_consistency']:.3f}"
          f"   (阈值 {AXIS_CONSISTENCY})")

    print()
    print("[4] 偏差与工具转角的相关性  (系统性错误 -> 偏差跟着工具转角走)")
    print(f"    相关系数 = {stats['corr']:+.3f}   (阈值 {CORRELATION})")

    # ---- 旋转 vs 平移 分开判 ----
    print()
    print("[5] 旋转 / 平移 分开诊断  (哪个坏了?)")
    rr = stats.get("rot_resid_mean")
    if rr is not None:
        rot_ok = rr < 2.0
        print(f"    AX=ZB 旋转残差: 平均 {rr:.2f}°  最大 "
              f"{max(stats['rot_resid']):.2f}°  -> 旋转 "
              f"{'自洽' if rot_ok else '不自洽'}")
    else:
        rot_ok = False
        print("    旋转残差: 算不出来")
    trans_ok = stats["trans_std"].max() < 0.030
    print(f"    平移残差标准差: {stats['trans_std'].max() * 1000:.1f} mm  -> 平移 "
          f"{'自洽' if trans_ok else '不自洽'}")

    # ---- 装置诊断 (不需要外参) ----
    print()
    print("[6] 装置诊断  (这些判据**不需要外参**, 采集现场也能用)")
    from calibration.diagnostics import (
        displacement_ratio,
        relative_rotation,
        rotation_excitation,
    )

    tool_poses = [s.t_world_tool for s in samples]
    board_poses = [s.t_cam_board for s in samples]

    per_axis, min_exc = rotation_excitation(tool_poses)
    exc_ok = min_exc >= 15.0
    print(f"    旋转激励度: 绕各轴最大旋转 X={per_axis['axis_x']:.1f}° "
          f"Y={per_axis['axis_y']:.1f}° Z={per_axis['axis_z']:.1f}°")
    print(f"                 最小 {min_exc:.1f}° (要求 >= 15°) -> "
          f"{'够' if exc_ok else '**不够 —— 旋转欠约束, 外参会解得出来但是错的**'}")

    rel = relative_rotation(tool_poses, board_poses)
    if rel:
        worst = max(rel)
        print(f"    板相对工具转动: 最大 {worst:.3f}° (容差 0.5°) -> "
              f"{'正常' if worst <= 0.5 else '**板在工具上滑动/摆动**'}")
        print(f"      逐帧: {', '.join(f'{r:.3f}' for r in rel)}")

    ratios = displacement_ratio(tool_poses, board_poses)
    if ratios:
        vals = [r for _, r in ratios]
        med = float(np.median(vals))
        print(f"    位移尺度比 (板位移/工具位移): 中位 {med:.4f} "
              f"(应为 1.000, 容差 5%) -> "
              f"{'正常' if abs(med - 1.0) <= 0.05 else '**位置通道有尺度偏差**'}")
        print(f"      逐帧: {', '.join(f'[{i}]{r:.3f}' for i, r in ratios)}")
        if abs(med - 1.0) > 0.05:
            print(f"      => 板实物尺寸可能是标称的 {med:.3f} 倍"
                  f" (用尺子量相邻角点间距; 标称 40mm)")
            print(f"      => 也可能是 TCP 配成了法兰而不是夹爪")
    else:
        print("    位移尺度比: 无数据 (工具转动太大, 无法用纯平移比较)")

    # ---------------- 判定 ----------------
    print()
    print(f"{bar}")
    print("判定")
    print(f"{bar}")

    wobble = (
        stats["max_rot_dev"] > TOLERANCE_DEG
        and stats["axis_consistency"] < AXIS_CONSISTENCY
        and abs(stats["corr"]) < CORRELATION
    )
    systematic = (
        stats["max_rot_dev"] > TOLERANCE_DEG
        and (
            stats["axis_consistency"] >= AXIS_CONSISTENCY
            or abs(stats["corr"]) >= CORRELATION
        )
    )

    # 装置诊断的结果也要参与判定 —— 光看 [1] 的偏差会漏掉很多问题
    # (比如纯平移采集时, 旋转残差天然好看, 但旋转其实欠约束)
    from calibration.diagnostics import (
        displacement_ratio,
        relative_rotation,
        rotation_excitation,
    )

    _tool_poses = [s.t_world_tool for s in samples]
    _board_poses = [s.t_cam_board for s in samples]
    _per_axis, _min_exc = rotation_excitation(_tool_poses)
    _rel = relative_rotation(_tool_poses, _board_poses)
    _ratios = displacement_ratio(_tool_poses, _board_poses)
    _rigid_ok = (not _rel) or max(_rel) <= 0.5
    _exc_ok = _min_exc >= 15.0
    _scale_ok = (
        not _ratios
        or abs(float(np.median([r for _, r in _ratios])) - 1.0) <= 0.05
    )

    if stats["max_rot_dev"] <= TOLERANCE_DEG and _rigid_ok:
        print("  [OK] 刚体条件成立 —— 板相对工具是固定的。")
        if not _exc_ok:
            print("       但**旋转激励不足** —— 见下面「下一步怎么办」。")
        else:
            print("       数据可以用。")
    elif systematic:
        print("  [!!] 这是**系统性错误**, 不是晃动。")
        print()
        print("       依据:")
        if stats["axis_consistency"] >= AXIS_CONSISTENCY:
            print(f"       - 偏差轴高度一致 (点积 {stats['axis_consistency']:.3f})"
                  " —— 偏差绕同一个固定轴")
        if abs(stats["corr"]) >= CORRELATION:
            print(f"       - 偏差与工具转角强相关 (r={stats['corr']:+.3f})"
                  " —— 工具转多少, 板就「偏」多少")
        print()
        if rot_ok and not trans_ok:
            print("       **旋转是自洽的, 平移不自洽** —— 问题集中在**位置**通道:")
            print("         - 控制器的 TCP 位置 (cartesian.x/y/z) 不是世界系?")
            print("         - 或者单位是 mm 而不是 m?")
            print("         - 或者 TCP 原点定义和标定假设的不一样")
        elif not rot_ok and trans_ok:
            print("       **平移是自洽的, 旋转不自洽** —— 问题集中在**姿态**通道:")
            print("         - 控制器的 cartesian.a/b/c 不是标准 RPY")
            print("         - 或者 Z 轴的朝向定义不一致")
        else:
            print("       旋转和平移都不自洽 —— 两个通道都有问题")
        print()
        print("       **重采数据不会解决这个。** 摆 20 次姿态还是同样结果。")
        print(f"         偏差轴 = ({ax[0]:+.3f}, {ax[1]:+.3f}, {ax[2]:+.3f})"
              " —— 接近哪个坐标轴, 就是哪处错了")
    elif wobble:
        print("  [~] 偏差大, 但**轴随机、与工具转角无关** —— 这是晃动 / 坏帧。")
        print("       对策: 剔除坏帧后重解, 或重采 (加装夹具把板锁死)。")
    elif not _rigid_ok:
        print("  [!!] **板没有刚性固定在工具上** —— 这不是「晃动」能解释的。")
        print(f"       板相对工具转了 {max(_rel):.2f}° (工具只转了 0.001°)。")
        print("       重采前必须先把板锁死 —— 否则采多少个姿态都一样。")
    else:
        print("  [?] 偏差大, 特征不明确 —— 需要人工判断。")

    print()
    print(f"  最大相对姿态偏差 = {stats['max_rot_dev']:.2f}°")
    # 超过 90° 时 tan 会爆掉, 只在 <90° 时给「等效晃动」参考
    if stats["max_rot_dev"] < 90.0:
        print(f"  位置残差量级参考: 板装夹距离 ~340mm 时, {stats['max_rot_dev']:.1f}° "
              f"相当于 {340 * math.tan(math.radians(stats['max_rot_dev'])):.0f} mm 的晃动")
    else:
        print("  (偏差 > 90°, 不再换算成「等效晃动」—— 那已经不是晃动的量级了)")

    # ---------------- 行动建议 ----------------
    print()
    print(f"{bar}")
    print("下一步怎么办")
    print(f"{bar}")

    actions: List[str] = []
    if not exc_ok:
        actions.append(
            "**旋转激励不足** —— 重采时让臂绕**三个不同方向**转起来 (绕 X、绕 Y、绕 Z "
            "各转 20~40°), 别只 jog 平移。只平移的采集对旋转没有约束, "
            "外参会解得出来但是错的。"
        )
    if rel and max(rel) > 0.5:
        actions.append(
            f"**板相对工具转了 {max(rel):.2f}°** —— 板没刚性固定在夹爪上。"
            "用夹具/螺丝/双面胶把它锁死, 再重采。重采前先确认: "
            "你松手后板不会晃。"
        )
    if ratios:
        med = float(np.median([r for _, r in ratios]))
        if abs(med - 1.0) > 0.05:
            actions.append(
                f"**位置通道尺度偏差 {med:.3f}** —— 重采前先做这两件事:\n"
                "         1. 用尺子量板上**相邻两个 ChArUco 内角点**的距离。"
                f"当前标称 40mm, 若量出 {40 * med:.1f}mm 就改 "
                "configs/calibration.yaml 的 square_length\n"
                "         2. 查夹爪的 TCP 配置 —— 是不是配成了法兰而不是夹爪尖端"
            )
    if not actions:
        actions.append("装置诊断全部通过 —— 可以直接用这份数据解算。")

    for a in actions:
        print(f"  * {a}")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
