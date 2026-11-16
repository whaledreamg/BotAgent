"""追溯系统集成测试。

演示:
  1. 创建追溯记录
  2. 查询记录
  3. 获取图像
"""

import json
import tempfile
from pathlib import Path

import numpy as np
import cv2

from tracing import DetectionTracer, DetectionRecord


def test_tracer_basic():
    """基础功能测试。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tracer = DetectionTracer(tmpdir)

        # 创建一张测试图像 (100x100 黑色)
        test_image = np.zeros((100, 100, 3), dtype=np.uint8)

        # 记录一个识别结果
        record1 = tracer.record(
            label="cup",
            confidence=0.95,
            bbox=(10.0, 15.0, 60.0, 80.0),
            image=test_image,
            pose={"x": 0.1, "y": 0.2, "z": 0.5},
            frame="camera",
            extra={"test": True},
        )

        assert record1 is not None
        assert record1.label == "cup"
        assert record1.confidence == 0.95
        print(f"✓ 记录 1: {record1.record_id}")

        # 记录第二个识别结果
        record2 = tracer.record(
            label="cup",
            confidence=0.87,
            bbox=(20.0, 25.0, 70.0, 85.0),
            image=test_image,
        )
        assert record2 is not None
        print(f"✓ 记录 2: {record2.record_id}")

        # 记录一个不同类别的物体
        record3 = tracer.record(
            label="person",
            confidence=0.92,
            bbox=(5.0, 10.0, 95.0, 100.0),
            image=test_image,
        )
        assert record3 is not None
        print(f"✓ 记录 3: {record3.record_id}")

        # 测试统计
        stats = tracer.get_stats()
        print(f"\n统计信息: {json.dumps(stats, indent=2, ensure_ascii=False)}")
        assert stats["total_records"] == 3

        # 测试按类别查询
        cup_records = tracer.query_by_label("cup", limit=10)
        print(f"\n查询 'cup': 找到 {len(cup_records)} 条记录")
        assert len(cup_records) == 2
        for record in cup_records:
            assert record["label"] == "cup"
            print(f"  - {record['record_id']}: {record['confidence']:.2%}")

        person_records = tracer.query_by_label("person", limit=10)
        print(f"\n查询 'person': 找到 {len(person_records)} 条记录")
        assert len(person_records) == 1

        # 验证图像被保存
        images_dir = Path(tmpdir) / "images"
        image_files = list(images_dir.glob("*.jpg"))
        print(f"\n保存的图像: {len(image_files)} 张")
        assert len(image_files) == 3
        for img_file in image_files[:1]:
            print(f"  - {img_file.name} ({img_file.stat().st_size} bytes)")

        # 验证记录文件
        records_file = Path(tmpdir) / "records.jsonl"
        assert records_file.exists()
        with open(records_file, "r") as f:
            lines = f.readlines()
        print(f"\n记录文件行数: {len(lines)}")
        assert len(lines) == 3

        # 验证索引文件
        index_file = Path(tmpdir) / "index.json"
        assert index_file.exists()
        with open(index_file, "r") as f:
            index = json.load(f)
        print(f"\n索引统计: {json.dumps(index, indent=2, ensure_ascii=False)}")
        assert "labels" in index
        assert "cup" in index["labels"]
        assert index["labels"]["cup"]["count"] == 2

        print("\n✓ 所有测试通过!")


def test_tracer_time_range():
    """时间范围查询测试。"""
    from datetime import datetime, timedelta

    with tempfile.TemporaryDirectory() as tmpdir:
        tracer = DetectionTracer(tmpdir)
        test_image = np.zeros((100, 100, 3), dtype=np.uint8)

        # 记录几条识别
        tracer.record("cup", 0.9, (10, 20, 30, 40), test_image)
        tracer.record("person", 0.85, (5, 10, 50, 100), test_image)

        # 获取时间范围
        now = datetime.now()
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=1)

        # 查询时间范围内的记录
        records = tracer.query_by_time_range(
            start.isoformat(), end.isoformat()
        )
        print(f"\n时间范围 ({start.isoformat()} ~ {end.isoformat()})")
        print(f"找到 {len(records)} 条记录")
        assert len(records) >= 2


if __name__ == "__main__":
    import sys

    # 确保在正确的目录
    sys.path.insert(0, "/home/gxh/workspace/project/Bot Agent/services/vision_service")

    print("=" * 60)
    print("追溯系统测试")
    print("=" * 60)

    try:
        test_tracer_basic()
        print("\n" + "=" * 60)
        test_tracer_time_range()
        print("\n" + "=" * 60)
        print("\n🎉 全部测试通过!")
    except AssertionError as e:
        print(f"\n❌ 断言失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
