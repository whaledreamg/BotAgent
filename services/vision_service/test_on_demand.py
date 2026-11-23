"""**按需识别** (不轮询) 的集成测试。

要验证的东西:
  1. Vision 的 WebSocket **连上后不产生任何检测** —— 之前它是
     「连上就每秒拍一次」, 那才是真正的轮询。
  2. 客户端发 ``{"action": "detect"}`` 才拍一次, 结果广播回来。
  3. 广播里带 ``annotated_image`` —— 监控端靠它拉那张画了所有框的整图。
  4. ``POST /v1/detect`` 也会广播 (不只 WS 触发才广播)。
  5. 追溯系统能存出「画了所有框」的那张总览图。

跑法::

    cd services/vision_service
    python test_on_demand.py
"""

from __future__ import annotations

import asyncio
import json
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}  {detail}")


# ---------------------------------------------------------------- 追溯总览图

def check_annotated_image() -> None:
    """追溯系统能存出「画了所有框」的那张整图。"""
    print("\n[1] 标注总览图")
    from tracing import DetectionTracer

    with tempfile.TemporaryDirectory() as tmpdir:
        tracer = DetectionTracer(tmpdir)
        image = np.zeros((200, 300, 3), dtype=np.uint8)
        image[:, :] = (40, 40, 40)

        objects = [
            {
                "index": 1, "label": "cup", "confidence": 0.9,
                "bbox": {"x1": 10, "y1": 20, "x2": 100, "y2": 150},
                "pose": {"x": 0.1, "y": 0.2, "z": 0.5},
            },
            {
                "index": 2, "label": "bottle", "confidence": 0.8,
                "bbox": {"x1": 120, "y1": 30, "x2": 200, "y2": 180},
                "pose": {"x": 0.3, "y": 0.1, "z": 0.6},
            },
        ]

        path = tracer.save_annotated(image, objects, "session_test")
        check("返回了相对路径", bool(path), f"got={path!r}")
        if path:
            full = tracer.get_image_path(path)
            check("图片文件真的落盘了", full is not None and full.exists(),
                  f"path={full}")
            if full and full.exists():
                import cv2
                saved = cv2.imread(str(full))
                check("图片能读出来", saved is not None)
                if saved is not None:
                    check("尺寸跟原图一致", saved.shape == image.shape,
                          f"{saved.shape} vs {image.shape}")
                    # 框画上去之后像素应该有变化
                    check("图上确实画了东西", int(saved.sum()) != int(image.sum()))

        # ---- 空列表也不该崩 ----
        path2 = tracer.save_annotated(image, [], "session_test")
        check("空列表也能存", bool(path2))


# ---------------------------------------------------------------- 广播机制

def check_broadcast() -> None:
    """服务的广播机制: 订阅 -> 收到 -> 注销。"""
    print("\n[2] 检测结果广播")
    import queue
    from service import VisionService

    svc = VisionService.__new__(VisionService)   # 不跑 __init__ (要相机)
    import threading
    svc._subscribers = []
    svc._sub_lock = threading.Lock()

    q: queue.Queue = queue.Queue()
    callback = lambda msg: q.put(msg)  # noqa: E731
    svc.subscribe(callback)
    check("订阅成功", len(svc._subscribers) == 1)

    svc._broadcast({"type": "detection", "objects": []})
    check("订阅者收到了消息", not q.empty())
    if not q.empty():
        msg = q.get()
        check("消息类型是 detection", msg.get("type") == "detection")

    svc.unsubscribe(callback)
    check("注销成功", len(svc._subscribers) == 0)

    svc._broadcast({"type": "detection"})
    check("注销后不再收到", q.empty())


# ---------------------------------------------------------------- WebSocket

async def _ws_check() -> None:
    """连上 WS 不产生检测, 发命令才检测。"""
    print("\n[3] WebSocket 按需触发")

    from fastapi.testclient import TestClient

    import main as vision_main

    # ---- 把服务换成假的 (不碰相机) ----
    #
    # lifespan 里会 `service = VisionService(cfg)` —— 自己 new 一个实例。
    # 所以这里用 ``instances`` 把**实际被用的那个**抓出来, 后面的断言
    # 都得看它, 不能看测试自己 new 的那个。
    instances = []

    class FakeService:
        """假的视觉服务 —— 记录被调了几次。"""

        def __init__(self, *args, **kwargs) -> None:
            self.detect_calls = 0
            self.subscribers = []
            instances.append(self)

        def subscribe(self, callback):
            self.subscribers.append(callback)

        def unsubscribe(self, callback):
            if callback in self.subscribers:
                self.subscribers.remove(callback)

        def start(self):
            pass

        def stop(self):
            pass

        def detect(self, classes=None, conf=None, pose=False, frame="camera"):
            self.detect_calls += 1
            result = {
                "objects": [
                    {
                        "index": 1, "label": "cup", "label_zh": "杯子",
                        "confidence": 0.9,
                        "bbox": {"x1": 1, "y1": 2, "x2": 3, "y2": 4},
                        "pose": {"x": 0, "y": 0, "z": 0, "frame": "camera"},
                        "record_id": "123_0001",
                        "image_filename": "s/cup_01.jpg",
                    }
                ],
                "latency_ms": 12,
                "timestamp_us": 1,
                "frame": "camera",
                "requested_frame": frame,
                "session": "s",
                "annotated_image": "s/_annotated.jpg",
            }
            # 真实服务是在 detect() 内部广播的 —— 假服务也得照做,
            # 不然测不出「HTTP 触发也会广播」这条。
            self._broadcast({
                "type": "detection",
                "objects": result["objects"],
                "latency_ms": result["latency_ms"],
                "frame": result["frame"],
                "session": result["session"],
                "annotated_image": result["annotated_image"],
            })
            return result

        def _broadcast(self, message):
            for callback in list(self.subscribers):
                callback(message)

    # ---- 在 lifespan 跑之前把 VisionService 换掉 ----
    #
    # lifespan 里会 `service = VisionService(cfg)` —— 不换掉的话它会去
    # 开相机, 相机没插时这一句就能卡死整个测试。
    #
    # 换掉之后, lifespan 建的那个实例会被 ``instances`` 收走 ——
    # 后面的断言全都看它。
    original_cls = vision_main.VisionService
    vision_main.VisionService = FakeService  # type: ignore[misc]

    def make_receiver(ws):
        """给一条 WS 连接配一个**常驻**收消息线程。

        TestClient 的 ``receive_json`` 是阻塞的 —— 没消息就永远不返回。
        所以「连上后有没有自动推送」这种检查必须靠超时, 而超时的
        收消息线程如果直接扔掉, 它**还活着** —— 下一条消息会被它
        抢走, 后面的断言就永远收不到东西了。

        所以这里用一个常驻线程 + 队列: 线程一直收, 消息进队列;
        测试这边按需从队列里取, 取不到就是「没消息」。
        """
        inbox: "queue.Queue" = queue.Queue()

        def worker():
            while True:
                try:
                    inbox.put(ws.receive_json())
                except Exception:
                    return

        threading.Thread(target=worker, daemon=True).start()
        return inbox

    def recv_until(inbox, predicate, timeout=3.0):
        """从队列里等一条满足条件的消息 (超时返回 None)。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            if predicate(msg):
                return msg
        return None

    try:
        with TestClient(vision_main.app) as client:
            # lifespan 里真的建了一个假服务 —— 用它做断言
            check("lifespan 用了假服务 (没碰相机)", len(instances) == 1,
                  f"instances={len(instances)}")
            svc = instances[0] if instances else None
            if not svc:
                return

            with client.websocket_connect("/ws") as ws:
                inbox = make_receiver(ws)

                # ---- 连上之后先等一会儿 ----
                #
                # 旧实现会在这段时间里自己推检测结果过来 ——
                # 现在应该**什么都收不到**。
                time.sleep(1.5)
                check("连上后不会自动推送 (没有轮询)", inbox.empty(),
                      f"unexpected={list(inbox.queue)[:1]!r}")
                check("连上后服务端没有自己拍帧", svc.detect_calls == 0,
                      f"calls={svc.detect_calls}")

                # ---- 发命令触发 ----
                ws.send_text(json.dumps({"action": "detect"}))

                # ---- 结果会广播回来 ----
                #
                # 注意: WS 命令是在工作线程里跑 detect 的, 所以
                # 「发完命令立刻查计数」是**竞态** —— 先等广播回来,
                # 那时检测一定已经跑完了。
                received = recv_until(
                    inbox, lambda m: m.get("type") == "detection")
                check("广播了检测结果", received is not None)
                check("触发了一次检测", svc.detect_calls == 1,
                      f"calls={svc.detect_calls}")
                if received:
                    check("结果里有 objects",
                          isinstance(received.get("objects"), list)
                          and len(received["objects"]) == 1)
                    check("结果带 annotated_image (标注总览图)",
                          received.get("annotated_image") == "s/_annotated.jpg",
                          f"got={received.get('annotated_image')!r}")

            # ---- HTTP 触发也会广播 ----
            #
            # 监控端点「识别一次」走的就是这条路。
            with client.websocket_connect("/ws") as ws2:
                inbox2 = make_receiver(ws2)

                resp = client.post("/v1/detect", json={})
                check("HTTP /v1/detect 返回 200", resp.status_code == 200,
                      f"status={resp.status_code} body={resp.text[:200]}")
                check("HTTP 也触发了一次检测", svc.detect_calls == 2,
                      f"calls={svc.detect_calls}")

                got = recv_until(
                    inbox2, lambda m: m.get("type") == "detection")
                check("HTTP 触发的结果也广播给了 WS 订阅者", got is not None)

                # HTTP 响应里也带 annotated_image
                if resp.status_code == 200:
                    check("HTTP 响应里有 annotated_image",
                          resp.json().get("annotated_image") == "s/_annotated.jpg")
    finally:
        vision_main.VisionService = original_cls  # type: ignore[misc]


def main() -> int:
    print("=" * 62)
    print("按需识别 (不轮询) 集成测试")
    print("=" * 62)

    check_annotated_image()
    check_broadcast()
    asyncio.run(_ws_check())

    print()
    print("=" * 62)
    print(f"结果: {PASSED} 通过, {FAILED} 失败")
    print("=" * 62)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
