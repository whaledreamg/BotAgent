"""标定「会话」的存取 —— 采样 + 原始图 + 元数据, 打包成一份。

**为什么要有这个模块**

标定最花时间的不是解算, 是「摆位姿 -> 拍照 -> 发现这帧角点没检全 ->
重摆」。一次标定要摆 20~30 个位姿, 中途发现外参算错 (比如
``T_tool_board`` 填反了) 就得**全部重来**。

所以标定的产物不该只是「解出来的一个矩阵」, 而应该是**一整套可复现的
原始素材**:

- 每个位姿的**原始彩色图** —— 角点可以随时重新检测
- 每个位姿的**工具世界系位姿** —— 量尺
- 每个位姿的**角点 2D/3D 对应** —— 已经检好的
- **内参 / 板参数 / 工具偏置** —— 解算的输入, 必须记下来

有了这些, 后面任何时候都能**不动机械臂、不再拍照**地重新解算:
改了 ``T_tool_board`` 重新算、换一批内参重新精化、剔除几个坏样本再算。

**文件布局**

::

    <session_dir>/
      session.json      <- 元数据 + 每个采样的记录 (见 SessionData)
      images/
        0000.png
        0001.png
        ...

``session.json`` 的格式::

    {
      "version": 1,
      "created_at": "2026-11-12T17:30:00",
      "arm": "RightArm",
      "board": {"square_length": 0.04, ...},
      "intrinsics": {"camera_matrix": [[...]], "dist_coeffs": [...]},
      "tool_offset": [[...]],          <- 采样时用的工具偏置
      "samples": [
        {
          "index": 0,
          "image": "images/0000.png",
          "t_world_tool": [[...]],
          "t_tool_board": [[...]],
          "t_cam_board": [[...]],
          "obj_points": [[...]],
          "img_points": [[...]],
          "corner_count": 24,
          "board_distance_m": 0.83,
          "label": "RightArm"
        }
      ],
      "rejected": [ {"index": 5, "reason": "角点只检出 6 个"} ]
    }

**向后兼容**: 旧版 ``handeye_samples.json`` 是一个**裸数组**, 没有 meta。
:func:`load_session` 能读它 —— meta 全空, 但角点和位姿都在, 照样能解算。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .board import BoardSpec
from .handeye import PoseSample

logger = logging.getLogger("vision.calib.session")

#: session.json 的格式版本
SESSION_VERSION = 1


# ------------------------------------------------------------ 数据结构


@dataclass
class SessionData:
    """一次标定采样的全部素材。"""

    #: 每个采样 (图像路径相对 session 目录)
    samples: List[PoseSample] = field(default_factory=list)
    #: 采样时用的板参数
    board: Optional[BoardSpec] = None
    #: 采样时用的相机内参 (3x3) 与畸变系数
    camera_matrix: Optional[np.ndarray] = None
    dist_coeffs: Optional[np.ndarray] = None
    #: 采样时用的工具偏置 (4x4)
    tool_offset: Optional[np.ndarray] = None
    #: 用了哪条臂
    arm: str = ""
    #: 采集时间
    created_at: str = ""
    #: 采集过程中被跳过的尝试 (角点不够 / solvePnP 失败等)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    #: session 目录
    root: Path = field(default_factory=Path)

    # ------------------------------------------------------------ 存取

    def to_dict(self) -> Dict[str, Any]:
        def m4(x: Optional[np.ndarray]) -> Optional[List[List[float]]]:
            if x is None:
                return None
            return [[float(v) for v in row] for row in np.asarray(x)]

        def mpts(
            x: Optional[np.ndarray], cols: int
        ) -> Optional[List[List[float]]]:
            """点集 -> [[x, y, ...], ...]。

            **必须先摊成 2 维** —— ``PoseSample.obj_points`` 的形状
            取决于 OpenCV 版本 ((N,3) 或 (N,1,3))。不摊平的话
            ``float(v)`` 会拿到一个数组而不是标量, 直接抛
            "only 0-dimensional arrays can be converted to Python scalars"。
            """
            if x is None:
                return None
            arr = np.asarray(x, dtype=np.float64).reshape(-1, cols)
            return [[float(v) for v in row] for row in arr]

        return {
            "version": SESSION_VERSION,
            "created_at": self.created_at,
            "arm": self.arm,
            "board": self.board.to_dict() if self.board else None,
            "intrinsics": {
                "camera_matrix": m4(self.camera_matrix),
                "dist_coeffs": (
                    None
                    if self.dist_coeffs is None
                    else [float(v) for v in np.asarray(self.dist_coeffs).ravel()]
                ),
            },
            "tool_offset": m4(self.tool_offset),
            "samples": [
                {
                    "index": i,
                    "image": s.image_path,
                    "t_world_tool": m4(s.t_world_tool),
                    "t_tool_board": m4(s.t_tool_board),
                    "t_cam_board": m4(s.t_cam_board),
                    "obj_points": mpts(s.obj_points, 3),
                    "img_points": mpts(s.img_points, 2),
                    "corner_count": s.corner_count,
                    "board_distance_m": round(float(s.board_distance_m), 6),
                    "label": s.label,
                }
                for i, s in enumerate(self.samples)
            ],
            "rejected": list(self.rejected),
        }

    @staticmethod
    def from_dict(
        data: Dict[str, Any], root: Optional[Path] = None
    ) -> "SessionData":
        def arr(x: Any, shape: Optional[Tuple[int, ...]] = None) -> Optional[np.ndarray]:
            if x is None:
                return None
            a = np.array(x, dtype=np.float64)
            if shape is not None and a.shape != shape:
                return None
            return a

        board = None
        if data.get("board"):
            board = BoardSpec.from_dict(data["board"])

        intr = data.get("intrinsics") or {}
        samples: List[PoseSample] = []
        for item in data.get("samples") or []:
            t_cam = arr(item.get("t_cam_board"))
            t_tool = arr(item.get("t_world_tool"))
            if t_cam is None or t_tool is None:
                continue
            t_tb = arr(item.get("t_tool_board"))
            obj = item.get("obj_points")
            img = item.get("img_points")
            samples.append(
                PoseSample(
                    t_cam_board=t_cam,
                    t_world_tool=t_tool,
                    t_tool_board=(
                        t_tb
                        if t_tb is not None and t_tb.shape == (4, 4)
                        else np.eye(4, dtype=np.float64)
                    ),
                    obj_points=(
                        None if obj is None else np.array(obj, dtype=np.float64)
                    ),
                    img_points=(
                        None if img is None else np.array(img, dtype=np.float64)
                    ),
                    corner_count=int(item.get("corner_count", 0)),
                    label=str(item.get("label", "")),
                    image_path=str(item.get("image", "") or ""),
                    board_distance_m=float(item.get("board_distance_m", 0.0) or 0.0),
                )
            )

        return SessionData(
            samples=samples,
            board=board,
            camera_matrix=arr(intr.get("camera_matrix"), (3, 3)),
            dist_coeffs=arr(intr.get("dist_coeffs")),
            tool_offset=arr(data.get("tool_offset"), (4, 4)),
            arm=str(data.get("arm", "")),
            created_at=str(data.get("created_at", "")),
            rejected=list(data.get("rejected") or []),
            root=root or Path("."),
        )

    # ------------------------------------------------------------ 便捷

    def image_paths(self) -> List[Path]:
        """每个采样对应的图片绝对路径 (不存在的跳过)。"""
        out: List[Path] = []
        for s in self.samples:
            if not s.image_path:
                out.append(Path(""))
                continue
            out.append(self.resolve_image(s.image_path))
        return out

    def resolve_image(self, rel: str) -> Path:
        """把 session 内的相对路径解析成绝对路径。"""
        p = Path(rel)
        if p.is_absolute():
            return p
        return self.root / p

    def has_images(self) -> bool:
        """有没有至少一个采样的图片还在。"""
        for s in self.samples:
            if s.image_path and self.resolve_image(s.image_path).exists():
                return True
        return False


# ------------------------------------------------------------ 读写


def save_session(
    session: SessionData,
    path: Path,
    images: Optional[Sequence[Optional[np.ndarray]]] = None,
) -> Path:
    """保存 session (可选地同时写图)。

    ``images`` 与 ``session.samples`` 一一对应 —— BGR 图。为 ``None``
    的项跳过写图 (那一条的 ``image_path`` 会被清空)。

    图片写在 ``<session.json 同级>/images/`` 下, 文件名 ``0000.png`` …
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    img_dir = path.parent / "images"
    if images:
        img_dir.mkdir(parents=True, exist_ok=True)

    for i, s in enumerate(session.samples):
        s.image_path = ""
        if not images or i >= len(images):
            continue
        img = images[i]
        if img is None:
            continue
        rel = f"images/{i:04d}.png"
        try:
            import cv2

            ok = cv2.imwrite(str(img_dir / f"{i:04d}.png"), img)
            if not ok:
                logger.warning("写图失败: %s", rel)
                continue
        except Exception as exc:
            logger.warning("写图失败 (%s): %s", rel, exc)
            continue
        s.image_path = rel

    session.root = path.parent
    session.created_at = session.created_at or datetime.now().isoformat(timespec="seconds")

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(session.to_dict(), fh, ensure_ascii=False, indent=2)
    return path


def load_session(path: Path) -> SessionData:
    """读 session。兼容旧版「裸数组」格式。"""
    path = Path(path)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    if isinstance(data, list):
        # 旧格式: 直接是采样数组, 没有 meta
        logger.info("读到旧版采样格式 (无 meta), 按裸数组解析")
        payload = {
            "samples": [
                {
                    **item,
                    "image": item.get("image_path", ""),
                    "index": i,
                }
                for i, item in enumerate(data)
            ]
        }
        return SessionData.from_dict(payload, root=path.parent)

    return SessionData.from_dict(data, root=path.parent)


__all__ = [
    "SESSION_VERSION",
    "SessionData",
    "load_session",
    "save_session",
]
