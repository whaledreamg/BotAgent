#!/usr/bin/env python3
"""验证「按名字找目标」用的是**新鲜**的检测结果, 不是旧缓存。

**背景**: 抓取的时候报出来的坐标必须是**此刻**物体所在的位置。
之前 ``find()`` 命中缓存就直接返回, 于是连续三次抓同一个瓶子拿到的
坐标和置信度**完全一样** —— 连 conf 都一样, 那显然不是两次真实推理
的结果。

这个脚本用假的 detector / camera 跑, 不需要真相机:

    conda run -n yolo python tools/check_fresh_find.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service import VisionService  # noqa: E402


class _FakeDetector:
    """每次推理返回一个「位置随调用次数变化」的目标。"""

    def __init__(self) -> None:
        self.calls = 0
        self.loaded = True
        self.pose_loaded = True
        self.device = "cpu"
        self.class_names = {"0": "bottle"}

    def infer(self, image, conf=0.0, pose=False) -> List[Any]:
        self.calls += 1
        # 位置每次往前挪 0.1 —— 模拟「物体被挪动了」
        return [_FakeDet("bottle", 0.5, 0.1 * self.calls)]


class _FakeDet:
    def __init__(self, label: str, conf: float, x: float) -> None:
        self.label = label
        self.confidence = conf
        # 把「位置」编码进 bbox 的 y2 —— solver 从 bbox 取值。
        # (bbox 是 (x1, y1, x2, y2), 所以位置放在 index 3)
        self.bbox = (0.0, 0.0, 10.0, 10.0 + x * 100.0)
        self.keypoints = None


class _FakeCapture:
    def __init__(self) -> None:
        import numpy as np

        self.color = np.zeros((10, 10, 3), dtype=np.uint8)
        self.depth_meters = np.full((10, 10), 1.0, dtype=np.float32)
        self.intrinsics = None
        self.depth_intrinsics = None
        self.depth_to_color = None


class _FakeTracer:
    """假的追溯器 —— 不落盘。"""

    def new_session(self) -> str:
        return "test_session"

    def save_annotated(self, image, objects, session) -> str:
        return ""

    def record(self, **kw):
        return None


class _FakeCamera:
    opened = True
    device_info: Dict[str, Any] = {}

    def read(self) -> _FakeCapture:
        return _FakeCapture()

    def map_bbox_to_depth(self, bbox, intr):
        return bbox


class _FakeSolver:
    """把 bbox 直接当成 3D 点 —— 够验证「有没有重新拍」了。"""

    extrinsic_frame = "camera"

    def __init__(self) -> None:
        self.extrinsic = None
        self._provider = None

    def solve(self, depth, bbox, intr, **kw) -> Any:
        class _T:
            def __init__(self, x: float) -> None:
                self.x = x

            def to_dict(self) -> Dict[str, Any]:
                return {"x": self.x, "y": 0.0, "z": 1.0, "depth_valid": True}

            def as_tuple(self):
                return (self.x, 0.0, 1.0)

        # x 从 bbox 的 y2 还原 (fake detector 把位置编码进去了)
        return _T((bbox[3] - 10.0) / 100.0)

    def extrinsic_matrix(self):
        return None

    def to_base_frame(self, xyz):
        return None


def _make_service() -> VisionService:
    svc = VisionService.__new__(VisionService)
    svc.detector = _FakeDetector()
    svc.camera = _FakeCamera()
    svc.solver = _FakeSolver()
    svc.cfg = type("C", (), {"detector": type("D", (), {"conf": 0.35, "find_conf": 0.15})()})()
    svc._lock = __import__("threading").Lock()
    svc._sub_lock = __import__("threading").Lock()
    svc._subscribers = []
    svc._last_objects = []
    svc._last_ms = 0
    svc._timestamp_us = 0
    svc._errors = []
    svc._dynamic_provider = None
    svc._world_context = None
    svc._reconnect_enabled = False
    svc._reconnect_failures = 0
    svc._tracer = _FakeTracer()
    return svc


def main() -> int:
    svc = _make_service()
    fails = 0

    print("========== 1. 连续两次 find (默认, 走缓存) ==========")
    svc.detector.calls = 0
    a = svc.find("bottle", frame="camera")
    b = svc.find("bottle", frame="camera")
    print(f"  第一次: calls={svc.detector.calls} x={a[0]['pose']['x'] if a else None}")
    print(f"  第二次: calls={svc.detector.calls} x={b[0]['pose']['x'] if b else None}")
    if svc.detector.calls == 1:
        print("  [PASS] 第二次命中缓存, 没有重新拍 (符合原行为)")
    else:
        print(f"  [FAIL] 缓存没生效, 拍了 {svc.detector.calls} 次")
        fails += 1

    print()
    print("========== 2. fresh=True 必须重新拍 ==========")
    svc.detector.calls = 0
    c = svc.find("bottle", frame="camera", fresh=True)
    print(f"  fresh 第一次: calls={svc.detector.calls} x={c[0]['pose']['x'] if c else None}")
    d = svc.find("bottle", frame="camera", fresh=True)
    print(f"  fresh 第二次: calls={svc.detector.calls} x={d[0]['pose']['x'] if d else None}")
    if svc.detector.calls == 2:
        print("  [PASS] 每次 fresh 都重新拍")
    else:
        print(f"  [FAIL] fresh 没生效, 只拍了 {svc.detector.calls} 次")
        fails += 1

    if c and d and c[0]["pose"]["x"] != d[0]["pose"]["x"]:
        print("  [PASS] 两次 fresh 的坐标不同 (位置确实更新了)")
    else:
        print("  [FAIL] 两次 fresh 的坐标一样 —— 还是旧数据")
        fails += 1

    print()
    print("========== 3. fresh=True 且缓存里本来就有 ==========")
    # 先让缓存里有一份「旧数据」
    svc.detector.calls = 0
    svc.find("bottle", frame="camera")
    cached_calls = svc.detector.calls
    svc.find("bottle", frame="camera", fresh=True)
    if svc.detector.calls > cached_calls:
        print("  [PASS] 缓存有货时 fresh 仍然重新拍")
    else:
        print("  [FAIL] 缓存有货时 fresh 没重新拍")
        fails += 1

    print()
    if fails:
        print(f"FAILED: {fails} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
