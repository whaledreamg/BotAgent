"""视觉服务编排: 采帧 -> 检测 -> 解算 -> 缓存。

流程:
    camera.read()      -> Frame (彩色 + 深度 + 内参)
    detector.infer()   -> [Detection]
    solver.solve()     -> Target3D (相机系)
    外参变换            -> Target3D (基座系)

相机不可用时服务仍然能启动 —— /health 会显示 camera_connected=false,
这样 agent 那边能给出明确提示, 而不是整个服务起不来。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from camera import CameraError, Frame, OrbbecCamera
from detector import Detection, Detector, DetectorError
from solver import PoseSolver, Target3D
import class_names

logger = logging.getLogger("vision.service")


def _normalize_frame(frame: str) -> str:
    """把请求里的坐标系名归一化。

    兼容旧写法: ``base`` 就是 ``arm_base``。别的名字一律当相机系 ——
    写错的坐标系名不该悄悄给出错误坐标, 直接退回相机系更安全。

    支持的系:
    - ``camera``      相机系 (X右 Y下 Z前)
    - ``world``       世界系
    - ``arm_base``    机械臂基座系 (按配置里的 arm 选左/右)
    - ``left_base``   左臂基座系
    - ``right_base``  右臂基座系
    """
    name = str(frame or "camera").strip().lower()
    if name in ("base", "arm_base", "arm", "baselink", "base_link"):
        return "arm_base"
    if name in ("world", "global"):
        return "world"
    if name in ("left", "left_base", "left_arm", "l_base"):
        return "left_base"
    if name in ("right", "right_base", "right_arm", "r_base"):
        return "right_base"
    return "camera"


#: 一次请求可以返回的坐标系 (顺序 = 展示顺序)
ALL_FRAMES = ("camera", "world", "left_base", "right_base", "arm_base")


class VisionService:
    """视觉服务。"""

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self.camera = OrbbecCamera(cfg.camera)
        self.detector = Detector(cfg.detector)
        self.solver = PoseSolver(cfg.solver, cfg.extrinsic)

        #: 动态外参的 provider (多臂分档 / 头部补偿时用)
        self._dynamic_provider: Any = None
        self._arm_client: Any = None
        self._world_context: Any = None

        self._lock = threading.Lock()
        self._last_objects: List[Dict[str, Any]] = []
        self._last_ms: int = 0
        self._timestamp_us: int = 0
        self._errors: List[str] = []
        self._started = False

    # ------------------------------------------------------------ 动态外参

    def _setup_dynamic_extrinsic(self) -> None:
        """配置了动态外参时, 准备跟 arm_service 对接。

        **什么时候需要动态**:
        - ``matrix_frame: head`` —— 头会动, 需要运行时补 T_world_head
        - ``dynamic: true``      —— 目标系是某个臂基座, 需要 T_world_base

        链路:

            T_world_cam = T_world_head(q_head) · T_head_cam   (头动)
            T_base_cam  = (T_world_base)⁻¹ · T_world_cam      (分臂基座)
        """
        ext = getattr(self.cfg, "extrinsic", None)
        if ext is None:
            return

        matrix_frame = str(getattr(ext, "matrix_frame", "world") or "world")
        dynamic = bool(getattr(ext, "dynamic", False))
        if matrix_frame != "head" and not dynamic:
            return

        try:
            from extrinsic import (
                ExtrinsicProviderFactory,
                RigidExtrinsicProvider,
                WorldContext,
            )

            matrix = ExtrinsicProviderFactory._matrix_from_config(ext)
            if matrix is None:
                logger.warning("动态外参缺 matrix, 忽略")
                return

            frame = str(getattr(ext, "target_frame", "world") or "world")
            arm = str(getattr(ext, "arm", "auto") or "auto")

            self._world_context = WorldContext()
            self._dynamic_provider = RigidExtrinsicProvider(
                matrix,
                matrix_frame=matrix_frame,
                frame=frame,
                arm=arm,
                context=self._world_context,
            )
            self.solver.set_extrinsic_provider(self._dynamic_provider)
            logger.info(
                "已启用动态外参 (标定系=%s, 目标系=%s, 分臂=%s)",
                matrix_frame, frame, arm,
            )
        except Exception as exc:
            logger.warning("初始化动态外参失败: %s", exc)

    def _refresh_world_context(self) -> None:
        """从 arm_service 拉一次世界系上下文 (基座位姿 / 头位姿)。"""
        if self._dynamic_provider is None or self._world_context is None:
            return

        try:
            from arm_kinematics import ArmKinematicsClient

            if self._arm_client is None:
                url = getattr(
                    getattr(self.cfg, "extrinsic", None), "arm_service_url",
                    "http://127.0.0.1:8802",
                )
                self._arm_client = ArmKinematicsClient(url)

            snap = self._arm_client.snapshot(refresh=False)
            if not snap:
                return

            from extrinsic import WorldContext, matrix_from_pose

            ctx = WorldContext()
            ctx.timestamp_ms = int(snap.get("timestamp_ms") or 0)

            for name, data in (snap.get("arms") or {}).items():
                if not isinstance(data, dict):
                    continue
                base = data.get("base_frame")
                if not isinstance(base, dict):
                    continue
                ctx.base_frames[name] = matrix_from_pose(
                    (base.get("x", 0.0), base.get("y", 0.0), base.get("z", 0.0)),
                    (base.get("a", 0.0), base.get("b", 0.0), base.get("c", 0.0)),
                )

            head = snap.get("head_frame")
            if isinstance(head, dict):
                ctx.head_frame = matrix_from_pose(
                    (head.get("x", 0.0), head.get("y", 0.0), head.get("z", 0.0)),
                    (head.get("a", 0.0), head.get("b", 0.0), head.get("c", 0.0)),
                )
            ctx.head_frame_kind = str(snap.get("head_frame_kind") or "")
            ctx.notes = [str(n) for n in (snap.get("notes") or [])]

            self._world_context = ctx
            self._dynamic_provider.set_context(ctx)
            logger.debug(
                "已更新世界系上下文: 臂=%s 头=%s",
                list(ctx.base_frames), ctx.head_frame is not None,
            )
        except Exception as exc:
            logger.debug("拉世界系上下文失败: %s", exc)

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动服务。相机/模型失败只记录错误, 不抛异常。"""
        try:
            self.detector.load()
        except DetectorError as exc:
            self._record_error(f"检测模型加载失败: {exc}")

        try:
            self.camera.open()
        except CameraError as exc:
            self._record_error(str(exc))

        self._setup_dynamic_extrinsic()

        self._started = True

    def stop(self) -> None:
        try:
            self.camera.close()
        except Exception:  # pragma: no cover
            pass
        self._started = False

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._errors.append(message)
            if len(self._errors) > 20:
                self._errors = self._errors[-20:]
        logger.error("[视觉] %s", message)

    # ------------------------------------------------------------ 检测

    def detect(
        self,
        classes: Optional[List[str]] = None,
        conf: Optional[float] = None,
        pose: bool = False,
        frame: str = "camera",
    ) -> Dict[str, Any]:
        """采一帧做检测, 返回结果字典。"""
        started = time.time()
        frame = _normalize_frame(frame)

        # 动态外参: 每次检测前刷新一次世界系上下文 (基座/头位姿)
        self._refresh_world_context()

        if not self.camera.opened:
            raise CameraError("相机没连上")

        capture = self.camera.read()
        if capture is None:
            raise CameraError("采帧超时")
        if capture.color is None:
            raise CameraError("没拿到彩色帧")

        detections = self.detector.infer(
            capture.color, conf=conf, pose=pose
        )

        # 类别过滤
        if classes:
            wanted = {c.strip().lower() for c in classes if c.strip()}
            detections = [
                d for d in detections if d.label.lower() in wanted
            ]

        depth_m = capture.depth_meters
        objects: List[Dict[str, Any]] = []

        for det in detections:
            # 深度没跟彩色对齐时, 彩色框的像素坐标要映射到深度图上
            bbox = self.camera.map_bbox_to_depth(det.bbox, capture.intrinsics)
            target = self.solver.solve(
                depth_m,
                bbox,
                capture.intrinsics,
                depth_intrinsics=capture.depth_intrinsics,
                depth_to_color=capture.depth_to_color,
            )
            pose3d = self._pose_dict(target, frame)
            obj: Dict[str, Any] = {
                "label": det.label,
                "confidence": round(float(det.confidence), 4),
                "bbox": {
                    "x1": round(det.bbox[0], 1),
                    "y1": round(det.bbox[1], 1),
                    "x2": round(det.bbox[2], 1),
                    "y2": round(det.bbox[3], 1),
                },
                "pose": pose3d,
            }
            if det.keypoints is not None:
                obj["keypoints"] = [
                    {
                        "x": round(k[0], 1),
                        "y": round(k[1], 1),
                        "confidence": round(k[2], 3),
                    }
                    for k in det.keypoints
                ]
            objects.append(obj)

        elapsed = int((time.time() - started) * 1000)

        # 顶层 frame 报**实际拿到的**坐标系, 不是请求的那个。
        # 请求了 left_base 但外参没配好 -> 顶层报 camera,
        # 免得调用方以为拿到的是基座坐标。
        actual_frame = frame
        if frame != "camera":
            actual_frame = self._actual_frame_of(objects)

        with self._lock:
            self._last_objects = objects
            self._last_ms = elapsed
            self._timestamp_us = int(time.time() * 1_000_000)

        return {
            "objects": objects,
            "latency_ms": elapsed,
            "timestamp_us": self._timestamp_us,
            "frame": actual_frame,
            "requested_frame": frame,
        }

    def _actual_frame_of(self, objects: List[Dict[str, Any]]) -> str:
        """从结果里反推「实际坐标系」—— 所有目标一致才算一致。"""
        frames = set()
        for obj in objects:
            pose = obj.get("pose") or {}
            frames.add(str(pose.get("frame", "camera")))
        if len(frames) == 1:
            return frames.pop()
        return "camera"

    def _pose_dict(self, target: Target3D, frame: str) -> Dict[str, Any]:
        """Target3D -> 响应里的 pose 字典。

        frame="camera" 时原样输出; 其它系 (world / left_base / right_base /
        arm_base) 用外参变换; 变换不了就退回相机系并如实标注 ——
        **不假装是基座坐标**。
        """
        out = target.to_dict()
        want = _normalize_frame(frame)

        if want != "camera":
            xyz = self.transform_point(target.as_tuple(), want)
            if xyz is not None:
                out.update({"x": xyz[0], "y": xyz[1], "z": xyz[2], "frame": want})
                return out
            # 外参没配好 —— 如实退回相机系
            out["frame"] = "camera"
            return out

        out["frame"] = "camera"
        return out

    # ------------------------------------------------------------ 坐标变换

    def transform_point(
        self, xyz: Sequence[float], frame: str
    ) -> Optional[Tuple[float, float, float]]:
        """相机系的点 -> 指定的坐标系。

        变换不了返回 None (调用方据此退回相机系)。

        **各目标系怎么来**:
        - ``world``       T_world_cam (头动时 = T_world_head · T_head_cam)
        - ``left_base``   (T_world_base_left)⁻¹ · T_world_cam
        - ``right_base``  (T_world_base_right)⁻¹ · T_world_cam
        - ``arm_base``    按配置里的 ``arm`` 选左/右
        """
        want = _normalize_frame(frame)
        if want == "camera":
            return (float(xyz[0]), float(xyz[1]), float(xyz[2]))

        provider = self._dynamic_provider
        if provider is not None and hasattr(provider, "with_frame"):
            out = provider.with_frame(want).transform(xyz)
            if out is not None:
                return out

        # 静态 provider: 只认它自己的目标系
        out = self.solver.to_base_frame(xyz)
        if out is not None and self.solver.extrinsic_frame() == want:
            return out
        return None

    def frame_snapshot(
        self, xyz: Sequence[float]
    ) -> Dict[str, Any]:
        """一次给出所有坐标系下的同一个点 —— 调试标定用。

        返回结构::

            {
              "camera":    {"x":.., "y":.., "z":..},
              "world":     {"x":.., "y":.., "z":..},
              "left_base": {"x":.., "y":.., "z":..},
              "sources":   {"world": "static_matrix", "left_base": "base_frame:LeftArm"},
              "unavailable": ["right_base"]
            }
        """
        out: Dict[str, Any] = {"sources": {}, "unavailable": []}
        for name in ALL_FRAMES:
            xyz_out = self.transform_point(xyz, name)
            if xyz_out is None:
                out["unavailable"].append(name)
                continue
            out[name] = {
                "x": round(xyz_out[0], 6),
                "y": round(xyz_out[1], 6),
                "z": round(xyz_out[2], 6),
            }
            out["sources"][name] = self._frame_source(name)

        provider = self._dynamic_provider
        if provider is not None and hasattr(provider, "describe"):
            out["provider"] = provider.describe()
        out["extrinsic_frame"] = self.solver.extrinsic_frame()
        return out

    def _frame_source(self, name: str) -> str:
        """这个系的坐标是怎么来的 —— 出问题时能一眼看出。"""
        if name == "camera":
            return "camera"
        provider = self._dynamic_provider
        if provider is None:
            return "static_matrix" if name == self.solver.extrinsic_frame() else "camera"
        if name == "world":
            if getattr(provider, "matrix_frame", "") == "head":
                return "head_frame·head_matrix"
            return "static_matrix"
        side = "left" if "left" in name else "right" if "right" in name else "auto"
        ctx = getattr(provider, "context", None)
        arms = ctx.available_sides() if ctx is not None else []
        if side != "auto" and side in arms:
            return f"base_frame:{side}"
        return "base_frame:auto"

    # ------------------------------------------------------------ 查询

    def last_objects(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._last_objects)

    def find(
        self,
        label: str,
        frame: str = "camera",
    ) -> List[Dict[str, Any]]:
        """按类别名取最近一次检测到的目标。

        找不到就现场跑一次检测 —— 缓存可能是空的 (服务刚起)。

        `label` 可以是 YOLO 的英文类名 ("cup"), 也可以是中文
        ("杯子") —— 中文会先翻译成类名再匹配。

        **置信度阈值要放宽**: 用户明确点名了要找什么, 就该尽力去找 ——
        用默认阈值 (0.35) 会漏掉置信度 0.2~0.3 的目标 (比如半遮挡的
        瓶子、远处的杯子), 让服务回答「没有看到」, 明明目标就在画面里。
        """
        want = (label or "").strip().lower()
        if not want:
            return []

        # 中文 -> YOLO 类名
        names = [c.lower() for c in class_names.candidates(label)]

        with self._lock:
            cached = list(self._last_objects)

        hits = self._match_label(cached, names)
        if hits:
            return self._reframe(hits, frame)

        # 缓存里没有 -> 现场检测一次 (用更宽松的置信度阈值)
        try:
            result = self.detect(conf=self._find_conf(), frame=frame)
        except (CameraError, DetectorError) as exc:
            self._record_error(f"查找 {label} 失败: {exc}")
            return []

        return self._reframe(self._match_label(result["objects"], names), frame)

    def _find_conf(self) -> float:
        """按名字查找时的置信度阈值。

        取「默认阈值」和「宽松阈值」的较小值 —— 默认值配置的更宽松就
        用默认的, 否则用宽松值。
        """
        default = float(getattr(self.cfg.detector, "conf", 0.35))
        loose = float(getattr(self.cfg.detector, "find_conf", 0.15))
        return min(default, loose)

    @staticmethod
    def _match_label(
        objects: List[Dict[str, Any]], names: List[str]
    ) -> List[Dict[str, Any]]:
        """按类名筛出匹配的目标 (大小写不敏感)。"""
        if not names:
            return []
        wanted = {n for n in names if n}
        return [o for o in objects if str(o.get("label", "")).lower() in wanted]

    def _reframe(
        self, objects: List[Dict[str, Any]], frame: str
    ) -> List[Dict[str, Any]]:
        """把缓存结果换到请求的坐标系。

        **缓存里的坐标是相机系** (detect 时按请求的系存过, 这里再转一次
        是幂等的 —— 换到相机系再换回来结果一样)。
        """
        want = _normalize_frame(frame)
        if want == "camera":
            return objects
        out = []
        for obj in objects:
            pose = obj.get("pose") or {}
            xyz = (pose.get("x", 0.0), pose.get("y", 0.0), pose.get("z", 0.0))
            # 缓存里的 pose 可能已经在别的系里了 —— 先转回相机系
            src_frame = _normalize_frame(str(pose.get("frame", "camera")))
            if src_frame != "camera":
                back = self.transform_point(xyz, "camera")
                if back is not None:
                    xyz = back
            converted = self.transform_point(xyz, want)
            new_pose = dict(pose)
            if converted is not None:
                new_pose.update(
                    {"x": converted[0], "y": converted[1], "z": converted[2],
                     "frame": want}
                )
            else:
                new_pose["frame"] = "camera"
            new_obj = dict(obj)
            new_obj["pose"] = new_pose
            out.append(new_obj)
        return out

    def summarize(
        self, objects: Optional[List[Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """按类别聚合 —— Agent 播报用。"""
        objs = objects if objects is not None else self.last_objects()
        groups: Dict[str, Dict[str, Any]] = {}
        for obj in objs:
            label = obj["label"]
            group = groups.setdefault(
                label,
                {"label": label, "count": 0, "pose": None,
                 "confidences": []},
            )
            group["count"] += 1
            group["confidences"].append(obj["confidence"])
            if group["pose"] is None and obj.get("pose"):
                group["pose"] = obj["pose"]
        return list(groups.values())

    # ------------------------------------------------------------ 状态

    def status(self) -> Dict[str, Any]:
        with self._lock:
            errors = list(self._errors)
            last_ms = self._last_ms
            last_count = len(self._last_objects)

        extrinsic_ok = (
            bool(getattr(self.cfg.extrinsic, "enabled", False))
            and self.cfg.extrinsic.is_configured()
        )
        frame = self.solver.extrinsic_frame() if extrinsic_ok else "camera"

        # 动态外参还要看基座位姿有没有拿到 —— 拿不到就等于不可用
        dynamic_ready = True
        if self._dynamic_provider is not None:
            dynamic_ready = self._dynamic_provider.get() is not None

        # 双臂分档 / 头部补偿的状态
        arms_available: List[str] = []
        head_compensation = False
        head_frame_available = False
        provider_info: Dict[str, Any] = {}
        if self._dynamic_provider is not None:
            ctx = getattr(self._dynamic_provider, "context", None)
            if ctx is not None:
                arms_available = ctx.available_sides()
                head_frame_available = ctx.head_frame is not None
            head_compensation = (
                getattr(self._dynamic_provider, "matrix_frame", "") == "head"
            )
            if hasattr(self._dynamic_provider, "describe"):
                provider_info = self._dynamic_provider.describe()

        return {
            "status": "ok",
            "camera_connected": self.camera.opened,
            "camera_name": self.camera.device_info.get("name", ""),
            "camera_serial": self.camera.device_info.get("serial_number", ""),
            "detector_loaded": self.detector.loaded,
            "pose_loaded": self.detector.pose_loaded,
            "device": self.detector.device,
            "weights": str(getattr(self.cfg.detector, "weights", "") or ""),
            "frame": frame,
            "extrinsic_configured": extrinsic_ok,
            "extrinsic_dynamic": self._dynamic_provider is not None,
            "extrinsic_dynamic_ready": dynamic_ready,
            # 标定矩阵描述的是「相机 -> 哪个系」: world / head / arm_base
            "extrinsic_matrix_frame": str(
                getattr(self.cfg.extrinsic, "matrix_frame", "world") or "world"
            ),
            # 双臂: 当前能拿到基座位姿的是哪几条臂
            "arms_available": arms_available,
            # 头会动时的动态补偿
            "head_compensation": head_compensation,
            "head_frame_available": head_frame_available,
            "calibrated_intrinsics": bool(
                getattr(self.cfg.camera, "calibrated_intrinsics", None)
            ),
            "last_detection_ms": last_ms,
            "last_object_count": last_count,
            "errors": errors[-10:],
            "provider": provider_info,
        }


__all__ = ["VisionService"]
