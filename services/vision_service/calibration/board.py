"""ChArUco 标定板: 生成 + 检测。

**为什么用 ChArUco 而不是纯棋盘格**:
- 纯棋盘格要求整张板都可见, 视野被挡一点就检不出来;
- ChArUco 由「棋盘格 + 每个格子里的 ArUco 标记」组成, 可以**局部检出**
  (比如板只有一半在画面里), 标定板可以做得更大、精度更高。

**OpenCV 5 的 API 变化** (和 4.x 不一样, 这里都兼容):
- 4.x: ``cv2.aruco.Dictionary_get(DICT_4X4_50)``
- 5.x: ``cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)``
- 4.x: ``cv2.aruco.CharucoBoard(...)``; 5.x 一样, 但必须传 Dictionary 对象
- 检测: ``cv2.aruco.CharucoDetector(board).detectBoard(img)``

**尺寸单位统一用米**, 打印时按 1:1 打印 (别缩放!)。缩放会让标定出的
物理尺寸全错, 外参平移跟着错。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("vision.calib.board")


#: 支持的预定义字典名 (OpenCV 5 的常量名)
DICT_NAMES = (
    "DICT_4X4_50",
    "DICT_4X4_100",
    "DICT_4X4_250",
    "DICT_4X4_1000",
    "DICT_5X5_50",
    "DICT_5X5_100",
    "DICT_5X5_250",
    "DICT_5X5_1000",
    "DICT_6X6_50",
    "DICT_6X6_100",
    "DICT_6X6_250",
    "DICT_6X6_1000",
)


@dataclass
class BoardSpec:
    """标定板的物理规格。

    **这些尺寸直接决定标定精度** —— 必须用尺子量实际的打印结果,
    然后填回来。打印缩放 2% 在 1.5 m 处就是 30 mm 的误差。
    """

    #: 棋盘格边长 (米), 即相邻角点的距离
    square_length: float = 0.040
    #: ArUco 标记边长 (米), 必须小于 square_length
    marker_length: float = 0.030
    #: 棋盘格的列数 (角点数 = (cols-1) x (rows-1))
    cols: int = 7
    #: 棋盘格的行数
    rows: int = 5
    #: 字典名, 如 "DICT_4X4_50"
    dictionary: str = "DICT_4X4_50"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "square_length": self.square_length,
            "marker_length": self.marker_length,
            "cols": self.cols,
            "rows": self.rows,
            "dictionary": self.dictionary,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "BoardSpec":
        return BoardSpec(
            square_length=float(d.get("square_length", 0.040)),
            marker_length=float(d.get("marker_length", 0.030)),
            cols=int(d.get("cols", 7)),
            rows=int(d.get("rows", 5)),
            dictionary=str(d.get("dictionary", "DICT_4X4_50")),
        )


def _dictionary(spec: BoardSpec):
    """拿 ArUco 字典 (兼容 OpenCV 4.x / 5.x)。"""
    import cv2

    ar = cv2.aruco
    name = spec.dictionary
    if name not in DICT_NAMES:
        raise ValueError(
            f"未知的字典 {name!r}, 可选: {', '.join(DICT_NAMES)}"
        )
    const = getattr(ar, name)

    # OpenCV 5: getPredefinedDictionary; 4.x: Dictionary_get
    getter = getattr(ar, "getPredefinedDictionary", None)
    if getter is not None:
        return getter(const)
    return ar.Dictionary_get(const)


def make_board(spec: BoardSpec):
    """构造 ChArUco 板对象。"""
    import cv2

    dictionary = _dictionary(spec)
    return cv2.aruco.CharucoBoard(
        (spec.cols, spec.rows),
        spec.square_length,
        spec.marker_length,
        dictionary,
    )


def board_object_points(spec: BoardSpec) -> np.ndarray:
    """板上所有角点的 3D 坐标 (Nx3, 米)。

    顺序与 ``CharucoDetector.detectBoard`` 返回的角点顺序**一致**
    (由 ``board.matchImagePoints`` 保证)。

    注意: ChArUco 的角点在棋盘格**内**, 不是格点 —— 所以第一个角点的
    坐标不是 (0,0,0), 而是 (square_length, square_length, 0)。
    """
    import cv2

    board = make_board(spec)
    # OpenCV 4.7+: getChessboardCorners(); 更早的 4.x: chessboardCorners 属性
    getter = getattr(board, "getChessboardCorners", None)
    if getter is not None:
        corners = getter()
    else:  # pragma: no cover - 老版本 OpenCV
        corners = getattr(board, "chessboardCorners", None)
        if corners is None:
            raise RuntimeError(
                "这个 OpenCV 版本不支持 ChArUco 角点查询, 请升级到 4.7+"
            )
    return np.asarray(corners, dtype=np.float64)


def generate_board_image(
    spec: BoardSpec, pixels_per_meter: int = 4000
) -> np.ndarray:
    """生成可打印的标定板图像 (灰度图)。

    ``pixels_per_meter`` = 每米多少像素。默认 4000 dpi ≈ 101.6 dpi,
    A4 打印分辨率足够 (角点定位精度 ~0.1 mm)。

    打印时**必须选 100% 原始尺寸**, 不能「适应页面」。
    """
    import cv2

    board = make_board(spec)
    # 板的外形尺寸 = (cols-1) 个格子 + 标记留边
    width_m = spec.cols * spec.square_length
    height_m = spec.rows * spec.square_length
    width = int(round(width_m * pixels_per_meter))
    height = int(round(height_m * pixels_per_meter))

    img = board.generateImage((width, height))
    return img


def detect_charuco(
    img: np.ndarray, spec: BoardSpec
) -> Tuple[np.ndarray, np.ndarray]:
    """检测板上的 ChArUco 角点。

    返回 ``(image_points, object_points)``:
    - ``image_points``: Nx2, 像素坐标
    - ``object_points``: Nx3, 米

    检不出来时返回空数组 (调用方自己决定跳过还是报错)。
    """
    import cv2

    board = make_board(spec)
    detector = cv2.aruco.CharucoDetector(board)

    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    corners, ids, _markers, _mids = detector.detectBoard(gray)
    if ids is None or corners is None or len(ids) == 0:
        return (
            np.empty((0, 2), dtype=np.float32),
            np.empty((0, 3), dtype=np.float64),
        )

    obj_pts, img_pts = board.matchImagePoints(corners, ids)
    obj_pts = np.asarray(obj_pts, dtype=np.float64)
    img_pts = np.asarray(img_pts, dtype=np.float32)

    if obj_pts.ndim == 3 and obj_pts.shape[0] == 1:
        obj_pts = obj_pts[0]
    if img_pts.ndim == 3 and img_pts.shape[0] == 1:
        img_pts = img_pts[0]

    return img_pts, obj_pts


def board_corners_2d(spec: BoardSpec) -> np.ndarray:
    """板上所有角点的 2D 布局 (Nx2, 米), 用于生成打印时的对齐参考。"""
    pts = board_object_points(spec)
    return pts[:, :2]


def render_with_instructions(
    spec: BoardSpec,
    pixels_per_meter: int = 4000,
    margin_m: float = 0.02,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """生成带说明文字的标定板图。

    板子四周留白 (margin), 免得角点紧贴纸边被裁掉。
    """
    import cv2

    img = generate_board_image(spec, pixels_per_meter)
    margin = int(round(margin_m * pixels_per_meter))
    canvas = 255 - np.zeros(
        (img.shape[0] + 2 * margin, img.shape[1] + 2 * margin), dtype=np.uint8
    )
    canvas[margin : margin + img.shape[0], margin : margin + img.shape[1]] = img

    info = {
        "spec": spec.to_dict(),
        "board_width_m": spec.cols * spec.square_length,
        "board_height_m": spec.rows * spec.square_length,
        "pixel_size_m": 1.0 / pixels_per_meter,
        "margin_m": margin_m,
        "image_size": [canvas.shape[1], canvas.shape[0]],
    }
    return canvas, info


__all__ = [
    "BoardSpec",
    "DICT_NAMES",
    "board_corners_2d",
    "board_object_points",
    "detect_charuco",
    "generate_board_image",
    "make_board",
    "render_with_instructions",
]
