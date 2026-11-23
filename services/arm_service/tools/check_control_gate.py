#!/usr/bin/env python3
"""控制指令总闸 (``arm.enabled``): 关着就一个控制指令都不下发。

用 mock 控制器验证三件事:
  1. 总闸关着: move_to 只算位置, 不入控、不发流, 返回 code="disabled"
  2. 总闸开着: move_to 正常下发指令 (确认闸确实管用, 不是永远拒绝)
  3. 动作库播放也走同一条闸: 关着时拒绝播放
  4. /health 暴露 control_enabled, 运行时能切

用法::
    python tools/check_control_gate.py
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
from typing import Any, Dict

TOOLS_DIR = Path(__file__).resolve().parent
SERVICE_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(SERVICE_DIR))

from check_arm_service import MockRobotServer, check, _results  # noqa: E402
from check_move_to import CartesianMock  # noqa: E402

BASE = "http://127.0.0.1:8894"
MOVE = {"arm": "left", "x": 0.40, "y": 0.20, "z": 0.10, "duration": 1.0, "speak": False}


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


def get(path: str) -> Dict[str, Any]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode() or "{}")


def main() -> int:
    robot = CartesianMock()
    ws_port = robot.start_ws()
    # ARM_ENABLED=false: 启动就是「只算不动」
    env = dict(os.environ, ARM_CONTROLLER_WS=f"ws://127.0.0.1:{ws_port}", ARM_SERVER_PORT="8894",
               ARM_ENABLED="false", ARM_ACTION_DIR=str(SERVICE_DIR / "data"))
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

        print("\n[1] 总闸关着 (启动即 false)")
        h = get("/health")
        check("/health 说控制是关的", h.get("control_enabled") is False, str(h.get("control_enabled")))

        g = get("/v1/control_enabled")
        check("查总闸接口", g.get("enabled") is False, str(g))

        print("\n[2] 总闸关着: move_to 只算位置, 不下发指令")
        n_cmd = len(robot.commands)
        n_udp = len(robot.udp_packets)
        r = post("/v1/move_to", MOVE)
        check("被拒绝", r.get("ok") is False, f"{r.get('code')}: {r.get('message')}")
        check("code=disabled", r.get("code") == "disabled", str(r.get("code")))
        check("stage=gate", r.get("stage") == "gate", str(r.get("stage")))
        check("没有发 startControl", not any(
            c.get("ty") == "realtime/startControl" for c in robot.commands[n_cmd:]))
        check("没有发末端包", not any(
            p.get("controlSpace") == 1 for p in robot.udp_packets[n_udp:]))
        check("TCP 没动", all(abs(a - b) < 1e-6 for a, b in zip(robot.tcp, (0.30, 0.20, 0.10))),
              str([round(v, 3) for v in robot.tcp]))

        print("\n[3] 总闸关着: 动作库播放也拒绝")
        actions = get("/v1/actions")
        names = [a.get("key") for a in actions.get("actions", [])]
        check("动作库非空", len(names) > 0, str(names[:3]))
        if names:
            n_cmd = len(robot.commands)
            r = post("/v1/play", {"action": names[0]})
            # 拒绝走 HTTP 409 + detail (与其它 ControllerError 一致)
            rejected = r.get("ok") is False or "控制已关闭" in str(r.get("detail", ""))
            check("播放被拒绝", rejected, str(r)[:120])
            check("没有发 startControl", not any(
                c.get("ty") == "realtime/startControl" for c in robot.commands[n_cmd:]))

        print("\n[4] 运行时打开总闸 -> 能正常下发")
        r = post("/v1/control_enabled", {"enabled": True})
        check("切到开启", r.get("enabled") is True, str(r))
        h = get("/health")
        check("/health 同步更新", h.get("control_enabled") is True, str(h.get("control_enabled")))

        r = post("/v1/move_to", MOVE)
        check("这次成功了", r.get("ok") is True, f"{r.get('code')}: {r.get('message')}")
        check("发了 startControl", any(
            c.get("ty") == "realtime/startControl" for c in robot.commands))
        check("TCP 到位", all(abs(a - b) < 0.01 for a, b in zip(robot.tcp, (0.40, 0.20, 0.10))),
              str([round(v, 3) for v in robot.tcp]))

        print("\n[5] 再关回去 -> 又不下发指令")
        post("/v1/control_enabled", {"enabled": False})
        n_cmd = len(robot.commands)
        n_udp = len(robot.udp_packets)
        r = post("/v1/move_to", MOVE)
        check("被拒绝", r.get("ok") is False and r.get("code") == "disabled", str(r.get("code")))
        check("没有发 startControl", not any(
            c.get("ty") == "realtime/startControl" for c in robot.commands[n_cmd:]))
        check("没有发末端包", not any(
            p.get("controlSpace") == 1 for p in robot.udp_packets[n_udp:]))

        print("\n[6] 回到配置默认值")
        r = post("/v1/control_enabled", {"enabled": None})
        check("回到配置的 false", r.get("enabled") is False, str(r))
        n_cmd = len(robot.commands)
        post("/v1/move_to", MOVE)
        check("依然不下发指令", not any(
            c.get("ty") == "realtime/startControl" for c in robot.commands[n_cmd:]))
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
