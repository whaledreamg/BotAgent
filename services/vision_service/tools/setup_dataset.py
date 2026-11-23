#!/usr/bin/env python3
"""搭数据集目录骨架 + 检查数据集质量。

`capture_dataset.py` 负责采图, 这个脚本负责:

1. **建目录** (images/{train,val}, labels/{train,val})
2. **生成 data.yaml** (类别名可指定)
3. **体检** —— 找出数据集里的常见问题:
   - 有图没标注 (漏标) —— 这是训练效果差的**头号原因**
   - 有标注没图 (标注残留)
   - 标注框格式错 / 越界 / 面积为零
   - 类别 id 超出 data.yaml 声明的范围
   - 类别分布严重不均衡 (某一类只有 2 张, 训不出来)
   - train/val 里出现了**同一张图** (数据泄漏, 验证指标虚高)

## 用法

    # 建目录 + 生成 data.yaml
    conda run -n yolo python tools/setup_dataset.py --out ../../datasets/myobjects --names 杯子 瓶子 螺丝

    # 只体检, 不改任何东西
    conda run -n yolo python tools/setup_dataset.py --out <目录> --check

    # 拆 train/val (从已有图片目录里按比例分)
    conda run -n yolo python tools/setup_dataset.py --out <目录> --split-existing 0.8
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp")


# ------------------------------------------------------------ 目录


def make_dirs(out_dir: Path) -> None:
    """建目录结构。

    **不建空的 label 文件** —— ultralytics 靠"有图无 label"表示负样本,
    提前建空 txt 反而会被当成"标注过的空图"。
    """
    for split in ("train", "val"):
        (out_dir / "images" / split).mkdir(parents=True, exist_ok=True)
        (out_dir / "labels" / split).mkdir(parents=True, exist_ok=True)
    print(f"[目录] {out_dir}")
    for split in ("train", ""):
        if not split:
            continue
        print(f"       images/{split}/  labels/{split}/")


def write_data_yaml(out_dir: Path, names: List[str]) -> Path:
    """写 data.yaml。"""
    out = out_dir / "data.yaml"
    names_block = "\n".join(f"  {i}: {n}" for i, n in enumerate(names))

    out.write_text(
        f"""# YOLO 数据集配置
#
# **names 里的类名会直接进 model.names**, find_object 是靠类名做匹配的:
#
#   - 训练时类名用中文 (比如 "螺丝"), 用户说"螺丝" 就能直接匹配上
#   - 训练时类名用英文 (比如 "screw"), 用户说"螺丝" 匹配不上 ——
#     得往 services/vision_service/class_names.py 的 ZH_TO_CLASS 补映射
#
# 详见 docs/vision-training.md。
path: {out_dir}
train: images/train
val: images/val

names:
{names_block}
""",
        encoding="utf-8",
    )
    print(f"[data.yaml] {out}")
    for i, n in enumerate(names):
        print(f"         {i}: {n}")
    return out


# ------------------------------------------------------------ 体检


class Report:
    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.infos: List[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def print(self) -> int:
        print()
        print("=" * 64)
        print("数据集体检")
        print("=" * 64)

        if self.infos:
            print("\n信息:")
            for m in self.infos:
                print(f"  [i] {m}")

        if self.warnings:
            print("\n警告:")
            for m in self.warnings:
                print(f"  [warn] {m}")

        if self.errors:
            print("\n错误:")
            for m in self.errors:
                print(f"  [FAIL] {m}")

        print()
        if not self.errors and not self.warnings:
            print("[ok] 数据集没问题, 可以开训")
            return 0
        if self.errors:
            print(f"[FAIL] {len(self.errors)} 个错误, {len(self.warnings)} 个警告 —— 先修错误")
            return 1
        print(f"[warn] {len(self.warnings)} 个警告 —— 不阻塞, 但会影响效果")
        return 2


def parse_label_file(path: Path) -> Tuple[List[Tuple[int, float, float, float, float]], List[str]]:
    """解析一个 YOLO 标注文件。

    返回 (有效标注列表, 错误信息列表)。
    """
    boxes: List[Tuple[int, float, float, float, float]] = []
    errors: List[str] = []

    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return [], [f"读不了: {exc}"]

    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) != 5:
            errors.append(f"{path.name}:{lineno} 字段数 {len(parts)} (应为 5)")
            continue

        try:
            cls = int(float(parts[0]))
            vals = [float(v) for v in parts[1:]]
        except ValueError:
            errors.append(f"{path.name}:{lineno} 有非数字: {line[:60]}")
            continue

        x, y, w, h = vals
        if w <= 0 or h <= 0:
            errors.append(f"{path.name}:{lineno} 宽高非正: w={w:.4f} h={h:.4f}")
            continue
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 <= w <= 1 and 0 <= h <= 1):
            errors.append(
                f"{path.name}:{lineno} 坐标越界 (应为 0~1): "
                f"x={x:.4f} y={y:.4f} w={w:.4f} h={h:.4f}"
            )
            continue

        # 框的中心 + 宽高 换算成边界, 检查是否完全在画面外
        if x - w / 2 > 1 or x + w / 2 < 0 or y - h / 2 > 1 or y + h / 2 < 0:
            errors.append(f"{path.name}:{lineno} 框完全在画面外")
            continue

        boxes.append((cls, x, y, w, h))

    return boxes, errors


def check_dataset(out_dir: Path) -> int:
    """体检。返回 0=好, 1=有错, 2=只有警告。"""
    report = Report()

    data_yaml = out_dir / "data.yaml"
    if not data_yaml.exists():
        report.error(f"没有 data.yaml: {data_yaml}")
        return report.print()

    # ---- 读 data.yaml ----
    names: List[str] = []
    try:
        import yaml

        raw = yaml.safe_load(data_yaml.read_text(encoding="utf-8")) or {}
        nm = raw.get("names")
        if isinstance(nm, dict):
            names = [str(nm[k]) for k in sorted(nm, key=lambda x: int(x))]
        elif isinstance(nm, list):
            names = [str(x) for x in nm]
        else:
            report.error("data.yaml 里没有 names")

        root_raw = raw.get("path")
        root = Path(root_raw) if root_raw else out_dir
        if not root.is_absolute():
            root = out_dir / root
    except Exception as exc:
        report.error(f"data.yaml 解析失败: {exc}")
        return report.print()

    if not names:
        report.error("data.yaml 里没有 names —— 必须写类别名")
        return report.print()

    report.info(f"类别 {len(names)} 个: {', '.join(names)}")
    report.info(f"根目录 {root}")

    # ---- 逐 split 检查 ----
    class_counts: Counter = Counter()
    per_class_images: Dict[int, Set[str]] = defaultdict(set)
    hash_locations: Dict[str, List[str]] = defaultdict(list)
    total_images = 0
    total_boxes = 0

    for split in ("train", "val"):
        img_dir = root / "images" / split
        lbl_dir = root / "labels" / split

        if not img_dir.exists():
            report.error(f"图片目录不存在: {img_dir}")
            continue

        images = sorted(
            p for p in img_dir.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        )
        if not images:
            report.error(f"{split} 集里没有图片: {img_dir}")
            continue

        labels = {p.stem for p in lbl_dir.glob("*.txt")} if lbl_dir.exists() else set()

        for img in images:
            total_images += 1
            stem = img.stem

            # 数据泄漏检查: 同一张图出现在 train 和 val
            digest = hashlib.md5(img.read_bytes()).hexdigest()
            hash_locations[digest].append(f"{split}/{img.name}")

            lbl = lbl_dir / f"{stem}.txt"
            if not lbl.exists():
                # 没有 txt = 负样本, 合法
                continue

            boxes, errs = parse_label_file(lbl)
            for e in errs:
                report.error(e)
            if not boxes and not errs:
                report.warn(f"{split}/{stem}.txt 是空文件 (算负样本的话可以删掉它)")
                continue

            total_boxes += len(boxes)
            for cls, *_ in boxes:
                if cls < 0 or cls >= len(names):
                    report.error(
                        f"{split}/{stem}.txt 里的类别 id {cls} 超出范围 "
                        f"(data.yaml 只声明了 0~{len(names) - 1})"
                    )
                    continue
                class_counts[cls] += 1
                per_class_images[cls].add(f"{split}/{stem}")

        # 有标注没图
        orphan = labels - {p.stem for p in images}
        if orphan:
            report.warn(
                f"{split} 集有 {len(orphan)} 个标注文件没有对应的图 "
                f"(例: {', '.join(sorted(orphan)[:3])})"
            )

        # 统计漏标
        unlabeled = [p.stem for p in images if p.stem not in labels]
        if unlabeled:
            ratio = len(unlabeled) / len(images)
            if ratio > 0.5:
                report.error(
                    f"{split} 集 {len(unlabeled)}/{len(images)} 张图没有标注 "
                    f"({ratio:.0%}) —— 漏标是训练效果差的头号原因"
                )
            elif ratio > 0.2:
                report.warn(
                    f"{split} 集 {len(unlabeled)}/{len(images)} 张图没有标注 "
                    f"({ratio:.0%}) —— 确认这些是不是故意的负样本"
                )

    # ---- 数据泄漏 ----
    leaked = {h: locs for h, locs in hash_locations.items() if len(locs) > 1}
    if leaked:
        examples = list(leaked.values())[:3]
        report.error(
            f"发现 {len(leaked)} 张重复图片出现在不同 split 里 (数据泄漏): "
            f"例 {examples}"
        )

    # ---- 类别分布 ----
    if total_boxes:
        report.info(f"总共 {total_images} 张图, {total_boxes} 个标注框")
        print()
        print("  类别分布:")
        for i, name in enumerate(names):
            n = class_counts.get(i, 0)
            n_imgs = len(per_class_images.get(i, ()))
            bar = "#" * min(40, int(n / max(1, total_boxes) * 40))
            print(f"    {i} {name:<16} {n:5d} 框 / {n_imgs:4d} 图  {bar}")

            if n == 0:
                report.error(f"类别「{name}」一个标注都没有 —— 删掉它或者去标")
            elif n_imgs < 10:
                report.warn(
                    f"类别「{name}」只有 {n_imgs} 张图 —— 太少, 训不出稳定的效果"
                )
            elif n_imgs < 30:
                report.warn(
                    f"类别「{name}」只有 {n_imgs} 张图 —— 建议补到 30+ 再正式训"
                )

        # 少数类/多数类比例
        counts = [class_counts.get(i, 0) for i in range(len(names)) if class_counts.get(i, 0) > 0]
        if len(counts) >= 2:
            ratio = max(counts) / max(1, min(counts))
            if ratio > 10:
                report.warn(
                    f"类别分布很不均衡 (最多/最少 = {ratio:.0f} 倍) —— "
                    f"少数类要么补数据, 要么用 single_cls 训练"
                )
    else:
        report.error("整个数据集一个标注都没有")

    # ---- train/val 比例 ----
    tr = len(list((root / "images" / "train").glob("*"))) if (root / "images" / "train").exists() else 0
    va = len(list((root / "images" / "val").glob("*"))) if (root / "images" / "val").exists() else 0
    if tr and va:
        ratio = tr / max(1, tr + va)
        report.info(f"train {tr} 张 / val {va} 张 (train 占比 {ratio:.0%})")
        if ratio < 0.5:
            report.warn("train 占比偏低 —— 验证集太大, 训练数据不够")
        elif ratio > 0.95:
            report.warn("几乎没有验证集 —— 没法判断模型有没有过拟合")

    return report.print()


# ---- 拆 train/val


def split_existing(out_dir: Path, ratio: float) -> int:
    """把 images/ 下的散图按比例拆成 train/val。

    用于"已经采了一堆图但没分 split"的补救。
    """
    src = out_dir / "images"
    if not src.exists():
        print(f"[FAIL] 没有 {src}")
        return 1

    # 收集所有图片 (不管在哪个子目录)
    all_imgs: List[Path] = []
    for p in src.rglob("*"):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            all_imgs.append(p)

    if not all_imgs:
        print(f"[FAIL] {src} 下没有图片")
        return 1

    # 按文件名排序后轮流分 (确定性, 可复现)
    all_imgs.sort(key=lambda p: p.name)
    n_train = int(len(all_imgs) * ratio)

    moved = 0
    for i, img in enumerate(all_imgs):
        target_split = "train" if i < n_train else "val"
        target = src / target_split / img.name
        lbl_src = out_dir / "labels" / img.parent.name / f"{img.stem}.txt"
        lbl_dst = out_dir / "labels" / target_split / f"{img.stem}.txt"

        if img.parent.name == target_split:
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        lbl_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(img), str(target))
        if lbl_src.exists():
            shutil.move(str(lbl_src), str(lbl_dst))
        moved += 1

    print(f"[拆分] 移动了 {moved} 张 (train {n_train} / val {len(all_imgs) - n_train})")
    return 0


# ------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description="数据集目录搭建与体检")
    ap.add_argument("--out", required=True, help="数据集根目录")
    ap.add_argument("--names", default="",
                    help="类别名, 逗号分隔 (中文也行)。例: 杯子,瓶子,螺丝")
    ap.add_argument("--check", action="store_true", help="只体检, 不建目录")
    ap.add_argument("--split-existing", type=float, default=0.0,
                    help="把 images/ 下已有的图按这个比例拆成 train/val")
    args = ap.parse_args()

    out_dir = Path(args.out)

    print("=" * 64)
    print("数据集搭建")
    print("=" * 64)
    print()

    if args.split_existing > 0:
        rc = split_existing(out_dir, args.split_existing)
        if rc != 0:
            return rc
        print()

    if not args.check:
        make_dirs(out_dir)
        names = [n.strip() for n in args.names.split(",") if n.strip()]
        if names:
            write_data_yaml(out_dir, names)
        else:
            # 没给类名也写一份占位的 —— 否则 data.yaml 不存在,
            # 后面训练预检会直接报"找不到 data.yaml", 多绕一圈
            write_data_yaml(out_dir, ["类名0", "类名1", "类名2"])
            print()
            print("[提示] 用的是占位类名 —— 标完记得改成你实际的类名:")
            print("       conda run -n yolo python tools/setup_dataset.py \\")
            print(f"         --out {out_dir} --names 你的类名1,你的类名2")
        print()

    return check_dataset(out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
