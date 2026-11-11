"""手眼外参: 相机坐标系 -> 目标坐标系。

**坐标系约定 (必须先搞清楚, 否则标定白做)**

相机系 (OpenCV 约定):
    X 向右, Y 向下, Z 向前 (沿光轴)

机械臂基座系 (ROS / URDF 常见约定):
    X 向前, Y 向左, Z 向上

这两个系之间的换算是一个**轴置换** (行列式 +1, 是旋转不是镜像):

    M = [[0, 0, 1],
         [-1, 0, 0],
         [0, -1, 0]]

    验算: X_base = Z_cam, Y_base = -X_cam, Z_base = -Y_cam
    即: 相机的前 -> 基座的前; 相机的右 -> 基座的右后侧 (-Y); 相机的下 -> 基座的下 (-Z)

**历史包袱**: 老版本这里有个 ``flip_axes`` 开关, 做的是
``(-x, -y, z)`` —— 那是**绕光轴转 180°**, 不是上面那个轴置换。
注释里却写着 "X前 Y左 Z上", 与实际行为不符, 很容易把标定结果带偏。

现在:
- 保留 ``flip_axes`` 字段, 但**语义改成明确的轴置换**;
- 推荐直接把标定结果写进 ``extrinsic.matrix`` (4x4, 含轴变换), 并把
  ``flip_axes`` 置 false —— 一切都在矩阵里, 不再有隐式变换。

**目标坐标系**

- ``camera``   相机系 (永远可用)
- ``arm_base`` 机械臂基座系 —— 相机固定在躯干上时用这个, 静态外参即可
- ``world``    世界系 —— 需要叠加基座/升降柱的动态补偿
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger("vision.extrinsic")


#: 相机系 (OpenCV) -> 基座系 (X前 Y左 Z上) 的轴置换矩阵。
#: 行列式 = +1 (是旋转, 不是镜像)。
AXIS_PERMUTATION_CAMERA_TO_BASE = np.array(
    [
        [0.0, 0.0, 1.0],
        [-1.0, 0.0, 0.0],
        [0.0, -1.0, 0.0],
    ],
    dtype=np.float64,
)


# ------------------------------------------------------------ 基础变换工具


def make_transform(
    rotation: Optional[Sequence[Sequence[float]]] = None,
    translation: Optional[Sequence[float]] = None,
) -> np.ndarray:
    """由 3x3 旋转 + 3 维平移构造 4x4 齐次矩阵。"""
    m = np.eye(4, dtype=np.float64)
    if rotation is not None:
        m[:3, :3] = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
    if translation is not None:
        m[:3, 3] = np.asarray(translation, dtype=np.float64).reshape(3)
    return m


def invert_transform(m: np.ndarray) -> np.ndarray:
    """4x4 齐次矩阵求逆 (刚体变换用解析式, 数值更稳)。"""
    m = np.asarray(m, dtype=np.float64)
    rot = m[:3, :3]
    trans = m[:3, 3]
    out = np.eye(4, dtype=np.float64)
    out[:3, :3] = rot.T
    out[:3, 3] = -rot.T @ trans
    return out


def rpy_to_matrix(rpy: Sequence[float]) -> np.ndarray:
    """RPY (roll, pitch, yaw, rad) -> 3x3 旋转矩阵。

    用 **ZYX 内旋** (yaw -> pitch -> roll), 与控制器
    ``cartesian.a/b/c`` 的语义一致 (控制器的 xyzabc 也是这个约定)。
    """
    r, p, y = [float(v) for v in rpy[:3]]
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ],
        dtype=np.float64,
    )


def matrix_to_rpy(m: np.ndarray) -> Tuple[float, float, float]:
    """3x3 旋转矩阵 -> RPY (rad), ZYX 内旋约定。"""
    r = np.asarray(m, dtype=np.float64)[:3, :3]
    sp = -float(r[2, 0])
    sp = max(-1.0, min(1.0, sp))
    pitch = math.asin(sp)
    if abs(sp) < 0.999999:
        roll = math.atan2(float(r[2, 1]), float(r[2, 2]))
        yaw = math.atan2(float(r[1, 0]), float(r[0, 0]))
    else:  # 万向锁
        roll = math.atan2(-float(r[1, 2]), float(r[1, 1]))
        yaw = 0.0
    return (roll, pitch, yaw)


def rotation_error_rad(a: np.ndarray, b: np.ndarray) -> float:
    """两个旋转矩阵之间的角度差 (rad)。

    用迹公式: cos(theta) = (tr(R_a^T R_b) - 1) / 2
    """
    ra = np.asarray(a, dtype=np.float64)[:3, :3]
    rb = np.asarray(b, dtype=np.float64)[:3, :3]
    tr = float(np.trace(ra.T @ rb))
    c = max(-1.0, min(1.0, (tr - 1.0) / 2.0))
    return math.acos(c)


def transform_points(m: np.ndarray, points: np.ndarray) -> np.ndarray:
    """用 4x4 变换作用在 Nx3 点集上。"""
    m = np.asarray(m, dtype=np.float64)
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    return pts @ m[:3, :3].T + m[:3, 3]


# ------------------------------------------------------------ 外参提供方


class ExtrinsicProvider:
    """相机系 -> 目标系的外参。

    **静态外参** (相机固定在躯干上, 目标系 = 臂基座系):
        直接把标定出来的 4x4 写进配置, 用 ``StaticExtrinsicProvider``。

    **动态外参** (目标系 = 世界系, 且基座会动):
        静态矩阵 + 一个随时刻变化的偏移。``DynamicExtrinsicProvider``
        在静态矩阵左边乘一个「基座 -> 世界」的变换。

    接口统一成一个方法: ``get()`` 返回 4x4 或 None。
    """

    #: 目标坐标系的名字
    frame: str = "camera"

    def get(self) -> Optional[np.ndarray]:
        """拿当前的 4x4 外参。拿不到返回 None。"""
        raise NotImplementedError

    def available(self) -> bool:
        return self.get() is not None

    def get_for_frame(self, frame: str) -> Optional[np.ndarray]:
        """拿「相机 -> 指定目标系」的外参。

        默认只认 provider 自己的目标系 —— 请求别的系返回 None,
        上层据此退回相机系 (而不是给出错坐标)。
        """
        if str(frame or "") == self.frame:
            return self.get()
        return None

    def transform(self, xyz: Sequence[float]) -> Optional[Tuple[float, float, float]]:
        """相机系的点 -> 目标系。变换不了返回 None。"""
        m = self.get()
        if m is None:
            return None
        p = np.array([float(xyz[0]), float(xyz[1]), float(xyz[2]), 1.0])
        out = m @ p
        return (float(out[0]), float(out[1]), float(out[2]))


class StaticExtrinsicProvider(ExtrinsicProvider):
    """固定的外参 —— 标定一次, 一直用。

    这是最常见的情况: 相机刚性固定在躯干上, 目标是机械臂基座系。
    升降柱升降不影响 (相机和臂基座一起动), 所以静态外参成立。
    """

    def __init__(
        self,
        matrix: Optional[np.ndarray] = None,
        frame: str = "arm_base",
        flip_axes: bool = False,
    ) -> None:
        self._matrix = matrix
        self.frame = frame
        self.flip_axes = flip_axes

    def get(self) -> Optional[np.ndarray]:
        if self._matrix is None:
            return None
        m = np.array(self._matrix, dtype=np.float64)
        if self.flip_axes:
            # 相机系 (X右Y下Z前) -> 基座系 (X前Y左Z上) 的轴置换
            m = m @ _axis_permutation_4x4()
        return m


class DynamicExtrinsicProvider(ExtrinsicProvider):
    """静态外参 + 动态基座偏移。

    用于「目标系 = 世界系」且基座会动的情况 (升降柱升降 / 挂接安装)。

    链路:
        T_world_cam = T_world_base · T_base_cam

    - ``T_base_cam``: 标定出来的静态量 (相机到臂基座)
    - ``T_world_base``: 运行时从 ``arm_service /v1/kinematics`` 拿
      (``base_frame`` 是臂基座在世界系下的位姿)

    ``base_offset`` 是 ``T_world_base`` 的 4x4; 拿不到时返回 None ——
    **宁可返回 None 也不要假装**, 否则报出来的坐标是错的。
    """

    def __init__(
        self,
        base_to_camera: np.ndarray,
        frame: str = "world",
        base_offset: Optional[np.ndarray] = None,
    ) -> None:
        self._base_to_camera = np.asarray(base_to_camera, dtype=np.float64)
        self.frame = frame
        self._base_offset = base_offset

    def set_base_offset(self, offset: Optional[np.ndarray]) -> None:
        """更新「臂基座 -> 世界系」的变换。"""
        self._base_offset = (
            None if offset is None else np.asarray(offset, dtype=np.float64)
        )

    def get(self) -> Optional[np.ndarray]:
        if self._base_offset is None:
            return None
        return self._base_offset @ self._base_to_camera


# ------------------------------------------------------- 运行时世界系上下文


@dataclass
class WorldContext:
    """运行时从 ``arm_service`` 拿到的世界系上下文。

    **为什么要有它**: 相机装在躯干/头部上, 而双臂各有自己的基座。
    想把相机系坐标换算到「左臂基座系」和「右臂基座系」, 必须知道
    这两个基座在世界系下的位姿 —— 控制器才给得起。

    字段:
    - ``base_frames``: 臂名 -> ``T_world_base`` (4x4)
    - ``head_frame``:  ``T_world_head`` (4x4), 相机安装座 (头) 在世界系
      下的位姿。**头会动时需要它**; 头固定时可以是 None (退化成静态外参)。
    - ``timestamp_ms``: 快照时间 (毫秒)
    """

    #: 臂名 (控制器里的名字, 如 "LeftArm") -> T_world_base (4x4)
    base_frames: Dict[str, np.ndarray] = field(default_factory=dict)
    #: 头/相机安装座 -> 世界系 的位姿 (4x4)
    head_frame: Optional[np.ndarray] = None
    #: 快照时间 (毫秒)
    timestamp_ms: int = 0
    #: 头部位姿是不是「挂接派生」的 (会随父机动)
    head_frame_kind: str = ""
    #: 备注 (拿不到什么)
    notes: List[str] = field(default_factory=list)

    def arm_side(self, name: str) -> str:
        """判断臂名属于左臂还是右臂。"""
        lower = str(name or "").lower()
        if "left" in lower or "_l" in lower or lower.endswith("l"):
            return "left"
        if "right" in lower or "_r" in lower or lower.endswith("r"):
            return "right"
        return ""

    def base_frame_by_side(self, side: str) -> Optional[np.ndarray]:
        """按「左/右」取基座的世界位姿。"""
        if not side:
            return None
        for name, m in self.base_frames.items():
            if self.arm_side(name) == side:
                return m
        return None

    def available_sides(self) -> List[str]:
        """当前能拿到基座位姿的臂侧 (left / right)。"""
        out: List[str] = []
        for name in self.base_frames:
            side = self.arm_side(name)
            if side and side not in out:
                out.append(side)
        return out


class RigidExtrinsicProvider(ExtrinsicProvider):
    """相机装在躯干/头部上, 目标系可以是**任意一个臂的基座系**。

    标定产出的是 ``T_world_cam`` (相机 -> 世界系)。换算到某个臂基座系:

        T_base_cam = (T_world_base)⁻¹ · T_world_cam

    **头会动时**: 标定产出改成 ``T_head_cam`` (相机 -> 头座), 运行时补上
    头在世界系下的位姿:

        T_world_cam = T_world_head(q_head) · T_head_cam

    **拿不到就返回 None** —— 宁可说"没有基座坐标", 也不要给一个错的。
    """

    def __init__(
        self,
        matrix: np.ndarray,
        matrix_frame: str = "world",
        frame: str = "world",
        arm: str = "auto",
        context: Optional[WorldContext] = None,
    ) -> None:
        self._matrix = np.asarray(matrix, dtype=np.float64)
        #: 标定矩阵描述的是「相机 -> 哪个系」: world / head / arm_base
        self.matrix_frame = matrix_frame
        #: 运行时对外暴露的目标系: world / arm_base / left_base / right_base
        self.frame = frame
        #: 目标系是 arm_base 时用哪条臂: left / right / auto
        self.arm = arm or "auto"
        self._context = context or WorldContext()

    # ---- 上下文 ----

    def set_context(self, context: Optional[WorldContext]) -> None:
        self._context = context or WorldContext()

    @property
    def context(self) -> WorldContext:
        return self._context

    # ---- 求解 ----

    def _t_world_cam(self) -> Optional[np.ndarray]:
        """相机 -> 世界系。"""
        if self.matrix_frame == "head":
            head = self._context.head_frame
            if head is None:
                # 头会动但拿不到位姿 -> 不能用静态矩阵顶替 (会给出错坐标)
                return None
            return head @ self._matrix
        # matrix_frame == "world" 或 "arm_base"
        if self.matrix_frame == "arm_base":
            # 静态矩阵是「相机 -> 某条臂基座」, 直接用
            return self._matrix
        return self._matrix

    def _base_frame_for(self) -> Optional[np.ndarray]:
        """按 arm 设置取基座的世界位姿。"""
        ctx = self._context
        if self.arm in ("left", "right"):
            m = ctx.base_frame_by_side(self.arm)
            if m is not None:
                return m
            # 名字没匹配上 -> 退回「唯一一条臂」
            if len(ctx.base_frames) == 1:
                return next(iter(ctx.base_frames.values()))
            return None

        # auto: 有几条臂就用哪条 (优先 left, 其次 right)
        for side in ("left", "right"):
            m = ctx.base_frame_by_side(side)
            if m is not None:
                return m
        if ctx.base_frames:
            return next(iter(ctx.base_frames.values()))
        return None

    def get(self) -> Optional[np.ndarray]:
        t_world_cam = self._t_world_cam()
        if t_world_cam is None:
            return None

        target = self.frame
        if target in ("world", "global"):
            return t_world_cam

        if target in ("arm_base", "base"):
            base = self._base_frame_for()
            if base is None:
                return None
            return invert_transform(base) @ t_world_cam

        # left_base / right_base
        side = "left" if "left" in target else "right" if "right" in target else ""
        if not side:
            return None
        base = self._context.base_frame_by_side(side)
        if base is None:
            return None
        return invert_transform(base) @ t_world_cam

    def with_frame(self, frame: str) -> "RigidExtrinsicProvider":
        """复制一份, 换一个目标坐标系。

        上下文 (基座位姿 / 头位姿) 是共享的 —— 换目标系不换上下文。
        """
        return RigidExtrinsicProvider(
            self._matrix,
            matrix_frame=self.matrix_frame,
            frame=frame,
            arm=self.arm,
            context=self._context,
        )

    def describe(self) -> Dict[str, Any]:
        """自述 —— 给 /v1/frames 和 /health 用。"""
        ctx = self._context
        return {
            "matrix_frame": self.matrix_frame,
            "target_frame": self.frame,
            "arm": self.arm,
            "head_compensated": self.matrix_frame == "head",
            "head_frame_available": ctx.head_frame is not None,
            "head_frame_kind": ctx.head_frame_kind,
            "arms_available": ctx.available_sides(),
            "timestamp_ms": ctx.timestamp_ms,
        }


def _axis_permutation_4x4() -> np.ndarray:
    """轴置换的 4x4 形式。"""
    m = np.eye(4, dtype=np.float64)
    m[:3, :3] = AXIS_PERMUTATION_CAMERA_TO_BASE
    return m


class ExtrinsicProviderFactory:
    """按配置造出对应的 provider。

    配置 (``configs/vision.yaml`` 的 ``vision.extrinsic``)::

        extrinsic:
          enabled: false
          matrix: []                    # 4x4, 相机系 -> 目标系
          rotation: []                  # 3x3 (matrix 为空时用)
          translation: [0, 0, 0]
          flip_axes: false              # 老的兼容开关, 见模块文档
          target_frame: "arm_base"      # arm_base / world
          dynamic: false                # true = 需要叠加基座动态偏移

    **matrix_frame** 说明标定出来的矩阵描述的是「相机 -> 哪个系」:
    - ``world``    标定时直接解出的 T_world_cam (眼在手外, 板挂工具)
    - ``head``     T_head_cam (相机 -> 头座), 头会动时用, 运行时补
      T_world_head(q_head)
    - ``arm_base`` T_base_cam (相机 -> 某条臂基座), 静态量
    """

    @staticmethod
    def build(cfg: Any) -> ExtrinsicProvider:
        if cfg is None:
            return StaticExtrinsicProvider(None, frame="camera")

        enabled = bool(getattr(cfg, "enabled", False))
        if not enabled:
            return StaticExtrinsicProvider(None, frame="camera")

        matrix = ExtrinsicProviderFactory._matrix_from_config(cfg)
        if matrix is None:
            logger.warning("外参 enabled=true 但没填 matrix/rotation, 用相机系")
            return StaticExtrinsicProvider(None, frame="camera")

        frame = str(getattr(cfg, "target_frame", "arm_base") or "arm_base")
        flip = bool(getattr(cfg, "flip_axes", False))

        if flip:
            # 兼容老配置: 把轴置换叠在标定结果左边 (标定结果本身是相机系)
            matrix = _axis_permutation_4x4() @ matrix

        # 标定矩阵描述的是「相机 -> 哪个系」
        matrix_frame = str(
            getattr(cfg, "matrix_frame", "world") or "world"
        ).strip().lower()

        dynamic = bool(getattr(cfg, "dynamic", False))
        arm = str(getattr(cfg, "arm", "auto") or "auto")

        if matrix_frame == "head" or dynamic:
            provider = RigidExtrinsicProvider(
                matrix,
                matrix_frame=matrix_frame,
                frame=frame,
                arm=arm,
            )
            if matrix_frame == "head":
                logger.info(
                    "外参: 相机->头座 (头动时用运行时头位姿补偿), 目标系=%s",
                    frame,
                )
            else:
                logger.info("外参: 动态外参, 目标系=%s", frame)
            return provider

        return StaticExtrinsicProvider(matrix, frame=frame)

    @staticmethod
    def _matrix_from_config(cfg: Any) -> Optional[np.ndarray]:
        matrix = getattr(cfg, "matrix", None) or []
        if matrix and len(matrix) == 4:
            try:
                m = np.array(matrix, dtype=np.float64).reshape(4, 4)
                if abs(float(np.linalg.det(m[:3, :3]))) < 1e-9:
                    logger.error("外参 matrix 的旋转部分奇异 (行列式接近 0), 忽略")
                    return None
                return m
            except Exception as exc:
                logger.warning("外参 matrix 格式不对: %s", exc)

        rot = getattr(cfg, "rotation", None) or []
        trans = getattr(cfg, "translation", None) or [0.0, 0.0, 0.0]
        if rot and len(rot) == 3:
            try:
                return make_transform(rot, trans)
            except Exception as exc:
                logger.warning("外参 rotation/translation 格式不对: %s", exc)
        return None


def matrix_from_pose(
    xyz: Sequence[float], rpy: Sequence[float]
) -> np.ndarray:
    """由「位置 + RPY」构造 4x4 (便捷函数, 给标定脚本用)。"""
    m = np.eye(4, dtype=np.float64)
    m[:3, :3] = rpy_to_matrix(rpy)
    m[:3, 3] = np.asarray(xyz, dtype=np.float64).reshape(3)
    return m


def matrix_to_dict(m: np.ndarray, precision: int = 8) -> Dict[str, Any]:
    """4x4 -> 可直接写进 YAML 的嵌套列表 (带四舍五入)。"""
    arr = np.asarray(m, dtype=np.float64)
    return [[round(float(v), precision) for v in row] for row in arr]


__all__ = [
    "AXIS_PERMUTATION_CAMERA_TO_BASE",
    "DynamicExtrinsicProvider",
    "ExtrinsicProvider",
    "ExtrinsicProviderFactory",
    "RigidExtrinsicProvider",
    "StaticExtrinsicProvider",
    "WorldContext",
    "invert_transform",
    "make_transform",
    "matrix_from_pose",
    "matrix_to_dict",
    "matrix_to_rpy",
    "rotation_error_rad",
    "rpy_to_matrix",
    "transform_points",
]
