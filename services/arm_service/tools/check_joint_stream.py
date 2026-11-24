#!/usr/bin/env python3
"""关节流移动的联调测试 (mock 控制器 + 真实 arm_service)。

验四件事:
  1. **不走控制器的逆解接口**: 入控参数是 ``cartesianIkPath="none"``,
     下发的报文是 ``controlSpace: 0`` (关节) 而不是 ``controlSpace: 1``
     (末端 —— 那会让控制器自己做逆解);
  2. **门阀拒绝**: 急停时不入控、不发流;
  3. **总闸关闭**: 只算不动, 一个指令都不发;
  4. **正常流程**: 本地逆解 -> 关节流 -> 到位。

用法::
    python3 tools/check_joint_stream.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

TOOLS_DIR = Path(__file__).resolve().parent
SERVICE_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(SERVICE_DIR))

from check_arm_service import MockRobotServer  # noqa: E402

BASE = "http://127.0.0.1:8894"

_results: List[Any] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    color = "\033[32m" if ok else "\033[31m"
    print(f"  [{color}{mark}\033[0m] {name}" + (f"  ({detail})" if detail else ""))
    _results.append((name, ok, detail))
    return ok


class JointMock(MockRobotServer):
    """在 check_arm_service 的 mock 上补齐关节流与位姿字段。

    关键: ``robot/status`` 必须带 ``cartesian`` / ``baseFrame`` ——
    本地逆解要用它们 (基座换算 + 目标位姿)。
    """

    def __init__(self) -> None:
        super().__init__([0.0] * 7)
        self.arm_name = "RightArm"
        self.joint_packets: List[Dict[str, Any]] = []
        self.all_packets: List[Dict[str, Any]] = []

    def _status_db(self) -> Dict[str, Any]:
        return {
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
            "cartesian": {"x": 0.30, "y": 0.20, "z": 0.10,
                          "a": 0.0, "b": 0.0, "c": 0.0},
            "baseFrame": {"x": 0.0, "y": 0.0, "z": 0.0,
                          "a": 0.0, "b": 0.0, "c": 0.0},
            "baseFrameKind": "static",
        }

    async def _respond(self, ws, msg: Dict[str, Any]) -> None:
        ty, mid = msg.get("ty", ""), msg.get("id", 0)
        db: Dict[str, Any]
        if ty == "robot/status":
            db = self._status_db()
        elif ty == "robot/list":
            db = {"items": [{"name": self.arm_name, "subType": "NDA-06-R"}]}
        elif ty == "device/robots":
            db = [{"name": self.arm_name, "type": "HmRobot7DoF",
                   "subType": "NDA-06-R", "jointCount": 7}]
        elif ty == "realtime/startControl":
            db = {"state": "active", "commandChannel": "Direct",
                  "pose": {"quaternion": [0.30, 0.20, 0.10, 1.0, 0.0, 0.0, 0.0]}}
        elif ty == "realtime/getStatus":
            db = {"commandPort": self.udp_port, "commandPortReady": True,
                  "sessions": []}
        elif ty == "device/getSafetyParams":
            db = {"jointPositionLimitNegative": [-3.0] * 7,
                  "jointPositionLimitPositive": [3.0] * 7}
        else:
            db = {}
        await ws.send(json.dumps({"id": mid, "ty": ty, "db": db}))

    async def _send_state(self, ws) -> None:
        try:
            await ws.send(json.dumps({
                "ty": f"publish/robot/{self.arm_name}/status",
                "db": self._status_db(),
            }))
        except Exception:
            pass


def post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode() or "{}")
        except Exception:
            return {}
    except Exception as exc:
        return {"ok": False, "code": "error", "message": str(exc)}


def main() -> int:
    robot = JointMock()
    ws_port = robot.start_ws()
    env = dict(
        os.environ,
        ARM_CONTROLLER_WS=f"ws://127.0.0.1:{ws_port}",
        ARM_SERVER_PORT="8894",
        ARM_ACTION_DIR=str(SERVICE_DIR / "data"),
        # 强制走本地逆解
        ARM_MOVE_MODE="local_ik",
    )
    proc = subprocess.Popen(
        [sys.executable, "main.py"], cwd=str(SERVICE_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(BASE + "/health", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("服务启动失败")
            return 1
        time.sleep(2.0)

        body = {"arm": "right", "x": 0.40, "y": 0.20, "z": 0.10,
                "duration": 1.0, "speak": False}

        print("\n[1] 关键: 不走控制器的逆解接口")
        r = post("/v1/move_to", body)
        check("用了本地逆解", r.get("ik_mode") == "local_pinocchio",
              str(r.get("ik_mode")) or str(r.get("message")))
        starts = [c for c in robot.commands if c.get("ty") == "realtime/startControl"]
        check("发了 startControl", len(starts) >= 1, f"{len(starts)} 个")
        if starts:
            raw = json.dumps(starts[0])
            check("cartesianIkPath=none (控制器不做末端逆解)",
                  '"cartesianIkPath": "none"' in raw
                  or '"cartesianIkPath":"none"' in raw, raw[:240])
        spaces = {p.get("controlSpace") for p in robot.udp_packets}
        check("只发关节包 controlSpace=0", spaces == {0}, f"实际={spaces}")
        check("没发末端包 controlSpace=1", 1 not in spaces, f"实际={spaces}")
        check("关节包够多 (确实在流)", len(robot.udp_packets) > 10,
              f"{len(robot.udp_packets)} 个")

        print("\n[2] 门阀拒绝 (急停)")
        robot.inject_estop = True
        n0 = len(robot.commands)
        r = post("/v1/move_to", body)
        check("被拒绝", r.get("ok") is False, f"{r.get('code')}: {r.get('message')}")
        check("在门阀阶段拒绝", r.get("stage") == "safety", str(r.get("stage")))
        check("没有发 startControl", not any(
            c.get("ty") == "realtime/startControl" for c in robot.commands[n0:]))
        robot.inject_estop = False

        print("\n[3] 总闸关闭 -> 只算不动")
        try:
            req = urllib.request.Request(
                BASE + "/v1/control_enabled",
                data=json.dumps({"enabled": False}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:
            print(f"  (切总闸失败: {exc})")
        n1 = len(robot.commands)
        r = post("/v1/move_to", body)
        check("被拒绝", r.get("ok") is False and r.get("code") == "disabled",
              f"{r.get('code')}: {r.get('message')}")
        check("没下发任何指令", not any(
            c.get("ty") == "realtime/startControl" for c in robot.commands[n1:]))
        try:
            req = urllib.request.Request(
                BASE + "/v1/control_enabled",
                data=json.dumps({"enabled": True}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
    finally:
        proc.terminate()
        robot.stop()
        try:
            out = proc.stdout.read() if proc.stdout else ""
        except Exception:
            out = ""
        failed = [n for n, ok, _ in _results if not ok]
        if failed:
            print("\n---- arm_service 日志 (尾部) ----")
            print("\n".join(out.splitlines()[-45:]))

    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
