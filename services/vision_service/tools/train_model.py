#!/usr/bin/env python3
"""训练自定义 YOLO 检测模型。

读 ``configs/vision.yaml`` 的 ``vision.training`` 段, 调 ultralytics
训练, 训完把 best.pt 自动写回 ``detector.weights`` —— 之后视觉服务
直接就能用上, **代码零改动**。

完整流程见 ``docs/vision-training.md``。

## 用法

    # 按配置训练 (推荐)
    conda run -n yolo python tools/train_model.py

    # 只做预检, 不训练 (检查数据集/GPU/配置对不对)
    conda run -n yolo python tools/train_model.py --check

    # 临时覆盖参数
    conda run -n yolo python tools/train_model.py --epochs 50 --batch 8

    # 训练完不自动改配置
    conda run -n yolo python tools/train_model.py --no-apply
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config, load_config  # noqa: E402

#: 项目根目录 (Bot Agent/)
ROOT = Path(__file__).resolve().parents[3]
VISION_CONFIG_PATH = ROOT / "configs" / "vision.yaml"


class TrainError(RuntimeError):
    """训练流程出错。"""


# ------------------------------------------------------------ 预检


def check_cuda(device: str) -> Tuple[bool, str]:
    """检查训练设备可用性。

    **CPU 训练慢到不可用** —— 几百张图能跑一整夜。所以这里给个硬提示。
    """
    if str(device).strip().lower() == "cpu":
        return True, "用 CPU 训练 —— 会非常慢 (几小时到几天), 建议先修 GPU"

    try:
        import torch
    except Exception as exc:  # pragma: no cover
        return False, f"torch 导入失败: {exc}"

    if not torch.cuda.is_available():
        return False, (
            "CUDA 不可用 —— 训练会退回 CPU, 慢到不可用。\n"
            "       先修 GPU 设备节点: sudo ./scripts/fix_gpu.sh"
        )

    name = torch.cuda.get_device_name(0)
    total_gb = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
    return True, f"GPU: {name} ({total_gb:.1f} GB)"


def check_dataset(data_path: str) -> Tuple[bool, str, Dict[str, Any]]:
    """检查数据集。

    返回 (是否可用, 说明, data.yaml 内容)。
    """
    if not data_path:
        return False, "没配置 training.data (data.yaml 路径)", {}

    path = Path(data_path)
    if not path.exists():
        return False, f"找不到 data.yaml: {path}", {}

    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return False, f"data.yaml 解析失败: {exc}", {}

    names = raw.get("names")
    if not names:
        return False, "data.yaml 里没有 names —— 必须写类别名", raw

    # names 可能是 list 也可能是 dict
    if isinstance(names, dict):
        name_list = [names[k] for k in sorted(names, key=lambda x: int(x))]
    else:
        name_list = list(names)

    # 根目录: data.yaml 里的 path, 相对于 data.yaml 所在目录
    root = raw.get("path")
    if root:
        root_path = Path(root)
        if not root_path.is_absolute():
            root_path = path.parent / root_path
    else:
        root_path = path.parent

    train_dir = root_path / str(raw.get("train", "images/train"))
    val_dir = root_path / str(raw.get("val", "images/val"))

    problems: List[str] = []
    if not train_dir.exists():
        problems.append(f"训练集目录不存在: {train_dir}")
    if not val_dir.exists():
        problems.append(f"验证集目录不存在: {val_dir}")

    n_train = len(list(train_dir.glob("*"))) if train_dir.exists() else 0
    n_val = len(list(val_dir.glob("*"))) if val_dir.exists() else 0

    # 统计标注情况
    label_dir = root_path / "labels"
    n_labeled = 0
    n_images = 0
    if train_dir.exists():
        for img in list(train_dir.glob("*"))[:2000]:
            if img.suffix.lower() not in (".jpg", ".jpeg", ".png", ".bmp"):
                continue
            n_images += 1
            txt = label_dir / "train" / (img.stem + ".txt")
            if txt.exists() and txt.stat().st_size > 0:
                n_labeled += 1

    if n_images == 0:
        problems.append(f"训练集里没有图片: {train_dir}")
    elif n_labeled == 0:
        problems.append(
            f"训练集 {n_images} 张图但**一个标注都没有** —— 先用 LabelImg 标注"
        )

    if problems:
        return False, "\n       ".join(problems), raw

    detail = (
        f"类别 {len(name_list)} 个: {', '.join(str(n) for n in name_list)}\n"
        f"       训练图 {n_train} 张 (其中已标注 {n_labeled} 张), 验证图 {n_val} 张\n"
        f"       根目录 {root_path}"
    )
    return True, detail, raw


def check_weights(path: str) -> Tuple[bool, str]:
    """检查迁移学习的起点权重。"""
    if not path:
        return False, "没配置 training.base_weights"

    p = Path(path)
    if not p.exists():
        # .yaml 是模型结构文件 (从零训), 允许不存在?
        if p.suffix == ".yaml":
            return True, f"从零训练 (模型结构: {p})"
        return False, f"找不到权重文件: {p}"

    if p.suffix == ".yaml":
        return True, f"从零训练 (模型结构: {p})"

    size_mb = p.stat().st_size / (1024 * 1024)
    return True, f"迁移学习起点: {p} ({size_mb:.1f} MB)"


# ------------------------------------------------------------ 训练


def run_training(cfg: Config, args: argparse.Namespace) -> Path:
    """跑训练, 返回 best.pt 的路径。"""
    from ultralytics import YOLO

    tr = cfg.training

    weights = args.weights or tr.base_weights
    data = args.data or tr.data
    epochs = args.epochs or tr.epochs
    batch = args.batch or tr.batch
    imgsz = args.imgsz or tr.imgsz
    device = args.device or tr.device
    project = args.project or tr.project
    name = args.name or tr.name

    print("[训练] 加载起点权重 ...")
    model = YOLO(weights)

    kwargs: Dict[str, Any] = {
        "data": data,
        "epochs": epochs,
        "imgsz": imgsz,
        "batch": batch,
        "patience": tr.patience,
        "device": device,
        "project": project,
        "name": name,
        "seed": tr.seed,
        "hsv_h": tr.hsv_h,
        "hsv_s": tr.hsv_s,
        "hsv_v": tr.hsv_v,
        "mosaic": tr.mosaic,
        "close_mosaic": tr.close_mosaic,
        "exist_ok": True,
        "verbose": True,
    }

    print()
    print("=" * 64)
    print("训练参数")
    print("=" * 64)
    for k, v in kwargs.items():
        if k == "data":
            continue
        print(f"  {k:14s} {v}")
    print(f"  {'data':14s} {data}")
    print()

    print("[训练] 开始 ... (Ctrl+C 可以中断, 中间权重在 runs/ 里)")
    print()

    model.train(**kwargs)

    # ultralytics 会在 project/name 下放权重
    run_dir = Path(project) / name
    best = run_dir / "weights" / "best.pt"
    last = run_dir / "weights" / "last.pt"

    if not best.exists():
        # ultralytics 有时会自己加后缀 (name2)
        candidates = sorted(run_dir.glob("*/weights/best.pt"))
        if not candidates and last.exists():
            candidates = [last]
        if not candidates:
            raise TrainError(
                f"训练结束但没找到权重 —— 看看 {run_dir} 下有什么"
            )
        best = candidates[0]

    return best


# ------------------------------------------------------------ 结果


def read_metrics(run_dir: Path) -> Dict[str, float]:
    """从 results.csv 读最终指标。"""
    csv_path = run_dir / "results.csv"
    if not csv_path.exists():
        return {}

    try:
        with open(csv_path, "r", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except Exception:
        return {}

    if not rows:
        return {}

    last = rows[-1]
    out: Dict[str, float] = {}

    def pick(*keys: str) -> Optional[float]:
        for k in keys:
            if k in last:
                try:
                    return float(last[k])
                except (TypeError, ValueError):
                    continue
        return None

    out["map50"] = pick("metrics/mAP50(B)", "metrics/mAP50")
    out["map50_95"] = pick("metrics/mAP50-95(B)", "metrics/mAP50-95")
    out["precision"] = pick("metrics/precision(B)", "metrics/precision")
    out["recall"] = pick("metrics/recall(B)", "metrics/recall")
    return {k: v for k, v in out.items() if v is not None}


def apply_weights(cfg: Config, weights: Path) -> bool:
    """把权重写回 configs/vision.yaml 的 detector.weights。

    **只改这一行** —— 用正则替换, 不动 yaml 里的其他内容 (注释、
    缩进都得保住, 不然 diff 会很难看)。
    """
    if not VISION_CONFIG_PATH.exists():
        print(f"[warn] 找不到 {VISION_CONFIG_PATH}, 跳过写回")
        return False

    text = VISION_CONFIG_PATH.read_text(encoding="utf-8")

    # 匹配 detector: 段下的 weights: 行
    pattern = re.compile(
        r"(^(\s*)weights:\s*)\"[^\"]*\"(\s*(?:#.*)?$)",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        print("[warn] 在 configs/vision.yaml 里找不到 detector.weights, 跳过写回")
        return False

    new_text = pattern.sub(
        lambda m: f'{m.group(1)}"{weights}"{m.group(3)}',
        text,
        count=1,
    )
    VISION_CONFIG_PATH.write_text(new_text, encoding="utf-8")
    print(f"[已写回] detector.weights = {weights}")
    return True


# ------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description="训练自定义 YOLO 模型")
    ap.add_argument("--check", action="store_true", help="只做预检, 不训练")
    ap.add_argument("--data", default="", help="覆盖 data.yaml 路径")
    ap.add_argument("--weights", default="", help="覆盖起点权重")
    ap.add_argument("--epochs", type=int, default=0, help="覆盖训练轮数")
    ap.add_argument("--batch", type=int, default=0, help="覆盖批大小")
    ap.add_argument("--imgsz", type=int, default=0, help="覆盖输入尺寸")
    ap.add_argument("--device", default="", help="覆盖训练设备")
    ap.add_argument("--project", default="", help="覆盖输出根目录")
    ap.add_argument("--name", default="", help="覆盖实验名")
    ap.add_argument("--no-apply", action="store_true",
                    help="训完不自动写回 detector.weights")
    args = ap.parse_args()

    cfg = load_config()
    tr = cfg.training

    print("=" * 64)
    print("YOLO 自定义模型训练")
    print("=" * 64)
    print()

    # ---- 预检 ----
    failures: List[str] = []

    print("[预检 1/3] 训练设备")
    ok, msg = check_cuda(args.device or tr.device)
    print(f"  {msg}")
    if not ok:
        failures.append("设备不可用")
    print()

    print("[预检 2/3] 起点权重")
    ok, msg = check_weights(args.weights or tr.base_weights)
    print(f"  {msg}")
    if not ok:
        failures.append("起点权重有问题")
    print()

    print("[预检 3/3] 数据集")
    data_path = args.data or tr.data
    ok, msg, raw = check_dataset(data_path)
    print(f"  {msg}")
    if not ok:
        failures.append("数据集有问题")
    print()

    if failures:
        print("=" * 64)
        print("[FAIL] 预检没通过:")
        for f in failures:
            print(f"  - {f}")
        print("=" * 64)
        print()
        print("修复建议:")
        print("  - 数据集: conda run -n yolo python tools/capture_dataset.py")
        print("            然后用 LabelImg 标注 (框要贴紧物体)")
        print("  - GPU:   sudo ./scripts/fix_gpu.sh")
        print("  - 配置:  改 configs/vision.yaml 的 vision.training 段")
        return 1

    if args.check:
        print("[ok] 预检全部通过, 可以开始训练")
        print("     (去掉 --check 就真的开始训)")
        return 0

    # ---- 训练 ----
    try:
        best = run_training(cfg, args)
    except TrainError as exc:
        print(f"[FAIL] {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n[中断] 训练被 Ctrl+C 打断 —— 中间权重在 runs/ 里, 可以 --resume")
        return 130

    # ---- 结果 ----
    run_dir = best.parent.parent
    print()
    print("=" * 64)
    print("训练完成")
    print("=" * 64)
    print(f"  权重: {best}")
    print(f"  输出: {run_dir}")

    metrics = read_metrics(run_dir)
    if metrics:
        print()
        print("  指标:")
        for k, v in metrics.items():
            print(f"    {k:12s} {v:.4f}")

        map50 = metrics.get("map50")
        if map50 is not None and map50 < tr.min_map50:
            print()
            print(f"  [warn] mAP50 = {map50:.3f} < 阈值 {tr.min_map50} —— "
                  f"模型还没学好, 别急着接入")
            print("         常见原因: 标注太少 / 框没贴紧 / 场景覆盖不够")

    print()
    print("  评估图表:")
    for fname, desc in (
        ("results.png", "训练曲线 (看有没有过拟合)"),
        ("confusion_matrix.png", "类别混淆情况"),
        ("results.csv", "逐 epoch 指标"),
    ):
        p = run_dir / fname
        if p.exists():
            print(f"    {p}  ({desc})")

    # ---- 写回配置 ----
    if args.no_apply or not tr.auto_apply:
        print()
        print("[跳过] 自动写回 detector.weights (--no-apply 或 auto_apply=false)")
        print(f"        手动改 configs/vision.yaml:")
        print(f"          detector.weights: \"{best}\"")
    else:
        print()
        apply_weights(cfg, best)

    print()
    print("下一步:")
    print(f"  1. 重启视觉服务让它加载新权重:")
    print(f"     ./scripts/start_vision.sh")
    print(f"  2. 验证 (记得先停服务, 相机是独占的):")
    print(f"     conda run -n yolo python tools/check_recognition.py --live --classes <类名>")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
