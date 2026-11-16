#!/usr/bin/env python3
"""标定采样可视化 —— 把角点画在保存的图上, 一眼看出哪帧是坏的。

**为什么需要这个**

采样时屏幕上只显示「检出 N 个角点」, 看不到板在画面里是什么样 ——
板被挡了一半? 角度太斜? 曝光过度? 全都看不出来。

结果就是: 采完 20 个位姿, 解算失败, 不知道是哪一帧的问题, 只能
重新摆机械臂重采。

这个工具把每一帧画出来 (角点 + 序号 + 板在相机系里的距离),
**不用动机械臂**就能挑出坏帧, 然后用 ``--exclude`` 剔掉。

用法::

    # 生成叠加图到 /tmp/calib_views/
    conda run -n yolo python tools/calib_view.py --samples /tmp/handeye_samples.json

    # 也重检测一遍 (用保存的图, 看当前检测参数下角点对不对)
    conda run -n yolo python tools/calib_view.py --samples /tmp/handeye_samples.json --redetect

    # 只看某几帧
    conda run -n yolo python tools/calib_view.py --samples /tmp/handeye_samples.json --only 0,3,7

    # 直接拼成一张大图 (方便一眼看完)
    conda run -n yolo python tools/calib_view.py --samples /tmp/handeye_samples.json --montage

输出::

    <out-dir>/
      0000.png      <- 角点叠加图
      0001.png
      ...
      montage.png   <- --montage 时生成 (所有帧拼成一张)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.board import BoardSpec  # noqa: E402
from calibration.capture import redetect_from_image  # noqa: E402
from calibration.paths import config_path  # noqa: E402
from calibration.session import SessionData, load_session  # noqa: E402


def load_spec(session: Optional[SessionData]) -> BoardSpec:
    """板参数: session 里记的优先, 否则读配置文件。"""
    if session is not None and session.board is not None:
        return session.board
    path = config_path("calibration.yaml")
    if not path.exists():
        return BoardSpec()
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        section = data.get("vision_calibration", data)
        return BoardSpec.from_dict(section.get("board", {}))
    except Exception:
        return BoardSpec()


def _intrinsics(session: Optional[SessionData]) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    """内参: session 里记的优先, 否则读 configs/intrinsics.json。"""
    if session is not None and session.camera_matrix is not None:
        return session.camera_matrix, session.dist_coeffs
    path = config_path("intrinsics.json")
    if not path.exists():
        return None, None
    try:
        import json

        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return (
            np.array(data["camera_matrix"], dtype=np.float64),
            np.array(data["dist_coeffs"], dtype=np.float64),
        )
    except Exception:
        return None, None


def draw_overlay(
    img: np.ndarray,
    img_pts: Optional[np.ndarray],
    title: str,
    subtitle: str = "",
) -> np.ndarray:
    """在图上画角点 + 文字说明。"""
    import cv2

    vis = img.copy()
    if img_pts is not None and len(img_pts):
        pts = np.asarray(img_pts, dtype=np.float64).reshape(-1, 2)
        for i, (u, v) in enumerate(pts):
            cv2.circle(vis, (int(round(u)), int(round(v))), 7, (0, 220, 0), 2)
            cv2.putText(
                vis, str(i), (int(round(u)) + 9, int(round(v)) - 6),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 140, 255), 2,
            )
        # 板的外接框
        x1 = int(pts[:, 0].min()); y1 = int(pts[:, 1].min())
        x2 = int(pts[:, 0].max()); y2 = int(pts[:, 1].max())
        cv2.rectangle(vis, (x1, y1), (x2, y2), (255, 120, 0), 2)

    # 标题条
    bar = min(58, vis.shape[0])
    overlay = vis.copy()
    cv2.rectangle(overlay, (0, 0), (vis.shape[1], bar), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.62, vis, 0.38, 0, vis)

    cv2.putText(vis, title, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.68,
                (255, 255, 255), 2)
    if subtitle:
        cv2.putText(vis, subtitle, (12, 47), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (170, 230, 255), 1)
    return vis


def make_montage(tiles: List[np.ndarray], cols: int = 4) -> Optional[np.ndarray]:
    """把若干图拼成一张大图。"""
    if not tiles:
        return None
    import cv2

    th = min(t.shape[0] for t in tiles)
    tw = min(t.shape[1] for t in tiles)
    resized = []
    for t in tiles:
        scale = min(th / t.shape[0], tw / t.shape[1])
        r = cv2.resize(
            t, (int(t.shape[1] * scale), int(t.shape[0] * scale)),
            interpolation=cv2.INTER_AREA,
        )
        canvas = np.zeros((th, tw, 3), dtype=np.uint8)
        canvas[: r.shape[0], : r.shape[1]] = r
        resized.append(canvas)

    cols = max(1, min(cols, len(resized)))
    rows = (len(resized) + cols - 1) // cols
    H, W = th, tw
    out = np.zeros((rows * H, cols * W, 3), dtype=np.uint8)
    for i, t in enumerate(resized):
        rr = i // cols
        cc = i % cols
        out[rr * H : rr * H + H, cc * W : cc * W + W] = t
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="标定采样可视化")
    parser.add_argument("--samples", required=True, help="采样 session JSON")
    parser.add_argument("--out-dir", default="/tmp/calib_views", help="输出目录")
    parser.add_argument(
        "--redetect", action="store_true",
        help="用保存的图重新检测角点 (看当前检测参数下的效果)",
    )
    parser.add_argument(
        "--min-corners", type=int, default=8, help="重检测时的角点数下限",
    )
    parser.add_argument("--only", default="", help="只看这些帧, 如 0,3,7")
    parser.add_argument(
        "--montage", action="store_true", help="额外生成一张拼接大图",
    )
    args = parser.parse_args()

    path = Path(args.samples)
    if not path.exists():
        print(f"找不到 {path}", file=sys.stderr)
        return 1

    session = load_session(path)
    if not session.samples:
        print("session 里没有采样", file=sys.stderr)
        return 1
    if not session.has_images():
        print(
            "这个 session 没有原始图 —— "
            "只有存了图才能可视化 (旧版采样格式没存图)",
            file=sys.stderr,
        )
        return 1

    spec = load_spec(session)
    cam_m, dist_c = _intrinsics(session)

    only: set = set()
    for part in args.only.replace(" ", "").split(","):
        if part:
            try:
                only.add(int(part))
            except ValueError:
                pass

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tiles = []
    n_ok = 0
    print(f"逐帧处理 {len(session.samples)} 个采样...")
    for i, s in enumerate(session.samples):
        if only and i not in only:
            continue
        img_path = session.resolve_image(s.image_path)
        if not img_path.exists():
            print(f"  [{i}] 图没了: {img_path}")
            continue

        import cv2

        img = cv2.imread(str(img_path))
        if img is None:
            print(f"  [{i}] 读图失败: {img_path}")
            continue

        pts = s.img_points
        n_corners = s.corner_count
        dist = s.board_distance_m
        note = ""

        if args.redetect:
            res = redetect_from_image(
                img_path, spec, cam_m, dist_c, min_corners=args.min_corners
            )
            if res is None:
                note = "重检测失败"
                pts = None
                n_corners = 0
            else:
                pts, _obj, t_cam = res
                n_corners = len(pts)
                dist = float(abs(t_cam[2, 3]))

        title = f"#{i}  corners={n_corners}  dist={dist:.2f}m  {s.label}"
        if note:
            title += f"  [{note}]"

        vis = draw_overlay(img, pts, title, s.image_path)
        out_path = out_dir / f"{i:04d}.png"
        cv2.imwrite(str(out_path), vis)
        tiles.append(vis)
        n_ok += 1
        print(f"  [{i}] 角点 {n_corners} 个, 距离 {dist:.2f} m -> {out_path}")

    if args.montage and tiles:
        mg = make_montage(tiles)
        if mg is not None:
            mp = out_dir / "montage.png"
            import cv2

            cv2.imwrite(str(mp), mg)
            print(f"\n拼接图: {mp}  ({mg.shape[1]}x{mg.shape[0]})")

    print(f"\n共 {n_ok} 帧, 输出到 {out_dir}")
    print("提示: 角点少 / 板被挡 / 角度太斜的帧就是坏帧, 用 --exclude 剔掉")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
