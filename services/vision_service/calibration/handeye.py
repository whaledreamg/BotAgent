"""手眼标定 (eye-to-hand): 相机固定在机器人上, 标定板挂在工具上。

**为什么是「板挂工具」而不是「板放桌上」**

控制器给的世界系位姿 (``robot/status.cartesian``) 精度约 1 mm —— 那是我们
唯一的「量尺」。所以板必须挂在一个位姿能被控制器算出来的物体上。

- 板挂工具: 板的世界位姿 = 控制器正解 → 毫米级。误差只剩「板相对工具的
  物理安装偏置」, 用机械定位 (销钉 / 打印模板对齐) 可以做到 < 1 mm。
- 板放桌上: 板的世界位姿靠 TCP 触碰确定, 重复性只有 2~5 mm, 直接进外参
  就把误差预算吃光了。

**数学**

每个采样位姿 i:

    T_world_cam = T_world_tool(i) · T_tool_board · T_board_cam(i)

其中:
- ``T_world_tool(i)``: 控制器给的 TCP 世界系位姿 (6D)
- ``T_tool_board``:    板相对工具的固定偏置 (4x4), 标定前量好, 通常是单位阵
- ``T_board_cam(i)``:  由 ``solvePnP`` 从板角点解出

多组位姿解出的 ``T_world_cam`` 取平均 -> 外参。

**精化**: 用所有位姿上板角点的**重投影误差**做 Gauss-Newton 最小化,
只优化外参的 6 个自由度 (3 平移 + 3 旋转)。

**为什么不用 scipy**: 相机跑在 conda ``yolo`` 环境里, 那个环境没装 scipy。
这里用 numpy 自己写数值 Jacobian 的 Gauss-Newton —— 6 个参数, 收敛很快,
不值得为它加一个依赖。

**手性检查** (最常见的翻车点): 标定结果看起来「很像是对的」—— 距离和
深度全对, 只有左右反了。所以标完必须做 ``tools/calib_verify.py`` 里的
左右手性检查。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger("vision.calib.handeye")


@dataclass
class PoseSample:
    """一个标定采样。

    - ``t_cam_board``:  相机系下板的位姿 (4x4), 由 solvePnP 解出
    - ``t_world_tool``: 世界系下工具 (TCP) 的位姿 (4x4), 控制器给的
    - ``t_tool_board``: 板相对工具的固定偏置 (4x4), 标定前量好
    - ``obj_points`` / ``img_points``: 板角点的 3D/2D 对应 (精化用)
    """

    t_cam_board: np.ndarray
    t_world_tool: np.ndarray
    t_tool_board: np.ndarray = field(
        default_factory=lambda: np.eye(4, dtype=np.float64)
    )
    #: 板上角点的 3D 坐标 (板系, 米) 与对应的 2D 像素
    obj_points: Optional[np.ndarray] = None
    img_points: Optional[np.ndarray] = None
    #: 这一帧检出的角点数 (角点少说明板被挡了, 数据质量差)
    corner_count: int = 0
    #: 备注 (比如 "左臂")
    label: str = ""

    def t_world_cam(self) -> np.ndarray:
        """这一帧推出来的「相机系 -> 世界系」变换。"""
        from ._compat import invert_transform

        # T_world_cam = T_world_tool · T_tool_board · T_board_cam
        t_board_cam = invert_transform(self.t_cam_board)
        return self.t_world_tool @ self.t_tool_board @ t_board_cam

    def t_world_board(self) -> np.ndarray:
        """板的世界系位姿 (由控制器正解 + 工具偏置得到)。"""
        return self.t_world_tool @ self.t_tool_board


@dataclass
class HandEyeResult:
    """手眼标定结果。"""

    #: 相机系 -> 目标系 的 4x4 齐次矩阵
    matrix: np.ndarray
    #: 参与解算的采样数
    sample_count: int = 0
    #: 每个采样的位置残差 (米)
    position_errors: List[float] = field(default_factory=list)
    #: 每个采样的姿态残差 (rad)
    rotation_errors: List[float] = field(default_factory=list)
    #: 整体 RMS 位置残差 (米)
    rms_position: float = 0.0
    #: 整体 RMS 姿态残差 (rad)
    rms_rotation: float = 0.0
    #: 被剔除的野值采样索引
    rejected: List[int] = field(default_factory=list)
    #: 精化前后对比 (给用户看精化有没有用)
    rms_position_before_refine: float = 0.0

    @property
    def mean_position_error(self) -> float:
        if not self.position_errors:
            return 0.0
        return float(np.mean(self.position_errors))

    @property
    def max_position_error(self) -> float:
        if not self.position_errors:
            return 0.0
        return float(np.max(self.position_errors))

    @property
    def mean_rotation_error(self) -> float:
        if not self.rotation_errors:
            return 0.0
        return float(np.mean(self.rotation_errors))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "matrix": [
                [round(float(v), 8) for v in row] for row in self.matrix
            ],
            "sample_count": self.sample_count,
            "rms_position_m": round(self.rms_position, 6),
            "rms_position_before_refine_m": round(self.rms_position_before_refine, 6),
            "rms_rotation_rad": round(self.rms_rotation, 6),
            "mean_position_error_m": round(self.mean_position_error, 6),
            "max_position_error_m": round(self.max_position_error, 6),
            "mean_rotation_error_rad": round(self.mean_rotation_error, 6),
            "rejected_samples": list(self.rejected),
        }


def solve_handeye(
    samples: Sequence[PoseSample],
    outlier_threshold: float = 0.02,
    refine: bool = False,
    camera_matrix: Optional[np.ndarray] = None,
    dist_coeffs: Optional[np.ndarray] = None,
) -> Optional[HandEyeResult]:
    """解手眼外参。

    ``outlier_threshold``: 位置残差超过这个值 (米) 的采样会被剔掉。
    取 0.02 m 是因为标定板物理安装误差本身就有 1~2 mm, 超过 2 cm 基本
    是采到了坏位姿 (板被挡 / TCP 位姿读数抖动)。

    ``camera_matrix`` / ``dist_coeffs``: 标定好的内参。给了就做**精化**
    (用重投影误差非线性优化), 没给就只做稳健平均。

    **为什么精化要「固定内参」**: 如果边解外参边估内参, 两者会互相补偿
    (内参偏大 -> 外参偏小), 结果是外参不准但残差好看。真实流程是
    先单独标内参 (``tools/calib_intrinsics.py``), 再拿固定内参解外参。
    """
    if len(samples) < 3:
        logger.error("采样太少 (%d), 至少要 3 个", len(samples))
        return None

    from ._compat import rotation_error_rad

    # ---- 1) 逐位姿算初值 ----
    candidates = [s.t_world_cam() for s in samples]

    # 位置用中位数 (抗野值)
    positions = np.array([m[:3, 3] for m in candidates], dtype=np.float64)
    median_pos = np.median(positions, axis=0)

    # 旋转没有中位数 —— 用「到其它旋转的角度和最小」的那个当参考
    rotations = [m[:3, :3] for m in candidates]
    best_idx, best_score = 0, float("inf")
    for i, r in enumerate(rotations):
        score = sum(
            rotation_error_rad(r, rr) for j, rr in enumerate(rotations) if j != i
        )
        if score < best_score:
            best_score, best_idx = score, i

    initial = np.eye(4, dtype=np.float64)
    initial[:3, :3] = rotations[best_idx]
    initial[:3, 3] = median_pos

    # ---- 2) 剔野值 ----
    kept: List[int] = []
    rejected: List[int] = []
    for i, m in enumerate(candidates):
        dp = float(np.linalg.norm(m[:3, 3] - initial[:3, 3]))
        dr = rotation_error_rad(m[:3, :3], initial[:3, :3])
        if dp > outlier_threshold or dr > math.radians(5.0):
            rejected.append(i)
            logger.info(
                "剔除采样 %d: 位置差 %.1f mm, 姿态差 %.2f°",
                i, dp * 1000, math.degrees(dr),
            )
        else:
            kept.append(i)

    if len(kept) < 3:
        logger.error("剔野值后只剩 %d 个采样, 无法解算", len(kept))
        return None

    kept_samples = [samples[i] for i in kept]
    kept_candidates = [candidates[i] for i in kept]

    # ---- 3) 精化 ----
    rms_before = _rms_position(kept_candidates, initial)
    matrix = initial
    if refine and camera_matrix is not None:
        matrix = _refine(kept_samples, initial, camera_matrix, dist_coeffs)
    elif refine:
        logger.info("没给内参, 跳过精化 (只做稳健平均)")

    # ---- 4) 算残差 ----
    pos_errs: List[float] = []
    rot_errs: List[float] = []
    for m in kept_candidates:
        pos_errs.append(float(np.linalg.norm(m[:3, 3] - matrix[:3, 3])))
        rot_errs.append(rotation_error_rad(m[:3, :3], matrix[:3, :3]))

    result = HandEyeResult(
        matrix=matrix,
        sample_count=len(kept),
        position_errors=pos_errs,
        rotation_errors=rot_errs,
        rms_position=float(np.sqrt(np.mean(np.square(pos_errs)))) if pos_errs else 0.0,
        rms_rotation=float(np.sqrt(np.mean(np.square(rot_errs)))) if rot_errs else 0.0,
        rejected=rejected,
        rms_position_before_refine=rms_before,
    )
    logger.info(
        "手眼标定完成: %d 个采样 (剔掉 %d 个), 位置 RMS=%.2f mm (精化前 %.2f mm), "
        "姿态 RMS=%.3f°",
        result.sample_count,
        len(rejected),
        result.rms_position * 1000,
        rms_before * 1000,
        math.degrees(result.rms_rotation),
    )
    return result


# ------------------------------------------------------------ 精化


def _rms_position(candidates: Sequence[np.ndarray], matrix: np.ndarray) -> float:
    errs = [
        float(np.linalg.norm(m[:3, 3] - matrix[:3, 3])) for m in candidates
    ]
    return float(np.sqrt(np.mean(np.square(errs)))) if errs else 0.0


def _refine(
    samples: Sequence[PoseSample],
    initial: np.ndarray,
    camera_matrix: np.ndarray,
    dist_coeffs: Optional[np.ndarray] = None,
) -> np.ndarray:
    """用重投影误差做 Gauss-Newton 精化。

    变量: 外参的 6 个自由度 (tx,ty,tz + 旋转向量)。

    残差: 每个采样上, 板的世界位姿已知 (控制器给的), 相机在世界系里的
    位姿是待优化量, 于是板在相机系里的位姿也已知 -> 把板角点投影到
    图像上, 与实测角点的差就是残差。

    **内参是固定的** —— 必须先单独标好内参再解外参。边解边估会让两者
    互相补偿, 外参就不准了。
    """
    usable = [
        s for s in samples
        if s.obj_points is not None and s.img_points is not None
    ]
    if len(usable) < 2:
        logger.warning("没有角点对应关系, 跳过精化")
        return initial

    intr = np.asarray(camera_matrix, dtype=np.float64)
    dist = None if dist_coeffs is None else np.asarray(
        dist_coeffs, dtype=np.float64
    ).ravel()

    params = _matrix_to_params(initial)

    def residuals(p: np.ndarray) -> np.ndarray:
        m = _params_to_matrix(p)
        out: List[float] = []
        for s in usable:
            t_cam_board = _board_pose_in_camera(m, s)
            if t_cam_board is None:
                continue
            rvec, tvec = _rodrigues(t_cam_board[:3, :3]), t_cam_board[:3, 3]
            proj = _project(s.obj_points, rvec, tvec, intr, dist)
            out.extend((proj - s.img_points).ravel().tolist())
        return np.array(out, dtype=np.float64) if out else np.zeros(1)

    r0 = residuals(params)
    if not np.any(r0):
        return initial

    rms_before = math.sqrt(float(np.dot(r0, r0)) / max(1, len(r0) // 2))

    # Gauss-Newton: J^T J d = -J^T r, 带 Levenberg-Marquardt 阻尼
    lam = 1e-4
    current = params
    current_cost = float(np.dot(r0, r0))

    for _iter in range(30):
        r = residuals(current)
        cost = float(np.dot(r, r))
        if not np.isfinite(cost):
            break

        # 数值 Jacobian (6 个参数, 开销可忽略)
        jac = np.zeros((len(r), 6), dtype=np.float64)
        eps = 1e-6
        for k in range(6):
            p2 = current.copy()
            p2[k] += eps
            jac[:, k] = (residuals(p2) - r) / eps

        jtj = jac.T @ jac
        jtr = jac.T @ r
        jtj += lam * np.diag(np.maximum(np.diag(jtj), 1e-12))

        try:
            delta = np.linalg.solve(jtj, -jtr)
        except np.linalg.LinAlgError:
            break

        candidate = current + delta
        r_new = residuals(candidate)
        cost_new = float(np.dot(r_new, r_new))

        if cost_new < current_cost:
            current = candidate
            current_cost = cost_new
            lam = max(lam * 0.5, 1e-9)
            if float(np.linalg.norm(delta)) < 1e-9:
                break
        else:
            lam *= 5.0
            if lam > 1e3:
                break

    refined = _params_to_matrix(current)
    rms_after = math.sqrt(current_cost / max(1, len(r0) // 2))
    logger.info(
        "精化: 平均重投影残差 %.3f -> %.3f px", rms_before, rms_after
    )
    return refined


def _board_pose_in_camera(
    matrix: np.ndarray, sample: PoseSample
) -> Optional[np.ndarray]:
    """板在相机系里的位姿 (4x4)。"""
    from ._compat import invert_transform

    t_cam_world = invert_transform(matrix)
    t_cam_board = t_cam_world @ sample.t_world_board()
    return t_cam_board


def _rodrigues(rot: np.ndarray) -> np.ndarray:
    """3x3 旋转矩阵 -> 旋转向量 (3,)。"""
    import cv2

    rvec, _ = cv2.Rodrigues(np.asarray(rot, dtype=np.float64))
    return np.asarray(rvec, dtype=np.float64).ravel()


def _project(
    obj: np.ndarray,
    rvec: np.ndarray,
    tvec: np.ndarray,
    intr: np.ndarray,
    dist: Optional[np.ndarray] = None,
) -> np.ndarray:
    """3D 点投影到像素平面 (Nx2)。"""
    import cv2

    proj, _ = cv2.projectPoints(
        np.asarray(obj, dtype=np.float64),
        np.asarray(rvec, dtype=np.float64),
        np.asarray(tvec, dtype=np.float64),
        np.asarray(intr, dtype=np.float64),
        None if dist is None else np.asarray(dist, dtype=np.float64).ravel(),
    )
    return proj.reshape(-1, 2)


def _params_to_matrix(params: np.ndarray) -> np.ndarray:
    """6 维参数 (tx,ty,tz,rx,ry,rz) -> 4x4 矩阵。"""
    import cv2

    m = np.eye(4, dtype=np.float64)
    rot, _ = cv2.Rodrigues(np.asarray(params[3:6], dtype=np.float64))
    m[:3, :3] = rot
    m[:3, 3] = np.asarray(params[0:3], dtype=np.float64)
    return m


def _matrix_to_params(m: np.ndarray) -> np.ndarray:
    """4x4 矩阵 -> 6 维参数。"""
    import cv2

    rvec, _ = cv2.Rodrigues(np.asarray(m[:3, :3], dtype=np.float64))
    return np.concatenate(
        [
            np.asarray(m[:3, 3], dtype=np.float64),
            np.asarray(rvec, dtype=np.float64).ravel(),
        ]
    )


def solve_pose(
    img_points: np.ndarray,
    obj_points: np.ndarray,
    camera_matrix: np.ndarray,
    dist_coeffs: Optional[np.ndarray] = None,
) -> Optional[np.ndarray]:
    """solvePnP 求板在相机系里的位姿 (4x4)。失败返回 None。"""
    import cv2

    obj = np.asarray(obj_points, dtype=np.float64).reshape(-1, 3)
    img = np.asarray(img_points, dtype=np.float64).reshape(-1, 2)
    if len(obj) < 4:
        return None

    dist = None if dist_coeffs is None else np.asarray(dist_coeffs, dtype=np.float64).ravel()

    ok, rvec, tvec = cv2.solvePnP(
        obj, img, np.asarray(camera_matrix, dtype=np.float64), dist
    )
    if not ok:
        return None

    rot, _ = cv2.Rodrigues(rvec)
    m = np.eye(4, dtype=np.float64)
    m[:3, :3] = rot
    m[:3, 3] = tvec.ravel()
    return m


__all__ = ["HandEyeResult", "PoseSample", "solve_handeye", "solve_pose"]
