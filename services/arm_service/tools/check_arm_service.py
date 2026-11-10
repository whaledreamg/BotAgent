#!/usr/bin/env python3
"""机械臂动作服务端到端联调 (mock 控制器 + 真实 HTTP/WS 服务)。

起一个假的机器人控制器 (WebSocket 控制面 + UDP 指令面), 再起真正的
arm_service, 然后通过 HTTP 下发播放、通过 WebSocket 观察阶段与进度。

验证的是**服务对外契约**, 与 check_arm.py 的协议级验证互补。

用法::
    python tools/check_arm_service.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_results: List[tuple] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    _results.append((name, condition, detail))
    tag = PASS if condition else FAIL
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""))
    return condition


# ---------------------------------------------------------------- mock 控制器


class MockRobotServer:
    """假机器人控制器。

    WebSocket 控制面: 接受连接, 响应 robot/list / robot/status /
    realtime/getStatus / realtime/startControl, 并主动推送状态。
    UDP 指令面: 收关节流包。
    """

    def __init__(self, actual: List[float]) -> None:
        self.actual = actual
        self.arm_name = "LeftArm"
        #: 故障注入: 置了这些字段, mock 就会回报对应的异常状态
        self.inject_faulted = False
        self.inject_estop = False

        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp_port = self.udp.getsockname()[1]
        self.udp_packets: List[Dict[str, Any]] = []

        self.ws_port = 0
        self.commands: List[Dict[str, Any]] = []
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

        self._udp_thread = threading.Thread(target=self._udp_loop, daemon=True)
        self._udp_thread.start()

    def _udp_loop(self) -> None:
        self.udp.settimeout(0.05)
        while not self._stop.is_set():
            try:
                data, _ = self.udp.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self.udp_packets.append(json.loads(data.decode("utf-8")))
            except json.JSONDecodeError:
                pass

    def start_ws(self) -> int:
        """起一个 WebSocket 服务端 (在独立线程里跑事件循环)。

        必须与客户端分处不同线程: websockets 的同步客户端会阻塞当前线程,
        如果服务端的事件循环也在这个线程里, 握手就永远等不到响应
        (表现为 client 侧 "timed out while waiting for handshake response")。
        """
        try:
            from websockets.asyncio.server import serve
        except ImportError as exc:
            raise RuntimeError(f"缺少 websockets 依赖: {exc}") from exc

        import asyncio

        holder: Dict[str, Any] = {"port": 0, "ready": threading.Event()}
        loop = asyncio.new_event_loop()

        async def handler(ws):
            await self._send_state(ws)
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    self.commands.append(msg)
                    await self._respond(ws, msg)
            except Exception:
                pass

        async def run_server():
            async with serve(handler, "127.0.0.1", 0) as server:
                holder["port"] = server.sockets[0].getsockname()[1]
                holder["ready"].set()
                while not self._stop.is_set():
                    await asyncio.sleep(0.05)

        def thread_main():
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(run_server())
            except Exception:
                pass

        thread = threading.Thread(target=thread_main, daemon=True)
        thread.start()
        self._threads.append(thread)

        if not holder["ready"].wait(5.0):
            return 0
        self.ws_port = holder["port"]
        return self.ws_port

    async def _send_state(self, ws) -> None:
        try:
            await ws.send(json.dumps({
                "ty": f"publish/robot/{self.arm_name}/status",
                "db": {"name": self.arm_name, "joints": list(self.actual),
                       "enabled": True},
            }))
        except Exception:
            pass

    async def _respond(self, ws, msg: Dict[str, Any]) -> None:
        ty = msg.get("ty", "")
        mid = msg.get("id", 0)
        db: Dict[str, Any] = {}

        if ty == "robot/list":
            db = {"items": [{"name": self.arm_name, "subType": "HM5-54-L"}]}
        elif ty == "robot/status":
            # 安全门阀要看的字段必须齐全 —— 缺一个字段门阀就判不通过,
            # 动作会被拦下来 (这正是门阀的预期行为)。
            db = {
                "name": self.arm_name,
                "joints": list(self.actual),
                "enabled": True,
                "mode": "CSPPassthrough",
                "modeCode": 2,
                "channel": "Planned",
                "faulted": self.inject_faulted,
                "faultCode": 13220 if self.inject_faulted else 0,
                "emergencyStopActive": self.inject_estop,
                "safetyResponse": "none",
                "safetyResponseCode": 0,
                "safetyPhase": "none",
                "safetyPhaseCode": 0,
            }
        elif ty == "realtime/getStatus":
            db = {"commandPort": self.udp_port, "commandPortReady": True,
                  "sessions": []}
        elif ty == "realtime/startControl":
            db = {"state": "active", "commandChannel": "Direct"}
        elif ty == "realtime/stopControl":
            db = {}

        try:
            await ws.send(json.dumps({"id": mid, "ty": ty, "db": db}))
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        try:
            self.udp.close()
        except OSError:
            pass


# ---------------------------------------------------------------- 主流程


def _shortest_action(base: str) -> str:
    """从动作清单里挑最短的动作。

    动作名不写死 —— 动作库换了内容这个测试照样能跑。
    """
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base}/v1/actions", timeout=8) as resp:
            body = json.load(resp)
        items = body.get("actions") or []
        if items:
            shortest = min(items, key=lambda a: float(a.get("duration", 0)))
            return str(shortest.get("key") or shortest.get("label"))
    except Exception:
        pass
    # 拿不到就用动作名兜底 (至少能测协议流程)
    return "零点"


def _two_shortest_actions(base: str) -> List[str]:
    """挑两个最短的动作 (用来测队列)。

    动作名不写死 —— 动作库换了内容这个测试照样能跑。
    """
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base}/v1/actions", timeout=8) as resp:
            body = json.load(resp)
        items = body.get("actions") or []
        if len(items) >= 2:
            ordered = sorted(items, key=lambda a: float(a.get("duration", 0)))
            return [
                str(a.get("key") or a.get("label")) for a in ordered[:2]
            ]
        if items:
            one = str(items[0].get("key") or items[0].get("label"))
            return [one, one]
    except Exception:
        pass
    return ["零点", "零点"]


def main() -> int:
    import urllib.error
    import urllib.request

    print("=" * 60)
    print("机械臂动作服务端到端联调 (mock 控制器)")
    print("=" * 60)

    actual = [0.2, -0.4, 0.6, 0.05, -0.15, 0.25, 0.0]
    robot = MockRobotServer(actual)
    ws_port = robot.start_ws()
    print(f"\nmock 控制器: ws://127.0.0.1:{ws_port}  udp={robot.udp_port}")

    env = dict(os.environ)
    env["ARM_CONTROLLER_WS"] = f"ws://127.0.0.1:{ws_port}"
    env["ARM_SERVER_PORT"] = "8892"
    env["ARM_ACTION_DIR"] = str(SERVICE_DIR / "data")

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=str(SERVICE_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    base = "http://127.0.0.1:8892"
    try:
        # 等服务就绪
        ok = False
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=2) as resp:
                    health = json.load(resp)
                    ok = True
                    break
            except Exception:
                time.sleep(0.5)
        if not ok:
            print("服务启动失败")
            proc.terminate()
            print(proc.stdout.read() if proc.stdout else "")
            return 1

        print("\n[1] 健康检查")
        check("服务已就绪", health.get("status") == "ok")
        check("已连上 mock 控制器", health.get("controller_connected") is True,
              str(health.get("controller_ws")))
        check("动作库已加载", health.get("actions", 0) > 0,
              f"{health.get('actions')} 个")

        # WebSocket 监听
        from websockets.sync.client import connect

        events: List[Dict[str, Any]] = []
        ws = connect("ws://127.0.0.1:8892/ws")
        ready = json.loads(ws.recv(timeout=5))
        check("WS 收到 ready 事件", ready.get("type") == "ready", str(ready))

        def ws_reader():
            try:
                while True:
                    events.append(json.loads(ws.recv(timeout=30)))
            except Exception:
                pass

        threading.Thread(target=ws_reader, daemon=True).start()

        # 下发播放
        #
        # 用最短的动作: 测试有 25s 的完成等待窗, 长的动作等不起,
        # 会让「播放完成」相关断言全部落空。动作名从动作库动态取, 不写死。
        print("\n[2] 下发播放")
        action_name = _shortest_action(base)
        req = urllib.request.Request(
            f"{base}/v1/play",
            data=json.dumps({"action": action_name, "speed": 2.0}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            play_resp = json.load(resp)
        check("播放指令被接受", play_resp.get("ok") is True, str(play_resp.get("key")))
        check("返回了动作时长", play_resp.get("duration", 0) > 0,
              f"{play_resp.get('duration')}s")

        # 等播放完成
        #
        # 等 WS 的 finished 事件, 而不是轮询 status 的 phase ——
        # 刚下发指令时 phase 可能短暂还是 idle (线程还没起来), 轮询会误判成
        # 「已经结束」而提前退出, 导致后面的断言全部落空。
        deadline = time.time() + 25.0
        finished = False
        while time.time() < deadline:
            if any(e.get("type") == "finished" for e in events):
                finished = True
                break
            time.sleep(0.1)

        print("\n[3] 播放结果")
        phases = [e.get("phase") for e in events if e.get("type") == "state"]
        print(f"      阶段序列: {phases}")
        check("收到 finished 事件", finished)
        check("阶段经过 ENTERING/ALIGNING/PLAYING",
              any(p in ("entering", "aligning") for p in phases)
              and "playing" in phases,
              str(phases))

        # 停控是后台线程做的, 给它一点时间
        deadline = time.time() + 5.0
        while time.time() < deadline and not robot.commands:
            time.sleep(0.1)
        stop_sent = any(c.get("ty") == "realtime/stopControl" for c in robot.commands)
        check("播放结束已发 stopControl (交还通道)", stop_sent)

        with urllib.request.urlopen(f"{base}/v1/status", timeout=3) as resp:
            final_status = json.load(resp)
        check("最终回到 IDLE", final_status.get("phase") == "idle",
              str(final_status.get("phase")))

        # 控制面命令
        cmds = [c.get("ty") for c in robot.commands]
        print(f"      控制面命令: {cmds}")
        check("发过 robot/list", "robot/list" in cmds)
        check("发过 realtime/getStatus (清残留)", "realtime/getStatus" in cmds)
        check("发过 realtime/startControl (入控)", "realtime/startControl" in cmds)
        check("发过 realtime/stopControl (交还通道)", "realtime/stopControl" in cmds)

        # UDP 指令面
        print("\n[4] 关节流")
        check("收到了 UDP 关节包", len(robot.udp_packets) > 0,
              f"{len(robot.udp_packets)} 包")
        if robot.udp_packets:
            pkt = robot.udp_packets[0]
            check("报文含 robot 字段", pkt.get("robot") == robot.arm_name)
            check("value 长度正确", len(pkt.get("value", [])) == 7)
            check("首包贴近实测角",
                  all(abs(pkt["value"][i] - actual[i]) < 1e-6 for i in range(7)),
                  str([round(v, 3) for v in pkt["value"][:3]]))

        # 停止接口
        print("\n[5] 停止接口")
        req = urllib.request.Request(f"{base}/v1/stop", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            stop_resp = json.load(resp)
        check("停止接口可用", stop_resp.get("ok") is True, str(stop_resp.get("message")))

        # ---- 安全门阀: 故障注入 ----
        print("\n[6] 安全门阀 (故障注入)")
        robot.inject_faulted = True
        robot.inject_estop = True

        with urllib.request.urlopen(f"{base}/v1/safety", timeout=10) as resp:
            safety = json.load(resp)
        check("门阀识别出故障", safety.get("ok") is False, str(safety.get("reason")))
        check("给出故障原因", bool(safety.get("reason")), str(safety.get("reason")))
        print(f"      门阀结论: code={safety.get('code')} reason={safety.get('reason')}")

        # 有故障时下发动作, 应当被拒绝 (不进���控, 不发关节流)
        cmds_before = len(robot.commands)
        pkts_before = len(robot.udp_packets)
        req = urllib.request.Request(
            f"{base}/v1/play",
            data=json.dumps({"action": "挥手"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.load(resp)
        time.sleep(2.0)

        new_cmds = [c.get("ty") for c in robot.commands[cmds_before:]]
        check("有故障时未入控", "realtime/startControl" not in new_cmds,
              str(new_cmds))
        check("有故障时未发关节流",
              len(robot.udp_packets) == pkts_before,
              f"{len(robot.udp_packets) - pkts_before} 包")

        with urllib.request.urlopen(f"{base}/v1/status", timeout=5) as resp:
            blocked_status = json.load(resp)
        check("状态里带门阀结论", blocked_status.get("safety") is not None
              and blocked_status["safety"].get("ok") is False)
        print(f"      phase={blocked_status.get('phase')!r} "
              f"last_error={blocked_status.get('last_error')!r}")
        check("错误信息说明被门阀拒绝",
              "门阀" in (blocked_status.get("last_error") or ""),
              str(blocked_status.get("last_error")))

        # 恢复正常后应该能放行
        robot.inject_faulted = False
        robot.inject_estop = False
        with urllib.request.urlopen(f"{base}/v1/safety", timeout=10) as resp:
            safety_ok = json.load(resp)
        check("恢复正常后门阀放行", safety_ok.get("ok") is True,
              str(safety_ok.get("reason")))

        # ---- 播放队列 ----
        #
        # 多个动作按顺序播, **只入控一次** —— 这是队列的核心价值:
        # 每个动作单独播都要重新入控 (700ms 对齐 + 通道断开重连)。
        print("\n[7] 播放队列")
        events.clear()
        two = _two_shortest_actions(base)
        check("拿到两个动作", len(two) == 2, str(two))

        cmds_before = len(robot.commands)
        req = urllib.request.Request(
            f"{base}/v1/play",
            data=json.dumps({"actions": two, "speed": 2.0}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            queue_resp = json.load(resp)
        check("队列指令被接受", queue_resp.get("ok") is True, str(queue_resp))
        check("返回了动作列表", queue_resp.get("actions") == two,
              str(queue_resp.get("actions")))
        check("返回了队列总时长",
              float(queue_resp.get("duration") or 0) > 0,
              str(queue_resp.get("duration")))

        # 队列播完要等两个动作都结束
        deadline = time.time() + 40.0
        finished_count = 0
        while time.time() < deadline:
            finished_count = sum(
                1 for e in events if e.get("type") == "finished"
            )
            if finished_count >= 2:
                break
            time.sleep(0.1)
        check("收到两个 finished 事件", finished_count >= 2,
              f"{finished_count} 个")

        # **只入控一次** —— 队列的 핵심
        new_cmds = [c.get("ty") for c in robot.commands[cmds_before:]]
        start_count = new_cmds.count("realtime/startControl")
        check("整个队列只入控一次", start_count == 1,
              f"startControl x{start_count}: {new_cmds}")
        check("队列结束已交还通道",
              "realtime/stopControl" in new_cmds, str(new_cmds))

        with urllib.request.urlopen(f"{base}/v1/status", timeout=5) as resp:
            after_queue = json.load(resp)
        check("队列播完回到 IDLE", after_queue.get("phase") == "idle",
              str(after_queue.get("phase")))
        check("队列已清空", after_queue.get("queue") == [],
              str(after_queue.get("queue")))

        # 旧的单动作参数仍然能用 (向后兼容)
        print("\n[8] 向后兼容: 单动作参数")
        events.clear()
        single = _shortest_action(base)
        req = urllib.request.Request(
            f"{base}/v1/play",
            data=json.dumps({"action": single, "speed": 2.0}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            single_resp = json.load(resp)
        check("单动作仍然接受", single_resp.get("ok") is True, str(single_resp))
        check("单动作也返回 actions 列表",
              single_resp.get("actions") == [single],
              str(single_resp.get("actions")))

        # 不给动作要被拒
        req = urllib.request.Request(
            f"{base}/v1/play",
            data=json.dumps({"speed": 1.0}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                json.load(resp)
            check("没指定动作被拒", False, "居然成功了")
        except urllib.error.HTTPError as exc:
            check("没指定动作被拒", exc.code == 400, f"HTTP {exc.code}")

        ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        robot.stop()

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 60)
    print(f"结果: {passed}/{total} 通过")
    if passed != total:
        print("\n失败项:")
        for name, ok, detail in _results:
            if not ok:
                print(f"  - {name}  {detail}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
