"""本地运动学 (pinocchio) —— 客户端自己做正/逆解, 不用控制器的逆解接口。

**为什么不用控制器的逆解**: 控制器的 ``cartesianIkPath: analytic``
有自己的参考坐标系约定 (HM5/DA5 是躯干系, NDA-06 是基座系), 与
``robot/status.cartesian`` 可能不在同一个系里 —— 搞错的话算出来的
「工具偏置」是垃圾 (现场见过 1.01m / 120°), 目标位姿整个算错。

**本模块的做法** (与 ``Telep`` 遥操方案一致):

* 用 URDF (pinocchio) 自己做 FK / IK, 全部在**该臂基座系**里做;
* 世界系 <-> 基座系靠控制器的 ``baseFrame`` 换算 (真机唯一正确的口径);
* IK 用阻尼最小二乘 (DLS), warm start + 零空间姿态回拉, 关节限位每步裁剪;
* 解出来的**关节角**通过 ``controlSpace: 0`` 的关节流下发 ——
  **控制器全程不做末端逆解** (入控用 ``cartesianIkPath: "none"``)。

坐标系 (关键, 别搞混)
----------------------
URDF 里的 ``world`` 与控制器上报的世界系**不是同一个系**。本模块的口径:

* IK / FK 都在**该臂基座系 (base_link)** 里做;
* ``T_world_base`` 来自控制器的 ``baseFrame``, 真机必须用它覆盖 URDF 缺省值。

关节序号 / 转向
---------------
URDF 的 ``j1..j7`` 与控制器上报的 ``joints[0..6]`` **是否同序同向没有文档
保证**。:func:`detect_joint_convention` 会把实测关节角代入 FK, 与控制器
上报的 TCP 位姿对比, 自动探测「同序 / 反序 / 取反」哪个对得上。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("arm.kinematics")

__all__ = [
    "ArmKinematics",
    "DualArmKinematics",
    "IkResult",
    "detect_joint_convention",
    "convention_matches",
    "JOINT_CONVENTIONS",
    "KINEMATICS_AVAILABLE",
    "KDLS",
]

#: URDF 目录 (本仓库内自带)
URDF_DIR = Path(__file__).resolve().parent.parent / "urdf"

#: 控制器臂名 -> URDF 前缀
PREFIX_OF = {"LeftArm": "left", "RightArm": "right"}
ARM_OF_PREFIX = {v: k for k, v in PREFIX_OF.items()}

#: 单臂关节数
NDOF = 7

#: 候选口径名 -> 把控制器实测关节角变成 URDF 关节角的变换
JOINT_CONVENTIONS = {
    "同序": lambda q: q,
    "反序": lambda q: q[::-1].copy(),
    "全取反": lambda q: -q,
    "反序+取反": lambda q: -q[::-1],
}


def _import_pin() -> Optional[Tuple[object, object]]:
    """import pinocchio, 拿不到就返回 None (服务降级但不崩)。"""
    try:
        import numpy as np  # noqa: F401
        import pinocchio as pin
        return pin, np
    except Exception as exc:  # pragma: no cover
        logger.warning("[运动学] pinocchio 不可用 (%s) —— 本地逆解已禁用", exc)
        return None


_PIN = _import_pin()
#: 本地逆解是否可用
KINEMATICS_AVAILABLE = _PIN is not None


if _PIN is not None:
    pin, np = _PIN  # type: ignore[misc]
else:  # pragma: no cover
    pin = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]


# ---------------------------------------------------------------- 姿态工具

def quat_xyzw_to_matrix(q: Sequence[float]):
    """``(qx,qy,qz,qw)`` -> 3x3 旋转矩阵。"""
    x, y, z, w = (float(v) for v in q)
    return np.asarray(pin.Quaternion(w, x, y, z).matrix(), dtype=float)


def matrix_to_quat_xyzw(R) -> Tuple[float, float, float, float]:
    """3x3 -> ``(qx,qy,qz,qw)`` (取 w>=0 的那个表示)。"""
    q = pin.Quaternion(np.asarray(R, dtype=float))
    q.normalize()
    if q.w < 0.0:
        q = pin.Quaternion(-q.w, -q.x, -q.y, -q.z)
    return (float(q.x), float(q.y), float(q.z), float(q.w))


def rpy_to_matrix(rpy: Sequence[float]):
    """``(roll, pitch, yaw)`` -> R = Rz(yaw)·Ry(pitch)·Rx(roll)。

    与控制器 ``a,b,c`` 的 ZYX 口径一致。
    """
    return np.asarray(pin.rpy.rpyToMatrix(np.asarray(rpy, dtype=float)), dtype=float)


def matrix_to_rpy(R) -> Tuple[float, float, float]:
    """3x3 -> ``(roll, pitch, yaw)``。"""
    rpy = pin.rpy.matrixToRpy(np.asarray(R, dtype=float))
    return (float(rpy[0]), float(rpy[1]), float(rpy[2]))


def pose_error(T_a, T_b) -> Tuple[float, float]:
    """两个位姿的 ``(位置误差 m, 姿态误差 rad)``, 姿态取测地角。"""
    e_pos = float(np.linalg.norm(T_a.translation - T_b.translation))
    dR = T_a.rotation.T @ T_b.rotation
    e_rot = float(np.linalg.norm(pin.log3(dR)))
    return e_pos, e_rot


def pose_error_world(T_world, pos: Sequence[float], rpy: Sequence[float]):
    """``T_world`` 与「世界系位置 + RPY」的位姿误差。

    控制器 ``cartesian`` 用的就是世界系 RPY (``a,b,c``, ZYX) —— 所以这是
    「拿实测关节角做 FK, 再和控制器说的末端位姿对账」的入口。
    """
    T_ref = pin.SE3(rpy_to_matrix(rpy), np.asarray(pos, dtype=float))
    return pose_error(T_world, T_ref)


# ---------------------------------------------------------------- 逆解结果

@dataclass
class IkResult:
    """一次逆解的结果。

    ``ok`` 表示「收敛且满足容差」—— 调用方**必须**看它再决定下发,
    不然 IK 失败时会把一个乱掉的关节角当目标发出去。
    """

    q: List[float]              # 关节角 (rad), 7 维
    err_pos: float              # 位置残差 (m)
    err_rot: float              # 姿态残差 (rad)
    ok: bool
    iters: int
    at_limit: bool = False      # 是否有关节顶在限位上

    def summary(self) -> str:
        return (f"{'OK ' if self.ok else 'FAIL'} "
                f"err={self.err_pos * 1000:.3f}mm/{math.degrees(self.err_rot):.3f}deg "
                f"iters={self.iters}"
                + ("  [顶限位]" if self.at_limit else ""))


# ---------------------------------------------------------------- 单臂

class ArmKinematics:
    """一条 7-DoF 臂的 FK / IK (pinocchio 实现)。

    每条臂加载**自己的**单臂 URDF, 模型天然是 7 维 —— ``q`` 就是干净的
    长度 7 向量, 与控制器 ``joints[0..6]`` 一一对应 (顺序见
    :func:`detect_joint_convention` 的探测结果)。
    """

    def __init__(self, arm: str, urdf_path: Optional[Path | str] = None,
                 tcp_frame: Optional[str] = None,
                 base_pos: Sequence[float] = (0.0, 0.0, 0.0),
                 base_rpy: Optional[Sequence[float]] = None):
        if not KINEMATICS_AVAILABLE:  # pragma: no cover
            raise RuntimeError(
                "pinocchio 不可用 —— 本地逆解无法工作。"
                "装: pip install numpy pin"
            )
        if arm not in PREFIX_OF:
            raise ValueError(f"未知臂名 {arm!r} (应为 {list(PREFIX_OF)})")
        self.arm = arm
        self.prefix = PREFIX_OF[arm]
        self.urdf_path = Path(urdf_path) if urdf_path else URDF_DIR / f"{self.prefix}_arm.urdf"
        if not self.urdf_path.exists():
            raise FileNotFoundError(f"URDF 不存在: {self.urdf_path}")

        full = pin.buildModelFromUrdf(str(self.urdf_path))
        self._full_joint_ids = [
            i for i in range(1, full.njoints)
            if full.names[i].startswith(f"{self.prefix}_j")
        ]
        if len(self._full_joint_ids) != NDOF:
            raise RuntimeError(
                f"{arm}: URDF 里找到 {len(self._full_joint_ids)} 个 "
                f"{self.prefix}_j* 关节, 期望 {NDOF} 个 ({full.names[1:]})")

        lock_ids = [i for i in range(1, full.njoints) if i not in self._full_joint_ids]
        if lock_ids:
            model = pin.buildReducedModel(full, lock_ids, pin.neutral(full))
        else:
            model = full
        if model.nq != NDOF:
            raise RuntimeError(f"{arm}: 降维后 nq={model.nq}, 期望 {NDOF}")

        self.model = model
        self.data = model.createData()
        self.joint_names = [model.names[i] for i in range(1, model.njoints)]

        self.tcp_frame = tcp_frame or f"{self.prefix}_j7_Link"
        self.frame_id = model.getFrameId(self.tcp_frame)
        if self.frame_id >= len(model.frames):
            raise RuntimeError(f"{arm}: URDF 里找不到末端 frame {self.tcp_frame!r}")

        self.lower = np.asarray(model.lowerPositionLimit, dtype=float).copy()
        self.upper = np.asarray(model.upperPositionLimit, dtype=float).copy()

        # URDF 里 world -> base_link (固定关节, 静态)
        self.T_urdf_world_base = self._frame_pose_at_neutral(f"{self.prefix}_base_link")
        # 世界系 (控制器口径) -> base_link。缺省用 URDF 自带的装法,
        # 真机必须用 set_base_from_controller() 覆盖成实测 baseFrame。
        self.T_world_base = pin.SE3(
            rpy_to_matrix(base_rpy if base_rpy is not None
                          else matrix_to_rpy(self.T_urdf_world_base.rotation)),
            np.asarray(base_pos, dtype=float))

    # ---------------- 基础 ----------------

    def _frame_pose_at_neutral(self, frame_name: str):
        """在关节全零位时取某 frame 在 URDF world 下的位姿。"""
        fid = self.model.getFrameId(frame_name)
        d = self.model.createData()
        pin.forwardKinematics(self.model, d, pin.neutral(self.model))
        pin.updateFramePlacements(self.model, d)
        return pin.SE3(np.array(d.oMf[fid].rotation, dtype=float),
                       np.array(d.oMf[fid].translation, dtype=float))

    def set_base_from_controller(self, pos: Sequence[float],
                                 rpy: Sequence[float]) -> None:
        """用控制器 ``baseFrame`` (``{x,y,z,a,b,c}``, 世界系) 设定基座位姿。

        **这是真机唯一正确的用法** —— URDF 的装法与实测 ``baseFrame``
        不是同一个数。
        """
        self.T_world_base = pin.SE3(rpy_to_matrix(rpy),
                                    np.asarray(pos, dtype=float))

    def set_base_from_arm_frame_dict(self, bf: Dict[str, float]) -> bool:
        """``base_frame`` 字典 -> 基座位姿。"""
        if not bf or not all(k in bf for k in ("x", "y", "z", "a", "b", "c")):
            return False
        self.set_base_from_controller((bf["x"], bf["y"], bf["z"]),
                                      (bf["a"], bf["b"], bf["c"]))
        return True

    @property
    def base_pos(self):
        return np.array(self.T_world_base.translation, dtype=float)

    @property
    def base_rpy(self) -> Tuple[float, float, float]:
        return matrix_to_rpy(self.T_world_base.rotation)

    def clamp(self, q: Iterable[float]):
        return np.clip(np.asarray(q, dtype=float), self.lower, self.upper)

    # ---------------- 正解 ----------------

    def fk_base(self, q: Sequence[float]):
        """关节角 -> 末端位姿 (**基座系**)。"""
        qq = np.asarray(q, dtype=float)
        if qq.shape != (NDOF,):
            raise ValueError(f"{self.arm}: q 必须是 {NDOF} 维 (收到 {qq.shape})")
        pin.forwardKinematics(self.model, self.data, qq)
        pin.updateFramePlacements(self.model, self.data)
        M = self.data.oMf[self.frame_id]
        return self.T_urdf_world_base.inverse() * pin.SE3(
            np.array(M.rotation, dtype=float),
            np.array(M.translation, dtype=float))

    def fk_world(self, q: Sequence[float]):
        """关节角 -> 末端位姿 (**控制器世界系**)。"""
        return self.T_world_base * self.fk_base(q)

    def fk_world_pose(self, q: Sequence[float]):
        """便利封装: 返回 ``(pos, quat_xyzw)``。"""
        T = self.fk_world(q)
        return (tuple(float(v) for v in T.translation),
                matrix_to_quat_xyzw(T.rotation))

    # ---------------- 逆解 ----------------

    def ik_base(self, T_target, q_seed: Sequence[float], *,
                max_iters: int = 60, tol: float = 1e-6,
                tol_pos: Optional[float] = None,
                tol_rot: Optional[float] = None,
                damping: float = 1e-6, step: float = 0.5,
                posture_gain: float = 0.0,
                rot_weight: float = 1.0,
                joint_weights: Optional[Sequence[float]] = None) -> IkResult:
        """阻尼最小二乘 (DLS) 逆解 (**基座系**目标)。

        - ``q_seed`` —— warm start (上一拍的解, 首次 = 实测关节角);
        - ``posture_gain`` —— 零空间姿态回拉, 把解往 ``q_seed`` 拉,
          防止七轴冗余跳解支导致关节突跳;
        - ``tol_pos`` / ``tol_rot`` —— 位置与姿态**分开**判阈值;
        - ``rot_weight`` —— **姿态权重**。``0`` = 只管位置, 姿态完全不管;
          ``1`` = 位置姿态同等重要 (缺省)。
          **抓取时用 0**: 七轴冗余有无数解, 位置定了姿态本来就不唯一;
          而且手臂伸直时 (j3≈0) 雅可比近奇异, 姿态方向的那个自由度
          **丢失了**, 要求姿态精确会把位置也一起拖死 (实测位置差
          100mm 都下不去)。位置才是抓取真正在乎的。
        - 关节限位**每步都裁剪** —— 顶到限位时残差降不下去会体现为
          ``ok=False``, 而不是悄悄越界。

        返回 ``ok=False`` 时调用方**必须**放弃这一帧。
        """
        q = self.clamp(q_seed)
        q_seed_arr = np.asarray(q_seed, dtype=float)
        T_target_urdf = self.T_urdf_world_base * T_target
        eye7 = np.eye(NDOF)
        if joint_weights is None:
            w_inv = eye7
        else:
            w = np.asarray(joint_weights, dtype=float)
            if w.shape != (NDOF,) or np.any(w <= 0.0):
                raise ValueError(f"joint_weights 须是 {NDOF} 个正数 (收到 {w})")
            w_inv = np.diag(1.0 / w)
        # 任务权重: 位置 1.0, 姿态 rot_weight
        rw = max(0.0, float(rot_weight))
        W = np.diag([1.0, 1.0, 1.0, rw, rw, rw]) if rw != 1.0 else None
        iters = 0
        ok = False

        for iters in range(1, max_iters + 1):
            pin.forwardKinematics(self.model, self.data, q)
            pin.updateFramePlacements(self.model, self.data)
            M = self.data.oMf[self.frame_id]
            err6 = np.asarray(pin.log6(M.actInv(T_target_urdf)).vector, dtype=float)
            e_pos, e_rot = pose_error(M, T_target_urdf)
            if tol_pos is not None or tol_rot is not None:
                # 姿态权重为 0 时, 不拿姿态当收敛判据
                rot_ok = (rw == 0.0) or (
                    e_rot <= (tol_rot if tol_rot is not None else 1e9))
                if e_pos <= (tol_pos if tol_pos is not None else 1e9) and rot_ok:
                    ok = True
                    break
            elif float(np.linalg.norm(err6)) <= tol:
                ok = True
                break

            J = pin.computeFrameJacobian(self.model, self.data, q,
                                         self.frame_id, pin.LOCAL)
            J = np.asarray(J, dtype=float)[:, :NDOF]
            if W is not None:
                err = np.concatenate([err6[:3], err6[3:] * rw])
                Jw = W @ J
            else:
                err = err6
                Jw = J
            JJt = Jw @ w_inv @ Jw.T + damping * np.eye(6)
            Jpinv = w_inv @ Jw.T @ np.linalg.inv(JJt)
            dq = Jpinv @ err
            if posture_gain > 0.0:
                N = eye7 - Jpinv @ Jw
                dq = dq + N @ (posture_gain * (q_seed_arr - q))
            q = self.clamp(pin.integrate(self.model, q, step * dq))

        pin.forwardKinematics(self.model, self.data, q)
        pin.updateFramePlacements(self.model, self.data)
        M = self.data.oMf[self.frame_id]
        e_pos, e_rot = pose_error(M, T_target_urdf)
        at_limit = bool(np.any(np.isclose(q, self.lower, atol=1e-9))
                        or np.any(np.isclose(q, self.upper, atol=1e-9)))
        return IkResult(q=[float(v) for v in q], err_pos=e_pos, err_rot=e_rot,
                        ok=ok, iters=iters, at_limit=at_limit)

    def ik_world(self, pos: Sequence[float], quat_xyzw: Sequence[float],
                 q_seed: Sequence[float], **kwargs) -> IkResult:
        """逆解 (**控制器世界系**目标位姿 -> 关节角)。"""
        T_world = pin.SE3(quat_xyzw_to_matrix(quat_xyzw),
                         np.asarray(pos, dtype=float))
        return self.ik_base(self.T_world_base.inverse() * T_world, q_seed, **kwargs)

    def ik_world_rpy(self, pos: Sequence[float], rpy: Sequence[float],
                     q_seed: Sequence[float], **kwargs) -> IkResult:
        """逆解 (世界系 **RPY** 目标 -> 关节角)。"""
        T_world = pin.SE3(rpy_to_matrix(rpy), np.asarray(pos, dtype=float))
        return self.ik_base(self.T_world_base.inverse() * T_world, q_seed, **kwargs)

    def ik_world_rpy_with_fallback(self, pos: Sequence[float],
                                   rpy: Sequence[float],
                                   q_seed: Sequence[float], **kwargs) -> IkResult:
        """带回退的逆解 (世界系 RPY 目标 -> 关节角)。

        见 :meth:`ik_base_with_fallback` —— 全姿态 -> 多起点 -> 位置优先。
        """
        T_world = pin.SE3(rpy_to_matrix(rpy), np.asarray(pos, dtype=float))
        return self.ik_base_with_fallback(
            self.T_world_base.inverse() * T_world, q_seed, **kwargs
        )

    def ik_multistart(self, T_target, q_seed: Sequence[float], *,
                      n_starts: int = 24, seed: int = 0, **kwargs) -> IkResult:
        """多起点逆解 —— 单起点解不出来时换种子。

        用于**首次**把末端送到某个绝对位姿 (比如归位准备点): 当前关节角
        可能离目标太远或落在不同的解支上。
        """
        best = self.ik_base(T_target, q_seed, **kwargs)
        if best.ok:
            return best
        rng = np.random.default_rng(seed)
        for _ in range(n_starts):
            q0 = rng.uniform(self.lower, self.upper)
            r = self.ik_base(T_target, q0, **kwargs)
            if r.ok and (not best.ok or r.err_pos < best.err_pos):
                best = r
            if best.ok:
                break
        return best

    def ik_base_with_fallback(self, T_target, q_seed: Sequence[float], *,
                              max_iters: int = 120,
                              tol_pos: float = 1e-4,
                              tol_rot: float = 1e-3,
                              damping: float = 1e-6,
                              step: float = 0.5,
                              posture_gain: float = 0.0,
                              rot_weight: float = 1.0,
                              multistart: int = 24) -> IkResult:
        """带回退的逆解 —— **抓取用这个**。

        依次尝试:

        1. **全姿态** (``rot_weight=1``): 位置姿态都要;
        2. **多起点**: 换种子再试全姿态 (目标离当前太远时有用);
        3. **位置优先** (``rot_weight=0``): 只管位置, 姿态随冗余自由度走。

        第 3 步是**必须的**, 不是可选优化: 七轴冗余有无数解, 位置定了
        姿态本来就不唯一; 而且手臂伸直时 (j3≈0) 雅可比近奇异, 姿态方向
        的那个自由度**丢失了** —— 此时要求姿态精确会把位置一起拖死
        (实测位置差 100mm 都下不去, 而纯位置逆解能到 0.000mm)。

        返回的 ``IkResult`` 里 ``err_rot`` 可能是几十度, 调用方要自己
        判断能不能接受。
        """
        # 1) 全姿态
        r = self.ik_base(T_target, q_seed, max_iters=max_iters,
                          tol_pos=tol_pos, tol_rot=tol_rot,
                          damping=damping, step=step,
                          posture_gain=posture_gain, rot_weight=rot_weight)
        if r.ok:
            return r

        # 2) 多起点 (全姿态)
        if multistart > 0:
            r = self.ik_multistart(
                T_target, q_seed, n_starts=multistart, max_iters=max_iters,
                tol_pos=tol_pos, tol_rot=tol_rot, damping=damping, step=step,
                posture_gain=posture_gain, rot_weight=rot_weight,
            )
            if r.ok:
                return r

        # 3) 位置优先 (姿态权重 0)
        r_pos = self.ik_base(T_target, q_seed, max_iters=max_iters,
                              tol_pos=tol_pos, tol_rot=tol_rot,
                              damping=damping, step=step,
                              posture_gain=posture_gain, rot_weight=0.0)
        if r_pos.ok:
            return r_pos
        if multistart > 0:
            r_pos = self.ik_multistart(
                T_target, q_seed, n_starts=multistart, max_iters=max_iters,
                tol_pos=tol_pos, tol_rot=tol_rot, damping=damping, step=step,
                posture_gain=posture_gain, rot_weight=0.0,
            )
            if r_pos.ok:
                return r_pos

        # 都没解出来 —— 返回**位置误差最小**的那个, 供调用方判断
        return r_pos if r_pos.err_pos < r.err_pos else r


# ---------------------------------------------------------------- 双臂

class DualArmKinematics:
    """``{臂名: ArmKinematics}`` 的薄封装。"""

    def __init__(self, urdf_paths: Optional[Dict[str, Path | str]] = None,
                 arms: Sequence[str] = ("LeftArm", "RightArm"),
                 tcp_frames: Optional[Dict[str, str]] = None,
                 base_frames: Optional[Dict[str, Dict[str, float]]] = None):
        self.arms: Dict[str, ArmKinematics] = {}
        for arm in arms:
            k = ArmKinematics(
                arm,
                (urdf_paths or {}).get(arm),
                tcp_frame=(tcp_frames or {}).get(arm),
            )
            bf = (base_frames or {}).get(arm)
            if bf is not None:
                k.set_base_from_arm_frame_dict(bf)
            self.arms[arm] = k

    def __getitem__(self, arm: str) -> ArmKinematics:
        return self.arms[arm]

    def __contains__(self, arm: str) -> bool:
        return arm in self.arms

    def sync_base_from_frames(self, frames: Dict[str, Dict[str, float]]) -> Dict[str, bool]:
        """用控制器的 ``base_frame`` 覆盖每条臂的基座位姿。

        **上机前必须做这一步** —— URDF 的 world 与控制器世界系不是同一个
        系, 不同步的话 IK 目标整体偏。
        """
        out: Dict[str, bool] = {}
        for arm, bf in (frames or {}).items():
            if arm not in self.arms:
                continue
            out[arm] = self.arms[arm].set_base_from_arm_frame_dict(bf)
        return out


# ---------------------------------------------------------------- 关节约定

def detect_joint_convention(kin: "ArmKinematics",
                            q_measured: Sequence[float],
                            tcp_pose: Dict[str, float],
                            *,
                            pos_tol_m: float = 0.02,
                            rot_tol_rad: float = math.radians(2.0),
                            ) -> List[Tuple[str, float, float]]:
    """探测 URDF 关节角与控制器实测关节角之间的口径。

    **背景**: URDF 的 ``j1..j7`` 与控制器 ``joints[0..6]`` 是否同序同向
    **没有文档保证**。猜错的后果是机械臂朝完全相反的方向动, 而且
    **不报任何错**。

    做法: 把实测关节角按每个候选口径变换后做 FK, 与控制器上报的
    ``cartesian`` (世界系位置 + RPY) 对比, 取误差最小的口径。

    返回 ``[(口径名, 位置误差 m, 姿态误差 rad), ...]``, 按误差从小到大。
    """
    q = np.asarray(q_measured, dtype=float)
    if q.shape != (NDOF,):
        raise ValueError(f"实测关节角必须是 {NDOF} 维 (收到 {q.shape})")
    if not all(k in tcp_pose for k in ("x", "y", "z", "a", "b", "c")):
        raise ValueError("tcp_pose 需含 x,y,z,a,b,c")
    pos = (tcp_pose["x"], tcp_pose["y"], tcp_pose["z"])
    rpy = (tcp_pose["a"], tcp_pose["b"], tcp_pose["c"])

    out: List[Tuple[str, float, float]] = []
    for name, fn in JOINT_CONVENTIONS.items():
        qq = np.asarray(fn(q), dtype=float)
        T = kin.fk_world(qq)
        e_pos, e_rot = pose_error_world(T, pos, rpy)
        out.append((name, e_pos, e_rot))
    out.sort(key=lambda t: t[1] + 0.01 * t[2])
    return out


def convention_matches(e_pos: float, e_rot: float, *,
                       pos_tol_m: float = 0.02,
                       rot_tol_rad: float = math.radians(2.0)) -> bool:
    """某个口径的误差是否算「对得上」。"""
    return e_pos < pos_tol_m and e_rot < rot_tol_rad


# ---------------------------------------------------------------- 关节流跟踪

class KDLS:
    """关节通道: 目标位姿 -> 目标关节角 (带限速与种子维护)。

    逐帧 IK 的三个坑, 这里都处理掉:

    1. **种子**: 每帧拿**上一帧的目标关节**当种子 (不是实测 —— 实测有
       伺服滞后, 跟随目标时会持续落在后面, 种子越用越偏)。
    2. **跳解支**: 七轴冗余有无数解, 种子变了就容易跳到另一支。用
       「以上一帧目标为种子 + 零空间回拉 + 每帧增量限幅」三重保险钉住。
    3. **限速**: 单帧关节增量限幅, 等比缩放保持各关节比例 (逐关节 clip
       会打乱比例, 末端就从直线变成弧线)。
    """

    def __init__(self, kin: ArmKinematics, *,
                 max_step_deg: float = 3.0,
                 posture_gain: float = 0.02,
                 tol_pos_m: float = 1e-4,
                 tol_rot_rad: float = 1e-3,
                 max_iters: int = 60,
                 damping: float = 1e-6,
                 step: float = 0.5,
                 joint_weights: Optional[Sequence[float]] = None):
        self.kin = kin
        self.joint_weights = (None if joint_weights is None
                              else tuple(float(v) for v in joint_weights))
        self.max_step = math.radians(max_step_deg)
        self.posture_gain = posture_gain
        self.tol_pos = tol_pos_m
        self.tol_rot = tol_rot_rad
        self.max_iters = max_iters
        self.damping = damping
        self.step = step
        self.q: Optional[object] = None
        self.last_ok = True
        #: 最近一次 IK 的详细结果 (诊断用)
        self.last_result: Optional[IkResult] = None

    def seed_from(self, q_measured: Optional[Sequence[float]]) -> None:
        """用当前实测关节角重置种子 (接管 / 重锚时调用)。"""
        if q_measured is None:
            return
        q = np.asarray(q_measured, dtype=float)
        if q.shape != (NDOF,):
            return
        self.q = self.kin.clamp(q)

    def reset(self) -> None:
        self.q = None
        self.last_ok = True
        self.last_result = None

    @staticmethod
    def _limit_step(dq, step: float):
        """单帧关节增量限幅: **等比缩放**, 保持各关节比例。"""
        m = float(np.max(np.abs(dq)))
        if m > step:
            dq = dq * (step / m)
        return dq

    def solve(self, pos: Sequence[float], quat_xyzw: Sequence[float], *,
              max_step_deg: Optional[float] = None) -> Optional[List[float]]:
        """目标世界系位姿 -> 限速后的目标关节角。解不出来返回 ``None``。

        返回 ``None`` 时调用方**不要下发**。
        """
        if self.q is None:
            return None
        r = self.kin.ik_world(pos, quat_xyzw, self.q,
                              max_iters=self.max_iters,
                              tol_pos=self.tol_pos, tol_rot=self.tol_rot,
                              posture_gain=self.posture_gain,
                              damping=self.damping, step=self.step,
                              joint_weights=self.joint_weights)
        self.last_result = r
        if not r.ok:
            self.last_ok = False
            return None
        self.last_ok = True
        step = (math.radians(max_step_deg) if max_step_deg is not None
                else self.max_step)
        dq = self._limit_step(np.asarray(r.q) - self.q, step)
        self.q = self.kin.clamp(self.q + dq)
        return [float(v) for v in self.q]

    def solve_rpy(self, pos: Sequence[float], rpy: Sequence[float], *,
                  max_step_deg: Optional[float] = None) -> Optional[List[float]]:
        """同 :meth:`solve`, 但目标姿态用 RPY。"""
        if self.q is None:
            return None
        r = self.kin.ik_world_rpy(pos, rpy, self.q,
                                  max_iters=self.max_iters,
                                  tol_pos=self.tol_pos, tol_rot=self.tol_rot,
                                  posture_gain=self.posture_gain,
                                  damping=self.damping, step=self.step,
                                  joint_weights=self.joint_weights)
        self.last_result = r
        if not r.ok:
            self.last_ok = False
            return None
        self.last_ok = True
        step = (math.radians(max_step_deg) if max_step_deg is not None
                else self.max_step)
        dq = self._limit_step(np.asarray(r.q) - self.q, step)
        self.q = self.kin.clamp(self.q + dq)
        return [float(v) for v in self.q]
