"""标定「装置」诊断 —— 采集时就能发现装夹/坐标/尺度的问题。

**为什么需要这个**

手眼标定失败时, 最耗时间的不是解算, 是**判断该不该重采**。而重采
一次要半小时, 采完还不知道有没有用。

这几种失败在「残差很大」这个现象上**长得一模一样**:

| 病 | 症状 | 重采有用吗 |
|----|------|-----------|
| 板没锁死 (相对转动) | 残差大 | 有用, 但要先锁紧 |
| 位置通道尺度错 | 残差大 | **没用** —— 摆多少个姿态都一样 |
| 坐标系约定错 | 残差大 | **没用** |
| 旋转激励不足 | 残差看着小, 但外参错 | **没用** —— 数据看着 OK 其实没约束 |

本模块提供几个**不依赖最终外参**的判据, 可以在采集过程中就实时检查:

1. :func:`rotation_excitation` —— 旋转激励度
   纯平移的采集对旋转几乎没有约束, 外参会「解得出来」但是错的。
2. :func:`relative_rotation` —— 板相对工具的转动
   与外参无关: ``inv(T_world_tool) · T_cam_board`` 的旋转部分, 各帧
   之间应该恒定。不恒定 = 板在工具上滑动/摆动。
3. :func:`displacement_ratio` —— 位移模长比
   与外参无关: 纯平移时 ``|板位移|`` 应该等于 ``|工具位移|``。比值
   系统性偏离 1 = 位置通道有尺度问题 (板尺寸错 / TCP 配错 / fx 错)。

**关键设计**: 前两个判据**不需要相机外参**, 所以能在采集现场用。
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

import numpy as np

#: 旋转激励度下限 —— 绕每个轴的旋转量至少要这么多度
ROTATION_EXCITATION_MIN_DEG = 15.0

#: 相对转动容差 (度) —— 板相对工具的转动超过这个就是装夹不牢
RELATIVE_ROTATION_TOL_DEG = 0.5

#: 位移模长比容差 —— 偏离 1 超过这个比例就是位置通道有问题
DISPLACEMENT_RATIO_TOL = 0.05


def _invert(m: np.ndarray) -> np.ndarray:
    """4x4 刚体变换求逆。"""
    arr = np.asarray(m, dtype=np.float64)
    r = arr[:3, :3]
    t = arr[:3, 3]
    out = np.eye(4)
    out[:3, :3] = r.T
    out[:3, 3] = -r.T @ t
    return out


def rotation_angle_deg(a: np.ndarray, b: np.ndarray) -> float:
    """两个旋转矩阵之间的夹角 (度)。"""
    ra = np.asarray(a, dtype=np.float64)[:3, :3]
    rb = np.asarray(b, dtype=np.float64)[:3, :3]
    cos_theta = (np.trace(ra.T @ rb) - 1.0) / 2.0
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_theta))))


def rotation_excitation(
    tool_poses: Sequence[np.ndarray],
) -> Tuple[Dict[str, float], float]:
    """算采集数据的**旋转激励度**。

    **为什么重要**: 手眼标定的旋转部分靠「工具在不同朝向下, 板在相机系
    里的朝向也随之改变」来约束。如果采集时只做了平移 (比如只 jog 一个
    轴), 旋转就是**欠约束**的 —— 解算照样收敛, 残差看着也漂亮, 但外参
    是错的。

    返回 ``(各轴旋转量, 最小激励度)``。各轴旋转量的算法: 把每帧相对
    第 0 帧的旋转增量投影到世界系三个基轴上, 取各轴分量的绝对值最大。
    """
    if len(tool_poses) < 2:
        return {f"axis_{k}": 0.0 for k in ("x", "y", "z")}, 0.0

    r0 = np.asarray(tool_poses[0], dtype=np.float64)[:3, :3]
    deltas = []
    for pose in tool_poses[1:]:
        r = np.asarray(pose, dtype=np.float64)[:3, :3]
        # 相对旋转 r0^T · r, 取旋转向量
        rel = r0.T @ r
        w = np.array(
            [
                rel[2, 1] - rel[1, 2],
                rel[0, 2] - rel[2, 0],
                rel[1, 0] - rel[0, 1],
            ],
            dtype=np.float64,
        )
        n = float(np.linalg.norm(w))
        if n > 1e-12:
            # 旋转向量在**世界系**里的表达 (r0 · w_hat * angle)
            axis = r0 @ (w / n)
            deltas.append(axis * math.degrees(n))
        else:
            deltas.append(np.zeros(3))

    if not deltas:
        return {f"axis_{k}": 0.0 for k in ("x", "y", "z")}, 0.0

    arr = np.abs(np.array(deltas))
    per_axis = {
        "axis_x": float(arr[:, 0].max()),
        "axis_y": float(arr[:, 1].max()),
        "axis_z": float(arr[:, 2].max()),
    }
    return per_axis, float(min(per_axis.values()))


def relative_rotation(
    tool_poses: Sequence[np.ndarray],
    board_poses: Sequence[np.ndarray],
) -> List[float]:
    """板相对工具的转动 (度) —— **不需要外参**。

    **正确的乘法顺序是 ``T_cam_board · inv(T_world_tool)``, 不是反过来。**

    推导: 刚体时 ``T_world_board(i) = A_i · B`` (B = 板相对工具),
    而 ``T_cam_board(i) = C_world_cam^T · A_i · B``, 所以::

        T_cam_board(i) · inv(A_i) = C_world_cam^T · B   <- 恒定!

    反过来 ``inv(A_i) · T_cam_board(i) = A_i^T · C^T · A_i · B``
    **不恒定** (A_i 出现在中间)。实测: 物理刚性的合成数据上,
    前者给 0.0000°, 后者给 8~30° —— 顺序写反会得到完全错误的结论。

    返回每帧相对第 0 帧的偏差 (第 0 帧为 0)。
    """
    if len(tool_poses) < 2 or len(tool_poses) != len(board_poses):
        return []

    mats = [
        np.asarray(b, dtype=np.float64) @ _invert(np.asarray(t, dtype=np.float64))
        for t, b in zip(tool_poses, board_poses)
    ]
    r0 = mats[0][:3, :3]
    return [rotation_angle_deg(r0, m[:3, :3]) for m in mats]


def displacement_ratio(
    tool_poses: Sequence[np.ndarray],
    board_poses: Sequence[np.ndarray],
) -> List[Tuple[int, float]]:
    """纯平移时的位移模长比 —— **不需要外参**。

    相机不动、工具和板刚性连接时, 工具在**世界系**平移的模长, 应该
    等于板在**相机系**平移的模长 (旋转保模长, 与坐标系无关)。

    比值系统性地偏离 1 = 位置通道有**尺度**问题:
    - 板的实物尺寸和标称不符 (打印缩放)
    - 控制器的 TCP 配的是法兰而不是夹爪
    - 内参 fx/fy 偏大

    只对「工具转动很小」的帧对有意义 (转动会引入额外的弧线位移)。
    返回 ``[(帧索引, 比值), ...]``。
    """
    if len(tool_poses) < 2 or len(tool_poses) != len(board_poses):
        return []

    out: List[Tuple[int, float]] = []
    r0t = np.asarray(tool_poses[0], dtype=np.float64)[:3, :3]
    r0b = np.asarray(board_poses[0], dtype=np.float64)[:3, :3]
    for i in range(1, len(tool_poses)):
        rt = np.asarray(tool_poses[i], dtype=np.float64)[:3, :3]
        rb = np.asarray(board_poses[i], dtype=np.float64)[:3, :3]
        # 只在工具几乎没转时比较
        if rotation_angle_deg(r0t, rt) > 2.0:
            continue
        dt = (np.asarray(tool_poses[i], dtype=np.float64)[:3, 3]
              - np.asarray(tool_poses[0], dtype=np.float64)[:3, 3])
        db = (np.asarray(board_poses[i], dtype=np.float64)[:3, 3]
              - np.asarray(board_poses[0], dtype=np.float64)[:3, 3])
        nd, nb = float(np.linalg.norm(dt)), float(np.linalg.norm(db))
        if nd < 1e-5 or nb < 1e-5:
            continue
        out.append((i, nb / nd))
    return out


def report(
    tool_poses: Sequence[np.ndarray],
    board_poses: Sequence[np.ndarray],
) -> List[str]:
    """生成一份人类可读的问题清单 (空列表 = 一切正常)。"""
    problems: List[str] = []

    # ---- 旋转激励度 ----
    per_axis, min_exc = rotation_excitation(tool_poses)
    if min_exc < ROTATION_EXCITATION_MIN_DEG:
        problems.append(
            f"旋转激励不足: 绕各轴的最大旋转量 "
            f"X={per_axis['axis_x']:.1f}° Y={per_axis['axis_y']:.1f}° "
            f"Z={per_axis['axis_z']:.1f}° (每个轴至少要 "
            f"{ROTATION_EXCITATION_MIN_DEG:.0f}°)。"
            "旋转欠约束时外参会「解得出来但是错的」——"
            "务必让臂绕**三个不同方向**转起来, 别只 jog 平移。"
        )

    # ---- 相对转动 ----
    rel = relative_rotation(tool_poses, board_poses)
    if rel:
        worst = max(rel)
        if worst > RELATIVE_ROTATION_TOL_DEG:
            problems.append(
                f"板相对工具转了 {worst:.2f}° (容差 "
                f"{RELATIVE_ROTATION_TOL_DEG}°)。"
                "板没有刚性固定在夹爪上 —— 运动中在滑动/摆动。"
                "请用夹具/螺丝把板锁死, 否则重采也没用。"
            )

    # ---- 位移尺度 ----
    ratios = displacement_ratio(tool_poses, board_poses)
    if ratios:
        vals = [r for _, r in ratios]
        med = float(np.median(vals))
        if abs(med - 1.0) > DISPLACEMENT_RATIO_TOL:
            problems.append(
                f"位置通道有尺度偏差: 板位移/工具位移 = {med:.3f} "
                f"(应为 1.000, 容差 {DISPLACEMENT_RATIO_TOL:.0%})。"
                + (
                    "板实物尺寸可能是标称的 "
                    f"{med:.2f} 倍 (用尺子量相邻角点间距)"
                    if med > 1.0
                    else "板实物尺寸可能是标称的 "
                    f"{med:.2f} 倍 (用尺子量相邻角点间距)"
                )
                + " —— 也可能是 TCP 配成了法兰而非夹爪。"
            )

    return problems
