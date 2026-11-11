"""位姿解算: 像素 + 深度 -> 3D 位置, 再按外参转到机械臂基座系。

**相机坐标系 (OpenCV 约定)**:
    X 向右, Y 向下, Z 向前 (沿光轴)

针孔模型反算:
    Z = depth (米)
    X = (u - cx) * Z / fx
    Y = (v - cy) * Z / fy

**深度采样**要小心: 直接取检测框中心一个像素很可能落在物体边缘
(深度跳变) 或者空洞 (0) 上。所以在一个网格上采样, 取中位数。

中位数比均值稳: 物体边缘的深度跳变只会影响少数采样点, 中位数不受影响。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger("vision.solver")


def _histogram_mode(values: np.ndarray) -> Optional[float]:
    """取一组深度值的众数 (直方图峰值)。

    比中位数稳: 中位数在「一半前景一半背景」时会落到中间的
    不存在的深度上, 众数不会。

    bin 宽度取数据自身尺度的 2%, 但至少 1cm, 最多 10cm。
    """
    if values.size == 0:
        return None
    vals = np.asarray(values, dtype=np.float64)
    lo = float(vals.min())
    hi = float(vals.max())
    if hi - lo < 1e-6:
        return lo

    bin_w = max(0.01, min(0.10, (hi - lo) / 50.0))
    nbins = max(1, int(np.ceil((hi - lo) / bin_w)))
    hist, edges = np.histogram(vals, bins=nbins)
    k = int(np.argmax(hist))
    # 用峰值 bin 里的中位数当代表值 (更精确)
    sel = vals[(vals >= edges[k]) & (vals <= edges[k + 1])]
    if sel.size == 0:
        return float(edges[k])
    return float(np.median(sel))


@dataclass
class Target3D:
    """一个目标的三维位置。"""

    #: 相机坐标系下的位置 (米)
    x: float
    y: float
    z: float
    #: 深度是否可信
    depth_valid: bool = True
    #: 检测框内有效深度像素占比
    valid_ratio: float = 0.0
    #: 采样到的深度中位数 (米)
    depth: float = 0.0

    def as_tuple(self) -> Tuple[float, float, float]:
        return (self.x, self.y, self.z)

    def to_dict(self) -> dict:
        return {
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "z": round(self.z, 4),
            "depth_valid": self.depth_valid,
            "valid_ratio": round(self.valid_ratio, 3),
            "depth": round(self.depth, 4),
        }


class PoseSolver:
    """把检测框解算成 3D 位置。"""

    def __init__(self, cfg: Any, extrinsic: Any = None) -> None:
        self.cfg = cfg
        self.extrinsic = extrinsic
        #: 显式注入的外参 provider (动态外参用)
        self._extrinsic_provider: Any = None
        #: 由配置构造的 provider (懒加载)
        self._provider_cache: Any = None

    # ------------------------------------------------------------ 深度采样

    def sample_depth(
        self,
        depth_m: np.ndarray,
        bbox: Sequence[float],
        grid: int = 5,
        depth_min: float = 0.1,
        depth_max: float = 5.0,
    ) -> Tuple[float, float, bool]:
        """在检测框内采样深度。

        返回 (深度中位数米, 占比, 锚点是否可靠)。

        **真实相机的深度是稀疏的** —— 整体有效率常常只有 30%~40%,
        反光/透明的物体表面更是成片空洞。所以既不能简单地「取中心
        一点」(大概率落在空洞上), 也不能「取整框中位数」(背景会混进来)。

        做法是 **从中心向外螺旋搜索锚点**:
        1. 从框中心开始, 一圈圈往外扩, 找到第一圈有有效深度的区域
        2. 那里的深度就是物体表面的深度 (锚点)
        3. 整框里跟锚点深度接近的像素 = 物体表面, 取中位数

        **为什么从中心往外找**: 检测框里不只有物体 —— 细长的物体
        (瓶子、杆子) 的框里大部分是背景。中心往外扩, 第一个碰到有效
        深度的位置, 一定是物体本身 (背景在框的边缘)。

        **占比的含义**: 物体表面像素 / 全部有效深度像素。
        深度稀疏时这个值天然偏低, 所以下限配得比较松 (默认 0.05),
        真正的可靠性由「锚点是否可靠」决定。

        **锚点不可靠**: 锚点是在框的外圈才找到的 (更可能是背景
        而不是物体), 或者整框都找不到有效深度。
        """
        h, w = depth_m.shape[:2]
        x1, y1, x2, y2 = [int(round(v)) for v in bbox[:4]]
        # 裁到图像内
        x1 = max(0, min(x1, w - 1))
        y1 = max(0, min(y1, h - 1))
        x2 = max(x1 + 1, min(x2, w))
        y2 = max(y1 + 1, min(y2, h))
        if x2 <= x1 or y2 <= y1:
            return 0.0, 0.0, False

        patch = depth_m[y1:y2, x1:x2]
        if patch.size == 0:
            return 0.0, 0.0, False

        valid = np.isfinite(patch) & (patch > depth_min) & (patch < depth_max)
        valid_count = int(valid.sum())
        if valid_count == 0:
            return 0.0, 0.0, False

        # ---- 1) 从中心向外螺旋搜索锚点 ----
        anchor, anchor_reliable = self._find_anchor(patch, valid)
        if anchor is None:
            return 0.0, 0.0, False

        # ---- 2) 跟锚点一致的像素 = 物体表面 ----
        tol = max(0.05, anchor * 0.15)
        on_surface = valid & (np.abs(patch - anchor) <= tol)
        surface_count = int(on_surface.sum())
        # 占比 = 物体表面 / 全部有效深度
        ratio = surface_count / float(valid_count)

        if surface_count == 0:
            return anchor, 0.0, anchor_reliable

        surface = patch[on_surface]

        # ---- 3) 网格采样后再取中位数 ----
        # 边缘的深度跳变只会影响少数格子, 中位数不受影响
        if grid <= 1:
            return float(np.median(surface)), ratio, anchor_reliable

        gh, gw = patch.shape
        step_h = max(1, gh // grid)
        step_w = max(1, gw // grid)
        samples: List[float] = []
        for gy in range(0, gh, step_h):
            for gx in range(0, gw, step_w):
                cell_patch = patch[gy: gy + step_h, gx: gx + step_w]
                cell_ok = on_surface[gy: gy + step_h, gx: gx + step_w]
                if not cell_ok.any():
                    continue
                samples.append(float(np.median(cell_patch[cell_ok])))

        if not samples:
            return float(np.median(surface)), ratio, anchor_reliable
        return float(np.median(samples)), ratio, anchor_reliable

    @staticmethod
    def _find_anchor(
        patch: np.ndarray,
        valid: np.ndarray,
    ) -> Tuple[Optional[float], bool]:
        """在检测框里找物体表面的深度锚点。

        返回 (锚点深度米 或 None, 是否可靠)。

        **做法 (三步)**:
        1. 找**离框中心最近的有效深度像素** —— 那一定是物体本身
           (背景都在框的边缘)
        2. 以它的深度为种子, 把整框里深度跟种子接近的像素收进来
           (容差 = 种子深度的 15%, 至少 5cm)
        3. 这批像素的中位数就是锚点 —— 那就是物体表面的深度

        **为什么要「最近的有效像素 + 按深度聚类」**:
        - 只用最近的有效像素: 可能是个孤立的噪点
        - 只用整框众数/中位数: 细长物体 (瓶子、杆子) 的框里背景像素远多于
          物体像素, 会取到背景的深度
        - 用局部小块: 局部块一大就把背景囊进来了 (细长物体尤其明显)

        **容差不够时自动放宽**: 如果按 15% 容差只收到几个像素
        (说明种子可能是噪点), 就把容差逐级放大, 直到收到足够样本。

        **可靠性判据**:
        - 最近的有效像素必须落在框的内圈 (切比雪夫半径 <= 框尺寸的 40%),
          否则它多半是背景
        - 聚类后至少要有 5 个有效深度像素
        """
        gh, gw = patch.shape
        if gh < 1 or gw < 1:
            return None, False

        ys, xs = np.nonzero(valid)
        if ys.size == 0:
            return None, False

        cy = (gh - 1) / 2.0
        cx = (gw - 1) / 2.0

        # ---- 1) 离中心最近的有效像素 ----
        dist = np.maximum(np.abs(ys - cy), np.abs(xs - cx))
        k = int(np.argmin(dist))
        near_y, near_x = int(ys[k]), int(xs[k])
        near_r = float(dist[k])
        seed = float(patch[near_y, near_x])

        # 内圈判据: 超出框尺寸的 40% 就多半是背景了
        inner_limit = 0.4 * max(gh, gw)
        reliable = near_r <= inner_limit

        # ---- 2) 按深度聚类 (容差不够就逐级放宽) ----
        vals = patch[valid]
        anchor = seed
        best_n = 0
        for tol_ratio in (0.15, 0.25, 0.40, 0.60):
            tol = max(0.05, seed * tol_ratio)
            on = vals[np.abs(vals - seed) <= tol]
            if on.size > best_n:
                best_n = on.size
                anchor = float(np.median(on))
            if on.size >= 5:
                break

        if best_n < 5:
            # 聚类样本太少 -> 种子很可能是噪点, 不可信
            return anchor, False

        return anchor, reliable

    # ------------------------------------------------------------ 3D 反算

    def solve(
        self,
        depth_m: Optional[np.ndarray],
        bbox: Sequence[float],
        intrinsics: Any,
        depth_intrinsics: Any = None,
        depth_to_color: Any = None,
    ) -> Target3D:
        """检测框 -> 3D 位置 (彩色相机坐标系)。

        **坐标系的选择**:
        最终输出统一用**彩色相机坐标系**, 因为检测框是在彩色图上的,
        用彩色内参反算 X/Y 最直接。

        - **Z**: 直接取深度值 (深度相机沿光轴量的距离)。
          两个镜头光轴基本平行, 差异 < 1cm。
        - **X/Y**: 用彩色像素 + 彩色内参算。
          深度相机与彩色相机有 ~2.4cm 的横向偏移 (Gemini 336L),
          但 X/Y 由彩色像素决定, 不受这个偏移影响 —— 偏移只影响
          「这个像素对应深度相机哪个像素」, 那部分交给 bbox 映射。

        `bbox` 是**深度图坐标** (由 camera.map_bbox_to_depth 映射而来),
        因为深度值只能从深度图上取。
        """
        cfg = self.cfg
        if depth_m is None:
            return Target3D(x=0.0, y=0.0, z=0.0, depth_valid=False)

        color_intr = intrinsics
        if not getattr(color_intr, "valid", False):
            return Target3D(x=0.0, y=0.0, z=0.0, depth_valid=False)

        depth, ratio, anchor_ok = self.sample_depth(
            depth_m,
            bbox,
            grid=int(getattr(cfg, "sample_grid", 5)),
            depth_min=float(getattr(cfg, "depth_min", 0.1)),
            depth_max=float(getattr(cfg, "depth_max", 5.0)),
        )

        min_ratio = float(getattr(cfg, "min_valid_ratio", 0.05))
        # 深度可信的条件: 锚点在框内圈找到 (是物体而不是背景)
        # 且表面占比够高
        depth_valid = depth > 0 and anchor_ok and ratio >= min_ratio
        if not depth_valid:
            # 深度不可信: 用框中心的深度值给个大概位置, 但标记 invalid
            h, w = depth_m.shape[:2]
            x1, y1, x2, y2 = [int(round(v)) for v in bbox[:4]]
            cx_px = int((max(0, x1) + min(w, x2)) / 2)
            cy_px = int((max(0, y1) + min(h, y2)) / 2)
            cx_px = max(0, min(cx_px, w - 1))
            cy_px = max(0, min(cy_px, h - 1))
            fallback = float(depth_m[cy_px, cx_px])
            if not np.isfinite(fallback) or fallback <= 0:
                # 中心是空洞 -> 保留采样到的深度 (可能来自框内圈),
                # 但标成不可信
                return Target3D(
                    x=0.0,
                    y=0.0,
                    z=depth,
                    depth_valid=False,
                    valid_ratio=ratio,
                    depth=depth,
                )
            depth = fallback

        # 彩色像素: 深度像素 -> 彩色像素 (走归一化像平面)
        u_c, v_c = self._depth_to_color_pixel(
            bbox, depth_intrinsics, color_intr
        )

        x = (u_c - color_intr.cx) * depth / color_intr.fx
        y = (v_c - color_intr.cy) * depth / color_intr.fy

        return Target3D(
            x=x,
            y=y,
            z=depth,
            depth_valid=depth_valid,
            valid_ratio=ratio,
            depth=depth,
        )

    @staticmethod
    def _depth_to_color_pixel(
        bbox: Sequence[float], depth_intr: Any, color_intr: Any
    ) -> Tuple[float, float]:
        """深度图上的框中心 -> 彩色图上的框中心。

        走「归一化像平面」这条路: 两个内参描述同一个相机, 所以
        归一化坐标 (tan 角) 可以直接换算。

        深度图与彩色图分辨率相同时直接返回 (已经是彩色坐标)。
        """
        x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
        du = (x1 + x2) / 2.0
        dv = (y1 + y2) / 2.0

        if depth_intr is None or not getattr(depth_intr, "valid", False):
            return (du, dv)
        if (
            depth_intr.width == color_intr.width
            and depth_intr.height == color_intr.height
        ):
            return (du, dv)

        try:
            nx = (du - depth_intr.cx) / depth_intr.fx
            ny = (dv - depth_intr.cy) / depth_intr.fy
            cu = nx * color_intr.fx + color_intr.cx
            cv = ny * color_intr.fy + color_intr.cy
            return (cu, cv)
        except Exception:
            return (du, dv)

    @staticmethod
    def _to_color_frame(
        xyz: Sequence[float], depth_to_color: Any
    ) -> Tuple[float, float, float]:
        """深度相机坐标系 -> 彩色相机坐标系。

        没给外参 (或外参是单位阵) 时原样返回。
        """
        if depth_to_color is None:
            return (float(xyz[0]), float(xyz[1]), float(xyz[2]))
        try:
            rot, trans = depth_to_color
            p = np.array([xyz[0], xyz[1], xyz[2]], dtype=np.float64)
            out = rot @ p + trans
            return (float(out[0]), float(out[1]), float(out[2]))
        except Exception:
            return (float(xyz[0]), float(xyz[1]), float(xyz[2]))

    @staticmethod
    def _pixel_to_3d(
        u: float, v: float, depth: float, intr: Any
    ) -> Tuple[float, float, float]:
        """像素 + 深度 -> 相机坐标系 3D 点。"""
        x = (u - intr.cx) * depth / intr.fx
        y = (v - intr.cy) * depth / intr.fy
        return (x, y, depth)

    # ------------------------------------------------------------ 外参

    def to_base_frame(self, xyz: Sequence[float]) -> Optional[Tuple[float, float, float]]:
        """相机系 -> 目标坐标系。

        外参没配 (或没启用) 时返回 None —— 调用方据此标 frame="camera"。

        坐标系的语义 (相机系 / 基座系 / 世界系) 由外参 provider 决定,
        这里只负责「有没有」和「怎么变」。
        """
        provider = self._provider()
        if provider is None:
            return None
        return provider.transform(xyz)

    def extrinsic_frame(self) -> str:
        """当前外参对应的目标坐标系名。

        外参不可用时返回 "camera"。
        """
        provider = self._provider()
        if provider is None:
            return "camera"
        return getattr(provider, "frame", "camera")

    def _provider(self):
        """拿外参 provider —— 优先用显式注入的, 其次用配置构造的。"""
        if self._extrinsic_provider is not None:
            return self._extrinsic_provider
        if self._provider_cache is None:
            try:
                from extrinsic import ExtrinsicProviderFactory

                self._provider_cache = ExtrinsicProviderFactory.build(self.extrinsic)
            except Exception as exc:  # pragma: no cover - 配置坏了不该崩服务
                logger.warning("构造外参 provider 失败: %s", exc)
                self._provider_cache = None
        return self._provider_cache

    def set_extrinsic_provider(self, provider: Any) -> None:
        """注入外参 provider (动态外参用)。"""
        self._extrinsic_provider = provider
        self._provider_cache = None


__all__ = ["PoseSolver", "Target3D"]
