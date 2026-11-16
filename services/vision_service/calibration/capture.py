"""采集辅助: 相机取帧 + 控制器位姿查询。

手眼标定的每个采样都要三样东西:
1. 一帧彩色图 (用来解板在相机系里的位姿)
2. 板角点的 3D/2D 对应 (从这帧里检出来)
3. 同一时刻工具 (TCP) 在世界系里的位姿 (控制器给)

**同步问题**: 相机和控制器是两套系统, 没法硬件同步。做法是:
- 先拉控制器的最新位姿 (TCP)
- 再采一帧图
- 再拉一次位姿
- 两次位姿差得小 (说明机械臂几乎没动) 就采这一帧, 否则丢弃重采

机械臂运动时这个策略很有效 —— 采不到静止位姿就跳过, 反正标定
需要的是「位姿不动」的时刻。
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .board import BoardSpec, detect_charuco

logger = logging.getLogger("vision.calib.capture")


# ------------------------------------------------------------ 控制器侧


class StalePoseError(RuntimeError):
    """拿到的位姿不可信 (控制器断开 / 快照卡住 / 位姿不随动)。

    **为什么要专门抛这个**: 手眼标定最贵的成本是「摆位姿 -> 拍照 ->
    发现这帧是坏的 -> 重摆」。如果位姿是假的, 摆 30 个姿态也是白摆。

    所以宁可**立刻中止**, 也不要收一堆废样本。
    """


class ArmKinematicsClient:
    """从 ``arm_service /v1/kinematics`` 取机械臂位姿。

    只依赖 HTTP, 不 import arm_service —— 两个服务独立部署, 视觉服务
    跑在 ``yolo`` 环境, arm_service 跑在 ``botagent`` 环境, 跨环境
    共享代码不合适。

    **会主动检查位姿是不是活的** —— 见 :meth:`require_fresh`。
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8802", timeout: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        #: 上一帧的 TCP 位姿 (用来判断位姿有没有跟着动)
        self._last_pose: Optional[np.ndarray] = None
        #: 上一帧位姿的取数时刻
        self._last_pose_time: float = 0.0

    def _get(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            import urllib.request

            url = f"{self.base_url}{path}"
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                import json

                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.warning("请求 %s 失败: %s", url if "url" in dir() else path, exc)
            return None

    def snapshot(self, refresh: bool = True) -> Optional[Dict[str, Any]]:
        """拿一份运动学快照。

        ``refresh=True`` 会让 arm_service 主动向控制器拉一次最新状态 ——
        标定必须用**当下**的位姿, 状态推送有 50ms 周期, 跟不上快速运动。
        """
        path = "/v1/kinematics?refresh=true" if refresh else "/v1/kinematics"
        return self._get(path)

    @staticmethod
    def is_stale(snapshot: Dict[str, Any]) -> bool:
        """这份快照是不是陈旧的。

        arm_service 会明确标 ``stale`` (控制器断开 / 快照很久没更新)。
        老版本的服务没这个字段 —— 那就退回用 ``controller_connected`` 判断。
        """
        if "stale" in snapshot:
            return bool(snapshot.get("stale"))
        return not bool(snapshot.get("controller_connected", True))

    def require_fresh(
        self, snapshot: Dict[str, Any], arm: str, check_motion: bool = True
    ) -> np.ndarray:
        """取 TCP 位姿, 并**确认它是活的**。不可信就抛 :class:`StalePoseError`。

        三重检查:

        1. ``controller_connected`` / ``stale`` —— 控制器断连了吗
        2. 这条臂有 ``cartesian`` 吗
        3. **位姿有没有跟着动** —— 断连后服务仍在返回 200 + 200ms 前的
           快照, 位姿纹丝不动。这个检查是最后一道防线, 也最重要。

        第 3 条用「和上一帧比」的方式: 摆一个位姿后, TCP 位置至少要变
        一点点 (1 mm 以上)。如果连续多次完全一样, 就是数据卡住了。
        """
        if self.is_stale(snapshot):
            notes = "; ".join(snapshot.get("notes") or [])
            raise StalePoseError(
                "控制器数据陈旧, 不能用于标定"
                + (f" ({notes})" if notes else "")
                + " —— 先确认 arm_service 与控制器连接正常"
            )

        pose = self.tool_pose(snapshot, arm)
        if pose is None:
            raise StalePoseError(f"拿不到 {arm} 的 TCP 位姿 (没有 cartesian)")

        if check_motion:
            now = time.monotonic()
            if self._last_pose is not None:
                moved = float(np.linalg.norm(pose[:3, 3] - self._last_pose[:3, 3]))
                rotated = float(
                    np.abs(pose[:3, :3] - self._last_pose[:3, :3]).max()
                )
                # 两次采样间隔至少 0.5s (人摆位姿没那么快), 位姿却一点没动
                if moved < 1e-4 and rotated < 1e-4 and (now - self._last_pose_time) > 0.5:
                    raise StalePoseError(
                        f"{arm} 的位姿在 {now - self._last_pose_time:.1f}s 内完全没变 "
                        f"(位置差 {moved * 1000:.3f} mm) —— "
                        "读数卡住了, 控制器多半已断开但服务还在返回缓存"
                    )
            self._last_pose = pose.copy()
            self._last_pose_time = now

        return pose

    def reset_motion_tracking(self) -> None:
        """清掉「上一帧位姿」—— 换臂/重新开始时调用。"""
        self._last_pose = None
        self._last_pose_time = 0.0

    @staticmethod
    def pick_arm(snapshot: Dict[str, Any], prefer: str = "") -> Optional[str]:
        """从快照里挑一条臂。

        ``prefer`` 可以是 "left" / "right" / 控制器里的机器人名。
        """
        arms = snapshot.get("arms") or {}
        if not arms:
            return None

        if prefer:
            low = prefer.lower()
            for name in arms:
                n = name.lower()
                if n == low:
                    return name
                if low in ("left", "l") and ("left" in n or n.endswith("l")):
                    return name
                if low in ("right", "r") and ("right" in n or n.endswith("r")):
                    return name

        # 没指定就取第一条有 cartesian 的
        for name, data in arms.items():
            if isinstance(data, dict) and data.get("cartesian"):
                return name
        return next(iter(arms), None)

    @staticmethod
    def tool_pose(snapshot: Dict[str, Any], arm: str) -> Optional[np.ndarray]:
        """取某条臂的 TCP 世界系位姿 (4x4)。

        用的是 ``cartesian`` (世界系) —— 这是标定的「量尺」。
        """
        arms = snapshot.get("arms") or {}
        data = arms.get(arm)
        if not isinstance(data, dict):
            return None
        cart = data.get("cartesian")
        if not isinstance(cart, dict):
            return None

        from ._compat import matrix_from_pose

        xyz = (cart.get("x", 0.0), cart.get("y", 0.0), cart.get("z", 0.0))
        rpy = (cart.get("a", 0.0), cart.get("b", 0.0), cart.get("c", 0.0))
        return matrix_from_pose(xyz, rpy)


# ------------------------------------------------------------ 相机侧


class CameraCapture:
    """相机取帧 (复用 vision_service 的 OrbbecCamera)。"""

    def __init__(self, camera: Any) -> None:
        self.camera = camera

    def grab(self) -> Optional[np.ndarray]:
        """取一帧彩色图 (BGR)。"""
        frame = self.camera.read()
        if frame is None or frame.color is None:
            return None
        return frame.color

    @property
    def intrinsics(self) -> Any:
        """当前生效的彩色相机内参。"""
        return self.camera.intrinsics

    def camera_matrix(self) -> Optional[np.ndarray]:
        """内参 -> 3x3 相机矩阵 (给 solvePnP 用)。"""
        intr = self.intrinsics
        if not getattr(intr, "valid", False):
            return None
        return np.array(
            [
                [intr.fx, 0.0, intr.cx],
                [0.0, intr.fy, intr.cy],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )


# ------------------------------------------------------------ 采样


def capture_sample(
    camera: CameraCapture,
    spec: BoardSpec,
    tool_pose: np.ndarray,
    camera_matrix: Optional[np.ndarray] = None,
    min_corners: int = 8,
    dist_coeffs: Optional[np.ndarray] = None,
    label: str = "",
) -> Optional[Any]:
    """采一个标定采样。

    检不出板 / 角点太少 / 解不出位姿都返回 None。

    **注意**: 采到的那一帧原始彩色图会挂在样本的 ``image`` 属性上
    (``numpy`` 数组), 调用方负责落盘 —— 这样「存图」这件事对
    ``capture_sample`` 是透明的, 失败重试也不会漏存。
    """
    from .handeye import PoseSample, solve_pose

    img = camera.grab()
    if img is None:
        logger.warning("没采到图")
        return None

    img_pts, obj_pts = detect_charuco(img, spec)
    if len(img_pts) < min_corners:
        logger.info("只检出 %d 个角点 (要求 >= %d), 跳过", len(img_pts), min_corners)
        return None

    if camera_matrix is None:
        camera_matrix = camera.camera_matrix()
    if camera_matrix is None:
        logger.warning("拿不到相机内参, 跳过")
        return None

    t_cam_board = solve_pose(img_pts, obj_pts, camera_matrix, dist_coeffs)
    if t_cam_board is None:
        logger.info("solvePnP 失败, 跳过")
        return None

    sample = PoseSample(
        t_cam_board=t_cam_board,
        t_world_tool=np.asarray(tool_pose, dtype=np.float64),
        obj_points=obj_pts,
        img_points=img_pts.astype(np.float64),
        corner_count=len(img_pts),
        label=label,
        board_distance_m=float(abs(t_cam_board[2, 3])),
    )
    #: 原始彩色图 (BGR) —— 存盘由调用方负责
    sample.image = img
    logger.info(
        "采到样本: %d 个角点, 板在相机系 Z=%.3f m",
        len(img_pts), float(t_cam_board[2, 3]),
    )
    return sample


def redetect_from_image(
    image_path: Any,
    spec: BoardSpec,
    camera_matrix: Optional[np.ndarray] = None,
    dist_coeffs: Optional[np.ndarray] = None,
    min_corners: int = 8,
) -> Optional[Tuple[np.ndarray, np.ndarray, Optional[np.ndarray]]]:
    """从**保存的图片**重新检测角点。

    返回 ``(img_points, obj_points, t_cam_board)``; 检不出返回 ``None``。

    **用途**: 标定时存了原始图, 之后想换检测参数 (比如提高角点阈值)
    或重新解 ``solvePnP`` 时, 不用再动机械臂重新采一遍。
    """
    import cv2

    p = str(image_path)
    img = cv2.imread(p)
    if img is None:
        logger.warning("读图失败: %s", p)
        return None

    img_pts, obj_pts = detect_charuco(img, spec)
    if len(img_pts) < min_corners:
        logger.info("%s 只检出 %d 个角点 (要求 >= %d)", p, len(img_pts), min_corners)
        return None

    t_cam_board = None
    if camera_matrix is not None:
        from .handeye import solve_pose

        t_cam_board = solve_pose(img_pts, obj_pts, camera_matrix, dist_coeffs)
        if t_cam_board is None:
            logger.info("%s solvePnP 失败", p)
            return None

    return img_pts.astype(np.float64), obj_pts, t_cam_board


def tool_pose_stable(
    client: ArmKinematicsClient,
    arm: str,
    tolerance_m: float = 0.002,
    max_wait_s: float = 10.0,
) -> Optional[np.ndarray]:
    """等到机械臂停稳再取位姿。

    连续两次快照的位置差小于 ``tolerance_m`` 才认为停稳。

    为什么要等停稳: 机械臂在动的时候, 相机和控制器之间有几十毫秒的
    时间差, TCP 位姿和图像对不上, 采到的样本是错的。

    **断连检测**: 控制器断开时服务仍然返回 200 + 陈旧快照, 位置差会
    一直「小于容差」—— 看起来像「停稳了」, 实际是**冻结**。
    所以这里额外检查快照本身是否陈旧 (``stale`` / ``controller_connected``),
    陈旧就抛 :class:`StalePoseError`, 让上层立刻中止, 别收废样本。
    """
    deadline = time.time() + max_wait_s
    previous: Optional[np.ndarray] = None

    while time.time() < deadline:
        snap = client.snapshot(refresh=True)
        if not snap:
            time.sleep(0.2)
            continue

        # 快照陈旧 -> 立刻报错 (而不是当成「停稳了」)
        if client.is_stale(snap):
            notes = "; ".join(snap.get("notes") or [])
            raise StalePoseError(
                "控制器数据陈旧, 不能用于标定"
                + (f" ({notes})" if notes else "")
            )

        pose = client.tool_pose(snap, arm)
        if pose is None:
            time.sleep(0.2)
            continue

        if previous is not None:
            delta = float(np.linalg.norm(pose[:3, 3] - previous[:3, 3]))
            if delta <= tolerance_m:
                return pose
        previous = pose
        time.sleep(0.15)

    logger.warning("等 %s 停稳超时 (%.1fs)", arm, max_wait_s)
    return None


__all__ = [
    "ArmKinematicsClient",
    "CameraCapture",
    "StalePoseError",
    "capture_sample",
    "redetect_from_image",
    "tool_pose_stable",
]
