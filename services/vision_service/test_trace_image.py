#!/usr/bin/env python3
"""追溯图片: 每次拍照一个文件夹, 每个物体一张图一张标注。

验证:
- 一次拍照 -> 一个文件夹
- 这个文件夹里有 N 个物体的 N 张图
- 每张图只画自己的那个框 (别的物体的框不在这张图上)
- 文件名带类别和序号
- 记录里的 image_filename 能换算成实际路径

运行: cd services/vision_service && python test_trace_image.py
"""

import json
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

from tracing import DetectionTracer

failures = []


def check(cond, msg, detail=""):
    print(("  ok   " if cond else "  FAIL ") + msg + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(msg)


def make_image(w=640, h=480):
    return np.zeros((h, w, 3), dtype=np.uint8)


def colored_pixels(img):
    return int(np.count_nonzero(img.any(axis=2)))


def main():
    tmp = Path(tempfile.mkdtemp())
    tracer = DetectionTracer(tmp / "traces")

    image = make_image()
    dets = [
        (50, 60, 200, 220, "mouse", 0.87),
        (300, 100, 450, 300, "cup", 0.65),
        (480, 200, 600, 400, "bottle", 0.42),
    ]

    # ---- 一次拍照 = 一个会话文件夹 ----
    session = tracer.new_session()
    check((tracer.sessions_dir / session).is_dir(), "会话文件夹建好了", session)

    # ---- 每个物体一张图 ----
    records = []
    for i, (x1, y1, x2, y2, label, conf) in enumerate(dets, start=1):
        rec = tracer.record(
            label=label, confidence=conf, bbox=(x1, y1, x2, y2),
            image=image, frame="camera", session=session, index=i,
        )
        records.append(rec)

    check(all(r is not None for r in records), "三条记录都写成功")

    session_dir = tracer.sessions_dir / session
    jpgs = sorted(session_dir.glob("*.jpg"))
    check(len(jpgs) == 3, "文件夹里有 3 张图", str(len(jpgs)))
    names = sorted(p.name for p in jpgs)
    check(names == ["bottle_03.jpg", "cup_02.jpg", "mouse_01.jpg"],
          "文件名带类别和序号", str(names))

    # ---- 每张图只画自己的框 ----
    for (x1, y1, x2, y2, label, conf), path in zip(dets, [jpgs[2], jpgs[1], jpgs[0]]):
        saved = cv2.imread(str(path))
        check(saved is not None, f"{path.name} 可读")
        # 自己的区域有内容
        own = colored_pixels(saved[y1:y2, x1:x2])
        check(own > 200, f"{path.name} 自己的框在", f"{own} 像素")
        # 别的物体的区域是空的 (说明没画别人的框)
        others = []
        for (ox1, oy1, ox2, oy2, _, _) in dets:
            if (ox1, oy1, ox2, oy2) == (x1, y1, x2, y2):
                continue
            others.append(colored_pixels(saved[oy1:oy2, ox1:ox2]))
        check(all(n == 0 for n in others),
              f"{path.name} 没有画别人的框", str(others))

    # ---- 原图没被改动 ----
    check(colored_pixels(image) == 0, "原图没被改动")

    # ---- 记录里的路径能换算成实际文件 ----
    for rec, path in zip(records, [jpgs[2], jpgs[1], jpgs[0]]):
        full = tracer.get_image_path(rec.image_filename)
        check(full is not None and full.exists(),
              f"{rec.label} 的图片能找到", rec.image_filename)

    # ---- JSONL 里的记录 ----
    lines = [json.loads(l) for l in
             tracer.records_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    check(len(lines) == 3, "JSONL 有 3 条", str(len(lines)))
    check(all("/" in l["image_filename"] for l in lines),
          "文件名带会话目录", lines[0]["image_filename"])

    # ---- 两次拍照 -> 两个文件夹 ----
    session2 = tracer.new_session()
    tracer.record(label="pen", confidence=0.5, bbox=(10, 10, 50, 50),
                  image=image, session=session2, index=1)
    dirs = sorted(p.name for p in tracer.sessions_dir.iterdir() if p.is_dir())
    check(len(dirs) == 2, "两次拍照两个文件夹", str(dirs))


if __name__ == "__main__":
    main()
    print(f"\n{'全部通过' if not failures else str(len(failures)) + ' 项失败'}")
    sys.exit(1 if failures else 0)
