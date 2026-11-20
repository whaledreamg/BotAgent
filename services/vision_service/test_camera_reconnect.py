#!/usr/bin/env python3
"""相机自动重连的单元测试。

用假相机 (不依赖真硬件) 验证:
- 相机没打开时, detect 会先尝试重连, 重连成功就继续
- 重连失败时抛 CameraError (不崩服务)
- 重连有间隔限制 (不会连着狂试)
- 采帧失败 (USB 掉线) 也会触发重连

运行: cd services/vision_service && python test_camera_reconnect.py
"""

import sys
import time

from camera_adapter import CameraError, Frame, Intrinsics
from service import VisionService

failures = []


def check(cond, msg, detail=""):
    print(("  ok   " if cond else "  FAIL ") + msg + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(msg)


class FakeCamera:
    """假相机: 可以模拟「没打开」「采帧失败」「重连成功」"""

    def __init__(self, open_ok=True, read_ok=True, reopen_ok=True):
        self._opened = False
        self.open_ok = open_ok
        self.read_ok = read_ok
        self.reopen_ok = reopen_ok
        self.open_calls = 0
        self.reopen_calls = 0
        self.read_calls = 0

    @property
    def opened(self):
        return self._opened

    @property
    def device_info(self):
        return {"name": "fake", "serial_number": "0", "pid": 0, "vid": 0}

    @property
    def intrinsics(self):
        return Intrinsics()

    def open(self):
        self.open_calls += 1
        if not self.open_ok:
            raise CameraError("打开相机失败: 假错误")
        self._opened = True

    def close(self):
        self._opened = False

    def reopen(self):
        self.reopen_calls += 1
        if not self.reopen_ok:
            raise CameraError("重开失败: 假错误")
        self._opened = True

    def read(self, timeout_ms=None):
        self.read_calls += 1
        if not self.read_ok:
            raise CameraError("采帧失败: 假错误")
        return Frame(color=object(), depth_raw=None, depth_scale=0.001,
                     intrinsics=Intrinsics(), depth_intrinsics=Intrinsics(),
                     depth_to_color=None)

    def map_bbox_to_depth(self, bbox, intr):
        return bbox


def make_service(cam, **cfg_kw):
    """造一个只用到相机和重连逻辑的 VisionService。"""
    svc = VisionService.__new__(VisionService)
    svc.camera = cam
    svc.cfg = type("C", (), {"auto_reconnect": True, "reconnect_interval": 0.0,
                             "detector": type("D", (), {"conf": 0.35, "find_conf": 0.15})()})()
    svc._errors = []
    svc._last_objects = []
    svc._last_ms = 0
    svc._timestamp_us = 0
    svc._started = True
    svc._reconnect_enabled = cfg_kw.get("auto_reconnect", True)
    svc._reconnect_interval = cfg_kw.get("reconnect_interval", 0.0)
    svc._last_reconnect = 0.0
    svc._reconnect_failures = 0
    import threading
    svc._camera_lock = threading.Lock()
    svc._lock = threading.Lock()
    svc._dynamic_provider = None
    svc._arm_client = None
    svc._world_context = None
    # 追溯器 (detect 里会用到)
    import tempfile
    from tracing import DetectionTracer
    svc._tracer = DetectionTracer(tempfile.mkdtemp())

    # 绕开真正的推理 (没有检测模型)
    svc.detector = type("Det", (), {
        "infer": staticmethod(lambda *a, **k: []),
        "loaded": True, "pose_loaded": False, "device": "cpu",
    })()
    svc.solver = type("Sol", (), {"solve": staticmethod(lambda *a, **k: None),
                                  "set_extrinsic_provider": staticmethod(lambda *a: None)})()
    svc._refresh_world_context = lambda: None
    svc._record_error = lambda m: None
    svc._pose_dict = lambda *a: {}
    return svc


def main():
    print("相机没打开 -> 重连成功 -> 继续检测:")
    cam = FakeCamera(open_ok=True)
    svc = make_service(cam)
    # 假装一开始没打开
    cam._opened = False
    try:
        svc.detect()
        check(True, "重连成功后检测正常完成")
    except Exception as exc:
        check(False, "重连成功后检测正常完成", repr(exc))
    check(cam.reopen_calls == 1, "调用了 reopen", str(cam.reopen_calls))
    check(cam.opened, "相机已打开")

    print("相机没打开 + 重连失败 -> 抛 CameraError:")
    cam = FakeCamera(open_ok=False, reopen_ok=False)
    svc = make_service(cam)
    try:
        svc.detect()
        check(False, "应该抛 CameraError")
    except CameraError:
        check(True, "抛 CameraError (不崩服务)")
    except Exception as exc:
        check(False, "应该抛 CameraError", repr(exc))

    print("重连有间隔限制:")
    cam = FakeCamera(open_ok=False, reopen_ok=False)
    svc = make_service(cam, reconnect_interval=60.0)
    for _ in range(5):
        try:
            svc.detect()
        except CameraError:
            pass
    check(cam.reopen_calls <= 1, "间隔内不重复重连", str(cam.reopen_calls))

    print("采帧失败 (USB 掉线) -> 重连并重试:")
    cam = FakeCamera(open_ok=True, read_ok=False)
    svc = make_service(cam)
    try:
        svc.detect()
        check(False, "重试后仍失败, 应该抛 CameraError")
    except CameraError:
        check(True, "抛 CameraError")
    check(cam.reopen_calls >= 1, "采帧失败触发了重连", str(cam.reopen_calls))

    print("auto_reconnect 关掉时不重连:")
    cam = FakeCamera(open_ok=False, reopen_ok=False)
    svc = make_service(cam, auto_reconnect=False)
    try:
        svc.detect()
    except CameraError:
        pass
    check(cam.reopen_calls == 0, "没有调用 reopen", str(cam.reopen_calls))


if __name__ == "__main__":
    main()
    print(f"\n{'全部通过' if not failures else str(len(failures)) + ' 项失败'}")
    sys.exit(1 if failures else 0)
