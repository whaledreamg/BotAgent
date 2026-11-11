"""YOLO 检测 / 姿态估计封装。

用 ultralytics 的 `YOLO` 类。两种模式:
- **检测**: 输出类别 + 框 + 置信度
- **姿态**: 额外输出关键点 (17 个人体关键点, COCO 格式)

姿态模型权重没配时 `pose` 模式会明确报错, 不静默降级 ——
静默降级会让调用方以为拿到的是关键点, 结果是空的。

**设备选择**: `device="auto"` 时有 CUDA 用 GPU, 没���用 CPU。
GPU 驱动异常时 (nvidia-smi 失败) torch.cuda.is_available() 会返回 False,
会自动走 CPU, 不会崩。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger("vision.detector")


@dataclass
class Detection:
    """一个检测结果。"""

    #: 类别名 (YOLO 的 names)
    label: str
    #: 置信度
    confidence: float
    #: 像素框 (x1, y1, x2, y2), 相对彩色图
    bbox: Tuple[float, float, float, float]
    #: 类别 id
    class_id: int = -1
    #: 姿态关键点: [(x, y, conf), ...] (仅姿态模式)
    keypoints: Optional[List[Tuple[float, float, float]]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "label": self.label,
            "confidence": round(float(self.confidence), 4),
            "bbox": [round(float(v), 1) for v in self.bbox],
            "class_id": int(self.class_id),
        }
        if self.keypoints is not None:
            out["keypoints"] = [
                [round(float(x), 1), round(float(y), 1), round(float(c), 3)]
                for x, y, c in self.keypoints
            ]
        return out


class DetectorError(RuntimeError):
    """模型加载或推理失败。"""


class Detector:
    """YOLO 检测器。

    用法::

        det = Detector(cfg.detector)
        det.load()
        results = det.infer(image)
    """

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self._model = None
        self._pose_model = None
        self._device = self._resolve_device()
        self._names: Dict[int, str] = {}

    # ------------------------------------------------------------ 设备

    def _resolve_device(self) -> str:
        """决定用 CPU 还是 GPU。"""
        want = str(getattr(self.cfg, "device", "auto") or "auto").strip().lower()
        if want not in ("auto", ""):
            return want

        try:
            import torch

            if torch.cuda.is_available():
                return "0"
            logger.warning(
                "CUDA 不可用 (GPU 驱动可能异常), YOLO 走 CPU —— "
                "延迟会明显变高"
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("检测 CUDA 状态失败 (%s), 走 CPU", exc)
        return "cpu"

    @property
    def device(self) -> str:
        return self._device

    # ------------------------------------------------------------ 加载

    def load(self) -> None:
        """加载模型。"""
        from ultralytics import YOLO

        weights = str(getattr(self.cfg, "weights", "") or "").strip()
        if not weights:
            raise DetectorError(
                "没配置检测权重 (detector.weights) —— "
                "填本地 .pt 路径, 或填 yolo26n.pt 让 ultralytics 自己下载"
            )

        try:
            self._model = YOLO(weights)
        except Exception as exc:
            raise DetectorError(f"加载检测模型失败 ({weights}): {exc}") from exc

        try:
            self._names = dict(self._model.names)
        except Exception:
            self._names = {}

        logger.info(
            "检测模型已加载: %s (device=%s, 类别数=%d)",
            weights,
            self._device,
            len(self._names),
        )

        # 姿态模型是可选的
        pose_weights = str(getattr(self.cfg, "pose_weights", "") or "").strip()
        if pose_weights:
            try:
                self._pose_model = YOLO(pose_weights)
                logger.info("姿态模型已加载: %s", pose_weights)
            except Exception as exc:
                logger.warning("加载姿态模型失败 (%s): %s", pose_weights, exc)
                self._pose_model = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def pose_loaded(self) -> bool:
        return self._pose_model is not None

    @property
    def class_names(self) -> Dict[int, str]:
        return dict(self._names)

    # ------------------------------------------------------------ 推理

    def infer(
        self,
        image: np.ndarray,
        conf: Optional[float] = None,
        iou: Optional[float] = None,
        imgsz: Optional[int] = None,
        pose: bool = False,
        max_objects: Optional[int] = None,
    ) -> List[Detection]:
        """对一张 BGR 图做检测。

        `pose=True` 时用姿态模型 (需要已配置 pose_weights)。
        """
        if image is None or image.size == 0:
            return []
        if self._model is None:
            raise DetectorError("模型没加载, 先调 load()")

        model = self._model
        if pose:
            if self._pose_model is None:
                raise DetectorError(
                    "没配置姿态模型 (detector.pose_weights) —— "
                    "填 yolo26n-pose.pt 之类的权重"
                )
            model = self._pose_model

        kwargs: Dict[str, Any] = {
            "conf": float(conf if conf is not None else getattr(self.cfg, "conf", 0.35)),
            "iou": float(iou if iou is not None else getattr(self.cfg, "iou", 0.5)),
            "imgsz": int(imgsz if imgsz is not None else getattr(self.cfg, "imgsz", 640)),
            "device": self._device,
            "verbose": False,
        }

        try:
            results = model.predict(image, **kwargs)
        except Exception as exc:
            raise DetectorError(f"推理失败: {exc}") from exc

        if not results:
            return []

        detections = self._parse(results[0], pose=pose)

        limit = int(
            max_objects
            if max_objects is not None
            else getattr(self.cfg, "max_objects", 20)
        )
        if limit > 0:
            detections.sort(key=lambda d: d.confidence, reverse=True)
            detections = detections[:limit]
        return detections

    def _parse(self, result: Any, pose: bool = False) -> List[Detection]:
        """从 ultralytics 的 Results 里抽出检测结果。"""
        out: List[Detection] = []

        boxes = getattr(result, "boxes", None)
        if boxes is None:
            return out

        try:
            xyxy = boxes.xyxy.cpu().numpy()
            confs = boxes.conf.cpu().numpy()
            clss = boxes.cls.cpu().numpy().astype(int)
        except Exception:
            return out

        kps: Optional[np.ndarray] = None
        if pose:
            kp_obj = getattr(result, "keypoints", None)
            if kp_obj is not None:
                try:
                    kps = kp_obj.xy.cpu().numpy()
                    kp_conf = kp_obj.conf.cpu().numpy()
                except Exception:
                    kps = None

        for i in range(len(xyxy)):
            x1, y1, x2, y2 = [float(v) for v in xyxy[i][:4]]
            cls_id = int(clss[i])
            label = str(self._names.get(cls_id, cls_id))

            points: Optional[List[Tuple[float, float, float]]] = None
            if kps is not None and i < len(kps):
                pts = []
                for j in range(len(kps[i])):
                    kx, ky = float(kps[i][j][0]), float(kps[i][j][1])
                    kc = float(kp_conf[i][j]) if kp_conf is not None else 0.0
                    pts.append((kx, ky, kc))
                points = pts

            out.append(
                Detection(
                    label=label,
                    confidence=float(confs[i]),
                    bbox=(x1, y1, x2, y2),
                    class_id=cls_id,
                    keypoints=points,
                )
            )
        return out


__all__ = ["Detection", "Detector", "DetectorError"]
