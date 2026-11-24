#!/usr/bin/env python3
"""只读诊断: 相机不可用时, 抓取流程会拿到什么?

不碰硬件 —— 用假相机/假检测器复现「相机断了但服务还在跑」的场景,
看视觉服务返回的是**真数据**还是**陈旧缓存**。

结论预期 (2026-11-24 现场):
  - ``fresh=True`` 且相机真断 -> detect() 抛 CameraError -> 返回 []
  - 但 ``_find_by_detect`` 把它**吞掉**了, 记进 errors 就返回空列表
  - 抓取 skill 收到 found=False -> 报「没有找到瓶子」-> 不会动

所以现场「拔了相机还能抓」的原因是另一个:
  **相机根本没断** —— Orbbec SDK 的流在 USB 拔出后仍能拿到
  **最后一帧** (或 SDK 自己缓存的帧), 推理照常出结果。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service import VisionService  # noqa: E402


class _FakeDet:
    def __init__(self, label: str, conf: float) -> None:
        self.label = label
        self.confidence = conf
        self.bbox = (0.0, 0.0, 10.0, 10.0)
        self.keypoints = None


class _DeadCamera:
    """相机已拔: read() 抛 CameraError。"""

    opened = False
    device_info: Dict[str, Any] = {}

    def read(self):
        from camera import CameraError
        raise CameraError("相机没连上")

    def map_bbox_to_depth(self, bbox, intr):
        return bbox


class _FakeTracer:
    def new_session(self) -> str:
        return "s"

    def save_annotated(self, image, objects, session) -> str:
        return ""

    def record(self, **kw):
        return None


class _FakeDetector:
    def __init__(self) -> None:
        self.calls = 0
        self.loaded = True
        self.pose_loaded = True
        self.device = "cpu"
        self.class_names = {"0": "bottle"}

    def infer(self, image, conf=0.0, pose=False) -> List[Any]:
        self.calls += 1
        return [_FakeDet("bottle", 0.5)]


def _svc(camera) -> VisionService:
    svc = VisionService.__new__(VisionService)
    svc.detector = _FakeDetector()
    svc.camera = camera
    svc.solver = None
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
    svc._camera_lock = __import__("threading").Lock()
    svc._last_reconnect = 0.0
    svc._reconnect_interval = 2.0
    return svc


def main() -> int:
    print("=" * 66)
    print("场景 A: 相机已拔 (read() 抛 CameraError), 请求 fresh=True")
    print("=" * 66)
    svc = _svc(_DeadCamera())
    hits = svc.find("bottle", frame="camera", fresh=True)
    print(f"  find() 返回 {len(hits)} 个结果")
    print(f"  视觉服务记录的 errors: {svc._errors}")
    print()
    if hits:
        print("  [!!] 返回了数据 —— 相机断了还能出坐标, 这是致命的")
    else:
        print("  [OK] 返回空 -> 抓取 skill 会报「没有找到瓶子」, 不会动")
    print("  detector.infer 调用次数 =", svc.detector.calls, "(0 = 确实没推理)")

    print()
    print("=" * 66)
    print("场景 B: 相机已拔, 但**缓存里还有上次的结果** (fresh=False)")
    print("=" * 66)
    svc2 = _svc(_DeadCamera())
    svc2._last_objects = [{
        "label": "bottle", "confidence": 0.37,
        "pose": {"x": 0.44, "y": -0.12, "z": -0.32,
                 "frame": "camera", "depth_valid": True},
    }]
    hits2 = svc2.find("bottle", frame="camera", fresh=False)
    print(f"  find() 返回 {len(hits2)} 个结果 (来自缓存)")
    if hits2:
        print(f"  pose = {hits2[0]['pose']}  <- 陈旧数据!")
        print("  [!!] 相机断了仍然能返回坐标 —— 抓取会照着陈旧坐标动")
    print("  detector.infer 调用次数 =", svc2.detector.calls)

    print()
    print("=" * 66)
    print("场景 C: 相机**已拔但 SDK 流没断** (现场真实情况)")
    print("=" * 66)
    print("  Orbbec SDK 在 USB 拔出后, stream 句柄可能仍然有效,")
    print("  read() 拿到的是**最后一帧** (或 SDK 内部缓存的帧)。")
    print("  于是:")
    print("    - detector.infer() 照常推理 -> 出结果")
    print("    - pose 照常算 -> 出坐标")
    print("    - 但画面是**拔线前那一帧** -> 坐标完全过期")
    print()
    print("  现场证据: 17:42 的检测图里瓶子清晰可见 (相机当时是接着的),")
    print("  而 17:31 那次 tcp_before.y 从 -0.43 翻成 +0.40 ——")
    print("  说明那几帧的状态推送本身就不稳定。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
