#!/usr/bin/env python3
"""断连重连 + 陈旧数据标记的回归测试。

**为什么要有这个**

上次手眼标定翻车的链条:

1. 控制器 WebSocket 断开 (keepalive ping timeout)
2. ``connection.py`` 的读线程 ``break`` 退出 —— **没有任何重连**
3. 服务还活着, ``/v1/kinematics`` 照样返回 200, 但返回的是
   **断连前的陈旧快照**
4. 标定采了 25 帧, ``T_world_tool`` **完全相同** -> 解算被剔到只剩 1 个

这个测试用 mock 控制器模拟「连上 -> 掉线 -> 应该自动重连」,
并验证 ``/v1/kinematics`` 会把陈旧状态**明确标出来**。

用法::

    conda run -n botagent python tools/check_reconnect.py
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_results: List[Tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    _results.append((name, condition, detail))
    tag = PASS if condition else FAIL
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""))
    return condition


# ---------------------------------------------------------------- mock 控制器


class FlakyController:
    """会主动断线的假控制器。

    用真的 ``websockets`` 库跑服务端 (握手得是标准的) ——
    手写帧不兼容, 连不上。
    """

    def __init__(self, port: int = 0) -> None:
        self.port = port
        self.connections = 0
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._loop = None
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        #: 断掉当前所有连接 (模拟 keepalive 超时)
        self._conns: List[Any] = []

    def start(self) -> int:
        try:
            from websockets.asyncio.server import serve
        except ImportError as exc:
            raise RuntimeError(f"缺少 websockets 依赖: {exc}") from exc

        import asyncio

        loop = asyncio.new_event_loop()
        self._loop = loop

        async def handler(ws):
            with self._lock:
                self.connections += 1
                self._conns.append(ws)
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    await self._respond(ws, msg)
            except Exception:
                pass

        async def run_server():
            async with serve(handler, "127.0.0.1", self.port) as server:
                self._server = server
                self.port = server.sockets[0].getsockname()[1]
                self._ready.set()
                while not self._stop.is_set():
                    await asyncio.sleep(0.05)

        def thread_main():
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(run_server())
            except Exception:
                pass

        self._thread = threading.Thread(target=thread_main, daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            return 0
        return self.port

    async def _respond(self, ws, msg: Dict[str, Any]) -> None:
        ty = msg.get("ty", "")
        mid = msg.get("id", 0)
        if ty == "robot/list":
            await ws.send(json.dumps({
                "id": mid, "ty": "robot/list",
                "db": [{"id": 0, "name": "LeftArm"},
                       {"id": 1, "name": "RightArm"}],
            }))
        elif ty == "robot/status":
            name = (msg.get("db") or {}).get("name", "RightArm")
            await ws.send(json.dumps({
                "id": mid, "ty": "robot/status",
                "db": {
                    "name": name,
                    "joints": [0.1] * 7,
                    "cartesian": {"x": 0.5, "y": -0.06, "z": -0.3,
                                  "a": 2.1, "b": 0.7, "c": 2.8},
                    "baseFrame": {"x": 0.0, "y": -0.125, "z": 0.0,
                                  "a": 1.570796, "b": 1.570796, "c": 0.0},
                    "baseFrameKind": "static",
                    "enabled": True,
                },
            }))

    def drop_all(self) -> None:
        """强行断开所有连接 (模拟控制器侧掉线)。

        **必须用传输层关闭**: ``ws.close()`` 是协程, 在别的线程里
        ``call_soon_threadsafe`` 调度没问题, 但如果事件循环本身被
        阻塞就静默失败 —— 表现为「掉线不掉」。
        所以这里直接 ``transport.close()``, 立刻断 TCP。
        """
        with self._lock:
            conns = list(self._conns)
            self._conns.clear()
        for ws in conns:
            try:
                transport = ws.transport
                if transport is not None:
                    transport.abort()
            except Exception:
                pass
            try:
                ws.protocol.fail_connection(1011, "mock drop")
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        self.drop_all()


# ---------------------------------------------------------------- 测试


def test_reconnect() -> None:
    """起一个真 arm_service (指向 mock 控制器), 验证断连后能自动重连。"""
    import main as arm_main
    from config import load_config
    from service import ArmService

    ctrl = FlakyController()
    port = ctrl.start()

    # 直接建一个指向 mock 控制器的 service
    cfg = load_config()
    cfg.arm.controller_ws = f"ws://127.0.0.1:{port}"
    svc = ArmService(cfg)

    print("\n[1] 连接 mock 控制器")
    ok = svc._conn.connect()
    check("能连上 mock 控制器", ok)
    if not ok:
        return

    # 模拟正常启动流程: 连上 -> 拉机器人清单 -> 识别左右臂 -> 查状态
    svc._on_connected()

    # 等状态快照到达
    time.sleep(1.5)
    kin = svc.kinematics()
    check("收到了状态快照", bool(kin.get("arms")),
          f"arms={list(kin.get('arms', {}).keys())}")
    check("快照不是陈旧的", not kin.get("stale"),
          f"stale={kin.get('stale')}")
    check("ok 为 true", kin.get("ok") is True)

    print("\n[2] 模拟控制器掉线")
    ctrl.drop_all()
    # 等读线程发现断连 + 进入重连
    #
    # 退避是 1s -> 2s -> 4s -> 8s, 最坏要等 ~15s 才重连上, 所以窗口给 30s。
    t0 = time.time()
    reconnected = False
    while time.time() - t0 < 30.0:
        if svc._conn.connected and ctrl.connections >= 2:
            reconnected = True
            break
        time.sleep(0.2)

    check("断连后自动重连", reconnected,
          f"耗时 {time.time() - t0:.1f}s, connections={ctrl.connections}")
    check("重连次数 >= 2", ctrl.connections >= 2,
          f"connections={ctrl.connections}")

    print("\n[3] 重连后能重新收到状态")
    svc._on_reconnected()
    time.sleep(1.5)
    kin2 = svc.kinematics()
    check("重连后又收到状态快照", bool(kin2.get("arms")),
          f"arms={list(kin2.get('arms', {}).keys())}")
    check("重连后快照不是陈旧的", not kin2.get("stale"),
          f"stale={kin2.get('stale')}")
    check("重连后 age_ms 很小 (数据是新的)", kin2.get("age_ms", 99999) < 2000,
          f"age_ms={kin2.get('age_ms')}")

    svc._conn.disconnect()
    ctrl.stop()


def test_stale_flag() -> None:
    """断连时 /v1/kinematics 必须明确标 stale=true。"""
    from fastapi.testclient import TestClient

    import main as arm_main
    from config import load_config
    from service import ArmService

    print("\n[4] 断连时 stale 标记")
    cfg = load_config()
    svc = ArmService(cfg)
    arm_main.service = svc
    # 强制标记成断连
    svc._conn._connected = False

    try:
        with TestClient(arm_main.app) as client:
            resp = client.get("/v1/kinematics?refresh=true")
            check("接口仍然返回 200", resp.status_code == 200,
                  f"HTTP {resp.status_code}")
            data = resp.json()
            check("stale 被标为 true", data.get("stale") is True,
                  f"stale={data.get('stale')}")
            check("controller_connected 为 false",
                  data.get("controller_connected") is False)
            check("ok 为 false (数据不可信)", data.get("ok") is False,
                  f"ok={data.get('ok')}")
            notes = " ".join(data.get("notes") or [])
            check("notes 里说明了断连", "断开" in notes or "陈旧" in notes,
                  notes[:80])
    finally:
        arm_main.service = None


def test_startup_reconnect() -> None:
    """**控制器晚启动** —— 服务起来时控制器还没开, 应该自动接上。

    之前只试连一次, 控制器晚启动就永远连不上, 得手动重启服务。
    """
    import asyncio

    from config import load_config
    from service import ArmService

    print("\n[5] 启动时控制器不可达 -> 后台自动重连")

    # 先占一个端口再释放 —— 拿到一个「确定此刻没人监听」的端口号
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    cfg = load_config()
    cfg.arm.controller_ws = f"ws://127.0.0.1:{port}"
    svc = ArmService(cfg)

    asyncio.run(svc.start())
    check("控制器不可达时服务仍能启动", svc._started)
    check("初始状态是未连接", not svc._conn.connected)

    # 现在「控制器上线」—— 在同一个端口起 mock
    ctrl = FlakyController(port=port)
    ctrl.start()

    # 等后台重连线程接上
    t0 = time.time()
    connected = False
    while time.time() - t0 < 30.0:
        if svc._conn.connected:
            connected = True
            break
        time.sleep(0.3)

    check("控制器上线后自动连上", connected, f"耗时 {time.time() - t0:.1f}s")
    check("连上后 connected 为 True", svc._conn.connected)

    svc.shutdown()
    ctrl.stop()


def main() -> int:
    print("=" * 62)
    print("arm_service 断连重连 + 陈旧数据标记 自检")
    print("=" * 62)

    test_reconnect()
    test_stale_flag()
    test_startup_reconnect()

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print()
    print("=" * 62)
    print(f"结果: {passed} 通过, {failed} 失败")
    print("=" * 62)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
