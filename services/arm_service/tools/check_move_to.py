#!/usr/bin/env python3
"""`POST /v1/move_to` 联调 (mock 控制器 + 真实 arm_service)。

mock 在 check_arm_service 的基础上补了末端控制: 收到绝对位姿包就把 TCP 挪过去,
并可注入「目标够不着」(只走到半路, 控制器报 poseClamped)。

验证三件事:
  1. 门阀拒绝: 急停时不入控、不发流
  2. 正常到位: 入控参数正确 (cartesianIkPath=analytic), TCP 到目标, 误差达标
  3. 够不着: 超出 max_reach 预检拒绝; 控制器夹紧时报「够不着」

用法::
    python tools/check_move_to.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

TOOLS_DIR = Path(__file__).resolve().parent
SERVICE_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(SERVICE_DIR))

from check_arm_service import MockRobotServer, check, _results  # noqa: E402

BASE = "http://127.0.0.1:8893"
START = (0.30, 0.20, 0.10)


class CartesianMock(MockRobotServer):
    def __init__(self) -> None:
        super().__init__([0.0] * 7)
        self.tcp = list(START)
        self.clamp_to: Optional[list] = None  # 注入: 目标被夹紧到这个点
        self.clamped = False

    def _udp_loop(self) -> None:  # 收包同时更新 TCP
        import socket

        self.udp.settimeout(0.05)
        while not self._stop.is_set():
            try:
                data, _ = self.udp.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                pkt = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            self.udp_packets.append(pkt)
            if pkt.get("commandMode") == 0 and pkt.get("controlSpace") == 1:
                v = pkt["value"][:3]
                if self.clamp_to is not None:
                    self.clamped = True
                    v = self.clamp_to
                self.tcp = list(v)

    def _status_db(self) -> Dict[str, Any]:
        return {
            "name": self.arm_name,
            "joints": list(self.actual),
            "cartesian": dict(zip("xyz", self.tcp), a=0.0, b=0.0, c=0.0),
            "baseFrame": {"x": 0.0, "y": 0.0, "z": 0.0, "a": 0.0, "b": 0.0, "c": 0.0},
            "baseFrameKind": "static",
        }

    async def _send_state(self, ws) -> None:
        try:
            await ws.send(json.dumps({
                "ty": f"publish/robot/{self.arm_name}/status",
                "db": {**self._status_db(), "enabled": True},
            }))
        except Exception:
            pass

    async def _respond(self, ws, msg: Dict[str, Any]) -> None:
        ty, mid = msg.get("ty", ""), msg.get("id", 0)
        if ty == "robot/status":
            await super()._respond(ws, msg)  # 先发门阀要的完整应答
            await self._send_state(ws)  # 再推一份带 cartesian 的快照
            return
        db: Dict[str, Any]
        if ty == "realtime/startControl":
            db = {"state": "active", "pose": {"quaternion": [*self.tcp, 1.0, 0.0, 0.0, 0.0]}}
        elif ty == "realtime/getStatus":
            stage = "poseClamped" if self.clamped else "normal"
            dev = 0.2 if self.clamped else 0.0
            db = {"commandPort": self.udp_port, "commandPortReady": True,
                  "sessions": [{"robot": self.arm_name, "state": "active", "error": "none",
                                "analyticIkStage": stage, "analyticIkPositionDeviation": dev}]}
        else:
            await super()._respond(ws, msg)
            return
        await ws.send(json.dumps({"id": mid, "ty": ty, "db": db}))


def post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode() or "{}")


def main() -> int:
    robot = CartesianMock()
    ws_port = robot.start_ws()
    env = dict(os.environ, ARM_CONTROLLER_WS=f"ws://127.0.0.1:{ws_port}", ARM_SERVER_PORT="8893",
               ARM_ACTION_DIR=str(SERVICE_DIR / "data"))
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=str(SERVICE_DIR), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(BASE + "/health", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("服务启动失败")
            return 1
        time.sleep(1.0)

        body = {"arm": "left", "x": 0.40, "y": 0.20, "z": 0.10, "duration": 1.0, "speak": False}

        print("\n[1] 门阀拒绝 (急停)")
        robot.inject_estop = True
        n0 = len(robot.commands)
        r = post("/v1/move_to", body)
        check("被拒绝", r.get("ok") is False, f"{r.get('code')}: {r.get('message')}")
        check("在门阀阶段拒绝", r.get("stage") == "safety", str(r.get("stage")))
        check("没有发 startControl", not any(
            c.get("ty") == "realtime/startControl" for c in robot.commands[n0:]))
        check("没有发末端包", not any(p.get("controlSpace") == 1 for p in robot.udp_packets))
        robot.inject_estop = False

        print("\n[2] 正常到位")
        r = post("/v1/move_to", body)
        check("成功", r.get("ok") is True, f"{r.get('code')}: {r.get('message')}")
        starts = [c for c in robot.commands if c.get("ty") == "realtime/startControl"]
        check("发了 startControl", len(starts) == 1)
        if starts:
            raw = json.dumps(starts[0])
            check("cartesianIkPath=analytic", "analytic" in raw, raw[:200])
        hold = [p for p in robot.udp_packets if p.get("commandMode") == 2]
        absolute = [p for p in robot.udp_packets if p.get("commandMode") == 0]
        check("先发零增量保持包", len(hold) > 0, f"{len(hold)} 个")
        check("再发绝对轨迹包", len(absolute) > 10, f"{len(absolute)} 个")
        if absolute:
            last = absolute[-1]["value"]
            check("终点是目标 (0.40,0.20,0.10)",
                  all(abs(a - b) < 1e-6 for a, b in zip(last[:3], (0.40, 0.20, 0.10))), str(last[:3]))
        check("stopControl 已发", any(c.get("ty") == "realtime/stopControl" for c in robot.commands))
        check("TCP 到位", all(abs(a - b) < 0.01 for a, b in zip(robot.tcp, (0.40, 0.20, 0.10))),
              str([round(v, 3) for v in robot.tcp]))
        check("误差 < 2cm", float(r.get("error_m", 9)) < 0.02, str(r.get("error_m")))

        print("\n[3] 够不着")
        n_cmd = len(robot.commands)
        r = post("/v1/move_to", {**body, "x": 3.0, "y": 0.0, "z": 0.0})
        check("超出 max_reach 预检拒绝", r.get("ok") is False and r.get("stage") in ("plan",),
              f"{r.get('code')} stage={r.get('stage')}: {r.get('message')}")
        check("预检拒绝时没入控", not any(
            c.get("ty") == "realtime/startControl" for c in robot.commands[n_cmd:]))

        robot.clamp_to = [0.45, 0.20, 0.10]  # 目标在范围内, 但控制器夹紧
        r = post("/v1/move_to", {**body, "x": 0.60, "y": 0.20, "z": 0.10})
        check("控制器夹紧被识别为失败", r.get("ok") is False, f"{r.get('code')}: {r.get('message')}")
        check("原因是够不着", r.get("code") in ("unreachable", "not_arrived"), str(r.get("code")))
    finally:
        proc.terminate()
        robot.stop()
        try:
            out = proc.stdout.read() if proc.stdout else ""
        except Exception:
            out = ""
        if any(not ok for _, ok, _ in _results):
            print("\n---- arm_service 日志 (尾部) ----")
            print("\n".join(out.splitlines()[-40:]))

    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
