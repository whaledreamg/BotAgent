#!/usr/bin/env python3
"""把旧版「一个文件装多个动作」的数据拆成「一个文件一个动作」。

旧版格式: 一个 JSON 里塞了整场编排 (action.json 24 段 / 138 秒),
动作靠片段的 label 区分, 时间上散布在整场里。

新版格式: 一个 JSON = 一个动作, **文件名就是动作名** (挥手.json -> "挥手"),
文件里的 segments 是这个动作的轨迹片段。

拆分规则:
1. 按片段的 label 分组; 「X 副本」与「X」视为同一个动作 (副本是重复录制) ;
2. 同组片段按时间排序, 保留为该动作的连续轨迹;
3. 时间轴平移到从 0 开始 (去掉编排时的绝对时间)。

用法::

    python tools/split_actions.py            # 只预览, 不写文件
    python tools/split_actions.py --apply    # 真正写入 data/
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("split")


def base_label(label: str) -> str:
    """去掉「副本」后缀 —— 副本是同一动作的重复录制。"""
    return label.replace(" 副本", "").strip()


def split_one_file(path: Path) -> Dict[str, List[Dict]]:
    """把一个旧文件拆成 {动作名: 片段列表}。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_segments = data.get("segments") or []
    if not raw_segments:
        return {}

    # 按时间排序 (原文件不保证有序: sport.json 29 段里有 3 段乱序)
    ordered = sorted(raw_segments, key=lambda s: float(s.get("startTime", 0.0)))

    groups: Dict[str, List[Dict]] = defaultdict(list)
    for seg in ordered:
        label = str(seg.get("label", "") or "") or path.stem
        groups[base_label(label)].append(seg)

    return dict(groups)


def make_action(segs: List[Dict], label: str) -> Dict:
    """把一组片段打包成一个动作文件的内容。

    片段之间原有的时间间隙是**编排时的空等** (录制时动作之间隔了几秒才做下一个),
    直接保留会让机器人「走到位后干等几秒」。动作文件应该是一段连续轨迹,
    所以这里把间隙压掉: 后一段紧接前一段结束。
    """
    out_segs = []
    cursor = 0.0
    for seg in segs:
        item = dict(seg)
        duration = float(seg.get("duration", 0.0))
        item["startTime"] = round(cursor, 6)
        item["duration"] = duration
        item["label"] = label
        out_segs.append(item)
        cursor += duration

    return {
        "segments": out_segs,
        "totalDuration": round(cursor, 6),
        "version": 4.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="拆分旧版动作文件")
    ap.add_argument("--apply", action="store_true", help="真正写入文件")
    ap.add_argument("--src", default=str(SERVICE_DIR / "data"), help="源目录")
    ap.add_argument("--dst", default=str(SERVICE_DIR / "data"), help="目标目录")
    args = ap.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)

    # 同一动作可能出现在多个源文件里 (如「准备点」在 action.json 和 test.json 都有),
    # 按动作名合并。
    merged: Dict[str, List[Dict]] = defaultdict(list)
    for path in sorted(src.glob("*.json")):
        for label, segs in split_one_file(path).items():
            merged[label].extend(segs)

    plan: List[tuple[Path, Dict]] = []
    for label, segs in sorted(merged.items()):
        segs.sort(key=lambda s: float(s.get("startTime", 0.0)))
        plan.append((dst / f"{label}.json", make_action(segs, label)))

    logger.info("将生成 %d 个动作文件:", len(plan))
    for path, act in plan:
        logger.info(
            "  %-22s %2d 段  %6.2fs",
            path.name,
            len(act["segments"]),
            act["totalDuration"],
        )

    if not args.apply:
        logger.info("\n(预览模式, 加 --apply 才会写入)")
        return 0

    dst.mkdir(parents=True, exist_ok=True)
    for path, act in plan:
        path.write_text(
            json.dumps(act, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    logger.info("\n已写入 %d 个文件到 %s", len(plan), dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
