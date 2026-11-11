#!/usr/bin/env python3
"""生成 ChArUco 标定板 (可打印的 PNG)。

用法::

    conda run -n yolo python tools/make_board.py
    conda run -n yolo python tools/make_board.py --square 0.04 --marker 0.03 \
        --cols 7 --rows 5 --dict DICT_4X4_50 --out /tmp/board.png

**打印要求 (极其重要)**:
1. 打印时选 **100% 原始尺寸** (不要「适应页面」/「缩放以适合」)
2. 打完用尺子量**相邻角点的距离**, 把实际值填回 ``--square``
   (A4 纸 + 普通打印机缩放误差常常有 1~2%, 1.5 m 处就是 15~30 mm)
3. 板要**平整**, 贴硬板子上, 别贴在软的地方 (褶皱会让角点定位偏)

生成的文件同时会打印一份 JSON 规格, 直接存到
``configs/calibration.yaml`` 里就能被后面的脚本用。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.board import (  # noqa: E402
    BoardSpec,
    DICT_NAMES,
    generate_board_image,
    render_with_instructions,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 ChArUco 标定板")
    parser.add_argument(
        "--square", type=float, default=0.040,
        help="棋盘格边长 (米), 即相邻角点距离。默认 0.04 (4cm)",
    )
    parser.add_argument(
        "--marker", type=float, default=0.030,
        help="ArUco 标记边长 (米), 必须小于棋盘格。默认 0.03",
    )
    parser.add_argument("--cols", type=int, default=7, help="棋盘格列数 (角点列) 。默认 7")
    parser.add_argument("--rows", type=int, default=5, help="棋盘格行数 (角点行)。默认 5")
    parser.add_argument(
        "--dict", dest="dictionary", default="DICT_4X4_50",
        choices=list(DICT_NAMES), help="ArUco 字典",
    )
    parser.add_argument(
        "--dpi", type=int, default=4000,
        help="每米多少像素。默认 4000 (约 101 dpi)",
    )
    parser.add_argument(
        "--out", default="/tmp/charuco_board.png", help="输出 PNG 路径",
    )
    parser.add_argument(
        "--margin", type=float, default=0.02,
        help="板四周留白 (米), 免得角点贴着纸边被裁。默认 0.02",
    )
    args = parser.parse_args()

    if args.marker >= args.square:
        print("错误: 标记边长必须小于棋盘格边长", file=sys.stderr)
        return 1

    spec = BoardSpec(
        square_length=args.square,
        marker_length=args.marker,
        cols=args.cols,
        rows=args.rows,
        dictionary=args.dictionary,
    )

    img, info = render_with_instructions(spec, args.dpi, args.margin)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        import cv2

        cv2.imwrite(str(out), img)
    except Exception as exc:
        print(f"写文件失败: {exc}", file=sys.stderr)
        return 1

    print(f"标定板已生成: {out}")
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print()
    print("=" * 60)
    print("打印与量尺步骤 (不做对标定就是错的):")
    print(f"  1. 打印时选 100% 原始尺寸, 别缩放")
    print(f"  2. 打完量相邻角点距离 —— 目标 {args.square * 1000:.1f} mm")
    print(f"  3. 量出来是 {args.square * 1000:.1f} mm 就保持不变; "
          f"不是就改 --square 重新生成")
    print(f"  4. 把实际量到的值填进 configs/calibration.yaml 的 square_length")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
