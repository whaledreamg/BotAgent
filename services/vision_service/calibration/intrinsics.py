"""内参标定: 用 ChArUco 板重标相机的针孔内参。

**为什么必须做**: SDK 给的是**标称内参**, 不是实测值。1% 的内参误差在
1.5 m 处就是 15 mm 的横向误差 —— 跟手眼外参的误差同量级。不先修内参,
外参标得再准也没用。

**流程**:
1. 打印 ChArUco 板 (见 ``board.py``), 量准实际尺寸填进 ``BoardSpec``
2. 在**实际工作距离** (0.5~2.0 m) 拍 20~30 张, 覆盖画面不同区域和角度
3. ``calibrate()`` 解出 fx/fy/cx/cy + 畸变系数
4. 用 ``write_intrinsics()`` 覆盖出厂内参

**注意**: 内参是**相机流**的属性, 换分辨率就失效。改分辨率要重标。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .board import BoardSpec, detect_charuco

logger = logging.getLogger("vision.calib.intrinsics")


@dataclass
class IntrinsicResult:
    """内参标定结果。"""

    #: 3x3 相机矩阵
    camera_matrix: np.ndarray
    #: 畸变系数 (长度 5 或更多, 取决于标定)
    dist_coeffs: np.ndarray
    #: 每张图的重投影误差 (像素)
    reprojection_errors: List[float] = field(default_factory=list)
    #: 参与标定的图片数
    image_count: int = 0
    #: 标定时的图片分辨率
    image_size: Tuple[int, int] = (0, 0)
    #: 整体 RMS 重投影误差 (像素)
    rms: float = 0.0

    @property
    def mean_error(self) -> float:
        if not self.reprojection_errors:
            return 0.0
        return float(np.mean(self.reprojection_errors))

    @property
    def max_error(self) -> float:
        if not self.reprojection_errors:
            return 0.0
        return float(np.max(self.reprojection_errors))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "camera_matrix": [
                [round(float(v), 6) for v in row] for row in self.camera_matrix
            ],
            "dist_coeffs": [round(float(v), 8) for v in self.dist_coeffs.ravel()],
            "rms": round(self.rms, 4),
            "mean_error": round(self.mean_error, 4),
            "max_error": round(self.max_error, 4),
            "image_count": self.image_count,
            "image_size": list(self.image_size),
        }

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
        logger.info("内参已保存到 %s", path)

    @staticmethod
    def load(path: Path) -> "IntrinsicResult":
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return IntrinsicResult(
            camera_matrix=np.array(d["camera_matrix"], dtype=np.float64),
            dist_coeffs=np.array(d["dist_coeffs"], dtype=np.float64),
            image_size=tuple(d.get("image_size", (0, 0))),  # type: ignore[arg-type]
            rms=float(d.get("rms", 0.0)),
        )


def calibrate(
    images: Sequence[np.ndarray],
    spec: BoardSpec,
    fix_k3: bool = True,
) -> Optional[IntrinsicResult]:
    """用一组图像标定内参。

    ``images`` 里每张图都要能看到板的一部分 (至少 6 个角点)。

    标定不出来 (图像太少 / 板都只在一个角度 / 尺寸不一致) 返回 None。
    """
    import cv2

    obj_points: List[np.ndarray] = []
    img_points: List[np.ndarray] = []
    used_sizes: List[Tuple[int, int]] = []
    used_images: List[np.ndarray] = []

    for img in images:
        if img is None:
            continue
        img_pts, obj_pts = detect_charuco(img, spec)
        if len(img_pts) < 6:
            logger.info("这张图只检出 %d 个角点, 跳过", len(img_pts))
            continue
        obj_points.append(obj_pts.astype(np.float32))
        img_points.append(img_pts.astype(np.float32))
        used_sizes.append((img.shape[1], img.shape[0]))
        used_images.append(img)

    if len(obj_points) < 3:
        logger.error("只有 %d 张图可用, 至少要 3 张", len(obj_points))
        return None

    # 分辨率必须一致 —— 不一致就没法一起标
    if len(set(used_sizes)) > 1:
        logger.error("图片分辨率不一致: %s", sorted(set(used_sizes)))
        return None

    image_size = used_sizes[0]

    rms, cam_matrix, dist, rvecs, tvecs = cv2.calibrateCamera(
        obj_points,
        img_points,
        image_size,
        None,
        None,
    )

    # 逐图重投影误差
    errors: List[float] = []
    for i, (obj, imgp) in enumerate(zip(obj_points, img_points)):
        proj, _ = cv2.projectPoints(obj, rvecs[i], tvecs[i], cam_matrix, dist)
        err = float(
            np.linalg.norm(
                proj.reshape(-1, 2) - imgp.reshape(-1, 2).astype(np.float64),
                axis=1,
            ).mean()
        )
        errors.append(err)

    logger.info(
        "内参标定完成: %d 张图, RMS=%.3f px, 平均=%.3f px, 最大=%.3f px",
        len(obj_points), rms, float(np.mean(errors)), float(np.max(errors)),
    )

    return IntrinsicResult(
        camera_matrix=np.asarray(cam_matrix, dtype=np.float64),
        dist_coeffs=np.asarray(dist, dtype=np.float64),
        reprojection_errors=errors,
        image_count=len(obj_points),
        image_size=image_size,
        rms=float(rms),
    )


def reprojection_error(
    result: IntrinsicResult,
    images: Sequence[np.ndarray],
    spec: BoardSpec,
) -> List[float]:
    """用标定好的内参重新算每张图的重投影误差 (验证用)。"""
    import cv2

    errors: List[float] = []
    for img in images:
        img_pts, obj_pts = detect_charuco(img, spec)
        if len(img_pts) < 6:
            continue
        ok, rvec, tvec = cv2.solvePnP(
            obj_pts.astype(np.float64),
            img_pts.astype(np.float64),
            result.camera_matrix,
            result.dist_coeffs.ravel(),
        )
        if not ok:
            continue
        proj, _ = cv2.projectPoints(
            obj_pts.astype(np.float64), rvec, tvec,
            result.camera_matrix, result.dist_coeffs.ravel(),
        )
        err = float(
            np.linalg.norm(
                proj.reshape(-1, 2) - img_pts.reshape(-1, 2).astype(np.float64),
                axis=1,
            ).mean()
        )
        errors.append(err)
    return errors


__all__ = ["IntrinsicResult", "calibrate", "reprojection_error"]
