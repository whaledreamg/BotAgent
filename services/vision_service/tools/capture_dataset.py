#!/usr/bin/env python3
"""采图工具 —— 给自定义 YOLO 模型准备训练数据。

复用 vision_service 的 `OrbbecCamera`, 所以采出来的图和线上推理
看到的是**同一路画面** (同样的分辨率、同样的 D2C 对齐、同样的色彩),
不会有"训练用一套图、部署用另一套图"的域差。

## 相机是独占的

Orbbec 是 USB 独占设备 —— 视觉服务在跑的时候, 别的进程开不了相机
(报 `uvc_open failed -6`)。所以采图前先:

    ./scripts/stop_all.sh

## 用法

    # 先看看相机能不能开 (不开相机流, 只做连通性检查)
    conda run -n yolo python tools/capture_dataset.py --check

    # 采 200 张到默认数据集目录
    conda run -n yolo python tools/capture_dataset.py --count 200

    # 指定输出目录 + 每张间隔 1 秒 (采慢一点, 场景变化更丰富)
    conda run -n yolo python tools/capture_dataset.py \
        --out ../../datasets/myobjects --count 300 --interval 1.0

    # **手动模式** —— 每张需按 Enter 触发，中间可调整场景 (推荐用这个!)
    conda run -n yolo python tools/capture_dataset.py --count 100 --manual

    # 只采负样本 (画面里没有目标物体) —— 抑制误检
    conda run -n yolo python tools/capture_dataset.py --count 50 --negative

    # 按 80/20 切分 train/val (默认就是 80/20)
    conda run -n yolo python tools/capture_dataset.py --count 100 --split 0.8

## 覆盖度比数量重要

采图时**主动变化**这些维度, 比闷头拍 500 张同一个场景有用得多:

- **距离**: 0.3 / 0.6 / 1.0 / 1.5 m (相机在头部, 物体到相机的距离跨度大)
- **角度**: 正对 / 斜 45° / 俯视 / 侧视
- **光照**: 正常 / 偏暗 / 强反光
- **背景**: 不同工位 / 不同桌面材质 (防止模型记住背景而非物体)
- **负样本**: 画面里**没有**目标物体 (防止误检 —— 抓取场景尤其重要)

脚本会在每张图旁边记一个 meta.json, 记录这张图采的时候的深度中位数
(粗略的距离档位), 方便标注时知道这张属于哪个距离组。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from camera import CameraError, OrbbecCamera  # noqa: E402
from config import load_config  # noqa: E402
from paths import resolve_data_dir  # noqa: E402

#: 默认数据集根目录 (仓库内 datasets/ —— 数据集本身不该进 git,
#: 目录结构进, 图片不进)
DEFAULT_OUT = resolve_data_dir() / "myobjects"

#: 粗略的距离档位 (米) —— 用来统计覆盖度
DISTANCE_BUCKETS = (0.4, 0.8, 1.2, 2.0)


def bucket_of(distance: Optional[float]) -> str:
    """把距离归到档位。"""
    if distance is None or distance <= 0:
        return "unknown"
    for edge in DISTANCE_BUCKETS:
        if distance < edge:
            return f"<{edge:.1f}m"
    return f">={DISTANCE_BUCKETS[-1]:.1f}m"


class CaptureError(RuntimeError):
    """采图失败。"""


# ------------------------------------------------------------ 服务占用检测


def vision_service_running(url: str = "http://127.0.0.1:8803") -> bool:
    """视觉服务在跑的话相机被占着, 采图一定失败。"""
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{url}/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


# ------------------------------------------------------------ 采图


class DatasetCapture:
    """边采边切分 train/val。"""

    def __init__(
        self,
        out_dir: Path,
        split: float = 0.8,
        negative_ratio: float = 0.0,
        interval: float = 0.5,
        prefix: str = "img",
        manual: bool = False,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.split = float(split)
        self.negative_ratio = float(negative_ratio)
        self.interval = float(interval)
        self.prefix = prefix
        self.manual = manual

        self.samples: List[Dict[str, Any]] = []
        self._index = 0

    # ------------------------------------------------------------ 目录

    def prepare(self) -> None:
        """建好目录结构。

        **注意**: 这里只建图片目录, **不建 label 目录下的空 txt** ——
        ultralytics 靠"有图无 label"来表示负样本, 提前建空 txt
        反而会让它当成"标注过的空图"。
        """
        for split in ("train", "val"):
            (self.out_dir / "images" / split).mkdir(parents=True, exist_ok=True)
            (self.out_dir / "labels" / split).mkdir(parents=True, exist_ok=True)
        print(f"[目录] {self.out_dir}")
        for split in ("train", "val"):
            print(f"       images/{split}  labels/{split}")

    def _next_split(self) -> str:
        """按 split 比例轮流分配 train/val。

        用"轮流"而不是"随机" —— 随机分配需要先采完再 shuffle, 而
        轮流分配能保证**采到一半中断了也有 train 和 val**。
        """
        idx = self._index
        # 前 split 比例给 train, 剩下的给 val
        # 用取模把 index 映射到 [0, 1) 的连续位置上
        pos = (idx % 100) / 100.0
        return "train" if pos < self.split else "val"

    # ------------------------------------------------------------ 采集

    def capture(
        self,
        count: int,
        camera: OrbbecCamera,
        negative: bool = False,
    ) -> None:
        """采 count 张图。"""
        for i in range(count):
            # 手动模式：等待用户触发
            if self.manual:
                if i > 0:
                    input(f"\n[等待] 调整场景后，按 Enter 采第 {i + 1} 张 (共 {count} 张)...")
                else:
                    print(f"\n[准备] 第 1 张准备完毕，按 Enter 开始采集...")
                    input()

            frame = camera.read()
            if frame is None or frame.color is None:
                print(f"[{i + 1}/{count}] 没采到帧, 跳过")
                continue

            # 深度中位数 -> 距离档位 (只用来统计覆盖度)
            distance = None
            depth = getattr(frame, "depth_meters", None)
            if depth is not None:
                try:
                    import numpy as np

                    valid = depth[(depth > 0.1) & (depth < 5.0)]
                    if valid.size:
                        distance = float(np.median(valid))
                except Exception:
                    distance = None

            split = self._next_split()
            name = f"{self.prefix}_{self._index:05d}"
            img_path = self.out_dir / "images" / split / f"{name}.jpg"

            try:
                import cv2

                if not cv2.imwrite(str(img_path), frame.color):
                    raise CaptureError(f"写文件失败: {img_path}")
            except CaptureError:
                raise
            except Exception as exc:
                raise CaptureError(f"写文件失败: {exc}") from exc

            meta = {
                "file": str(img_path.relative_to(self.out_dir)),
                "split": split,
                "distance_m": round(distance, 3) if distance is not None else None,
                "bucket": bucket_of(distance),
                "negative": bool(negative),
                "timestamp": time.time(),
            }
            self.samples.append(meta)
            self._index += 1

            bucket = meta["bucket"]
            dist_txt = f"{distance:.2f}m" if distance is not None else "无深度"
            tag = " [负样本]" if negative else ""
            print(f"[✓] {split:5s} {name} 距离={dist_txt} ({bucket}){tag}")

            if not self.manual and self.interval > 0 and i < count - 1:
                time.sleep(self.interval)

    # ------------------------------------------------------------ 统计

    def save_meta(self) -> Path:
        """把采集元信息写成 meta.json。"""
        out = self.out_dir / "meta.json"
        out.write_text(
            json.dumps(self.samples, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return out

    def report(self) -> None:
        """打印覆盖度统计 —— 这是采图质量的自检。"""
        if not self.samples:
            print("\n[统计] 一张都没采到")
            return

        total = len(self.samples)
        by_split: Dict[str, int] = {}
        by_bucket: Dict[str, int] = {}
        negatives = 0

        for s in self.samples:
            by_split[s["split"]] = by_split.get(s["split"], 0) + 1
            by_bucket[s["bucket"]] = by_bucket.get(s["bucket"], 0) + 1
            if s.get("negative"):
                negatives += 1

        print("\n" + "=" * 60)
        print("采集统计")
        print("=" * 60)
        print(f"  总计: {total} 张")
        for split, n in sorted(by_split.items()):
            print(f"    {split:6s} {n:5d} 张 ({n / total * 100:.0f}%)")

        print("\n  距离档位分布:")
        for bucket in sorted(by_bucket):
            n = by_bucket[bucket]
            bar = "#" * max(1, int(n / total * 40))
            print(f"    {bucket:10s} {n:5d} ({n / total * 100:4.1f}%) {bar}")

        print(f"\n  负样本: {negatives} 张 ({negatives / total * 100:.0f}%)")
        if negatives == 0:
            print("    [warn] 没有负样本 —— 容易误检, 建议再采 15~20%")
        elif negatives / total < 0.15:
            print("    [warn] 负样本偏少 —— 建议占比 15~20%")

        print("\n  [注意] 距离/角度/光照的覆盖度还得靠你自己现场变化 ——")
        print("         脚本只能统计距离, 拍的时候要主动换角度和背景。")


# ------------------------------------------------------------ data.yaml


def write_data_yaml(out_dir: Path, names: Optional[List[str]] = None) -> Path:
    """生成 data.yaml 骨架。

    类别名留空 —— 等标注完再填。ultralytics 要求 `names` 非空才能训,
    所以这里给几个占位, 提醒用户去改。
    """
    out = out_dir / "data.yaml"
    if names:
        names_block = "\n".join(f"  {i}: {n}" for i, n in enumerate(names))
    else:
        names_block = "\n".join(
            f"  {i}: 类名{i}" for i in range(3)
        )

    out.write_text(
        f"""# YOLO 数据集配置
#
# **names 一定要改成你实际的类别名** —— 类名会直接进 model.names,
# find_object 是靠类名做匹配的 (见 docs/vision-training.md)。
#
# 建议自定义类别**直接用中文**: 用户说"螺丝", 类名就叫"螺丝",
# 匹配就通了。用英文名的话还得往 class_names.py 补映射。
path: {out_dir}
train: images/train
val: images/val

names:
{names_block}
""",
        encoding="utf-8",
    )
    return out


# ------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description="采图工具 (YOLO 训练数据)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="数据集根目录")
    ap.add_argument("--count", type=int, default=100, help="采多少张")
    ap.add_argument("--interval", type=float, default=0.5, help="每张间隔秒数")
    ap.add_argument("--split", type=float, default=0.8, help="train 占比 (0~1)")
    ap.add_argument("--negative", action="store_true",
                    help="负样本模式 (画面里没有目标物体)")
    ap.add_argument("--negative-ratio", type=float, default=0.0,
                    help="负样本占比 —— 采 count 张里按这个比例混采负样本")
    ap.add_argument("--prefix", default="img", help="文件名前缀")
    ap.add_argument("--manual", action="store_true",
                    help="手动模式 —— 每张需按 Enter 触发采集，调整场景用")
    ap.add_argument("--check", action="store_true",
                    help="只做相机连通性检查, 不采图")
    args = ap.parse_args()

    print("=" * 64)
    print("YOLO 数据集采图")
    print("=" * 64)
    print()

    # ---- 服务占用检查 ----
    if vision_service_running():
        print("[FAIL] 视觉服务在跑 —— 相机是 USB 独占的, 采不了")
        print("       先停掉: ./scripts/stop_all.sh")
        return 1

    # ---- 相机 ----
    cfg = load_config()
    print("[相机] 打开 ...")
    cam = OrbbecCamera(cfg.camera)
    try:
        cam.open()
    except CameraError as exc:
        print(f"[FAIL] 打开相机失败: {exc}")
        return 1

    info = cam.device_info
    print(f"        {info.get('name')} (SN={info.get('serial_number')})")
    intr = cam.intrinsics
    print(f"        内参 fx={intr.fx:.2f} fy={intr.fy:.2f} "
          f"cx={intr.cx:.2f} cy={intr.cy:.2f}")

    try:
        if args.check:
            # 采一帧验证真的能出图
            frame = cam.read()
            if frame is None or frame.color is None:
                print("[FAIL] 没采到帧")
                return 1
            print(f"[ok] 采到帧 {frame.color.shape}")

            out_dir = Path(args.out)
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                import cv2

                p = out_dir / "check_frame.jpg"
                cv2.imwrite(str(p), frame.color)
                print(f"     样张: {p}")
            except Exception:
                pass
            return 0

        # ---- 采图 ----
        out_dir = Path(args.out)
        cap = DatasetCapture(
            out_dir=out_dir,
            split=args.split,
            interval=args.interval,
            prefix=args.prefix,
            manual=args.manual,
        )
        cap.prepare()

        if args.negative:
            print(f"\n[采图] 负样本模式 —— 采 {args.count} 张 (画面里不要放目标物体)")
            if args.manual:
                print("       每张需手动按 Enter 触发")
            cap.capture(args.count, cam, negative=True)
        elif args.negative_ratio > 0:
            n_neg = int(args.count * args.negative_ratio)
            n_pos = args.count - n_neg
            print(f"\n[采图] 混合模式 —— 正样本 {n_pos} 张 + 负样本 {n_neg} 张")
            print("       (负样本请在画面里**移走**目标物体)")
            if args.manual:
                print("       每张需手动按 Enter 触发")
            cap.capture(n_pos, cam, negative=False)
            cap.capture(n_neg, cam, negative=True)
        else:
            print(f"\n[采图] 采 {args.count} 张")
            if args.manual:
                print("       每张需手动按 Enter 触发，调整场景后再采集")
            cap.capture(args.count, cam, negative=False)

        meta_path = cap.save_meta()
        print(f"\n[元信息] {meta_path}")

        yaml_path = write_data_yaml(out_dir)
        print(f"[data.yaml] {yaml_path}")

        cap.report()

        print("\n" + "=" * 64)
        print("下一步")
        print("=" * 64)
        print("""
1. 标注 (框要**贴紧**物体 —— 抓取点靠框内深度采样算出来的):
     conda run -n yolo pip install labelImg
     conda run -n yolo labelImg
   图片在 images/train 和 images/val, 标签存到对应的 labels/ 目录

2. **先标 30~50 张** 跑一次训练看看效果, 比一次标 500 张高效得多:
     conda run -n yolo python tools/train_model.py

3. 效果达标后再补标剩余的图。
""".strip())

        return 0
    finally:
        cam.close()


if __name__ == "__main__":
    raise SystemExit(main())
