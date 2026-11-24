#!/usr/bin/env python3
"""端到端验证: 用历史瓶子的 xyz 跑完整抓取流程的移动步骤。

**不传 a/b/c** —— 让服务用 ``configs/arm.yaml`` 里的抓取姿态,
这才验证了配置真的生效。

流程: 准备点 -> above -> grasp -> 闭夹爪 -> 安全点
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8802"

TARGETS = [
    {"ts": "17:15:55", "xyz": (0.4392, -0.1203, -0.3186),
     "last": "failed:unreachable"},
    {"ts": "16:53:21", "xyz": (0.5013, -0.0986, -0.3887),
     "last": "failed:unreachable"},
]


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode() or "{}")
        except Exception:
            return {}
    except Exception as exc:
        return {"ok": False, "code": "error", "message": str(exc)}


def get(path: str) -> dict:
    try:
        with urllib.request.urlopen(BASE + path, timeout=15) as resp:
            return json.load(resp)
    except Exception as exc:
        return {"error": str(exc)}


def tcp() -> dict:
    s = get("/v1/kinematics?refresh=true")
    return ((s.get("arms") or {}).get("RightArm") or {}).get("cartesian") or {}


def show(tag: str, r: dict) -> bool:
    ik = r.get("ik") or {}
    ok = bool(r.get("ok"))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {tag}")
    print(f"    code={r.get('code')}  位置误差={r.get('error_m')} m  "
          f"姿态误差={r.get('error_rot_rad')} rad "
          f"({math.degrees(r.get('error_rot_rad') or 0):.3f} deg)")
    if ik:
        print(f"    逆解: {ik.get('err_pos_mm')}mm / {ik.get('err_rot_deg')}deg  "
              f"iters={ik.get('iters')}  "
              f"position_only={ik.get('position_only', False)}")
    if r.get("message"):
        print(f"    {r['message']}")
    return ok


def main() -> int:
    all_ok = True

    print("=" * 74)
    print("第 1 步: 播准备点")
    print("=" * 74)
    r = post("/v1/play", {"actions": ["准备点"], "speak": False})
    print(f"  ok={r.get('ok')} {r.get('message', '')}")
    time.sleep(7)
    c = tcp()
    print(f"  TCP = ({c.get('x',0):+.4f}, {c.get('y',0):+.4f}, {c.get('z',0):+.4f})  "
          f"abc = ({c.get('a',0):+.4f}, {c.get('b',0):+.4f}, {c.get('c',0):+.4f})")
    print()

    for t in TARGETS:
        x, y, z = t["xyz"]
        print("=" * 74)
        print(f"瓶子 @ {t['ts']}  ({x:+.4f}, {y:+.4f}, {z:+.4f})")
        print(f"上次结果: {t['last']}")
        print("=" * 74)

        # 第 3 步: above
        r = post("/v1/move_to", {"arm": "right", "x": x, "y": y, "z": z + 0.05,
                                 "duration": 3.0, "speak": False})
        all_ok &= show(f"第3步 above ({x:+.4f}, {y:+.4f}, {z+0.05:+.4f})", r)
        print()

        # 第 4 步: grasp
        r = post("/v1/move_to", {"arm": "right", "x": x, "y": y, "z": z,
                                 "duration": 2.0, "speak": False})
        all_ok &= show(f"第4步 grasp ({x:+.4f}, {y:+.4f}, {z:+.4f})", r)
        print()

        # 第 4 步后半: 闭夹爪
        r = post("/v1/play", {"actions": ["闭合夹爪"], "speak": False})
        ok = bool(r.get("ok"))
        all_ok &= ok
        print(f"  [{'OK  ' if ok else 'FAIL'}] 第4步 闭夹爪  {r.get('message','')}")
        time.sleep(4)
        print()

        # 第 5 步: 安全点
        r = post("/v1/play", {"actions": ["安全点"], "speak": False})
        ok = bool(r.get("ok"))
        all_ok &= ok
        print(f"  [{'OK  ' if ok else 'FAIL'}] 第5步 安全点  {r.get('message','')}")
        time.sleep(5)
        c = tcp()
        print(f"       TCP = ({c.get('x',0):+.4f}, {c.get('y',0):+.4f}, "
              f"{c.get('z',0):+.4f})")
        print()

    print("=" * 74)
    print("结果: " + ("全部成功 ✓" if all_ok else "有失败 ✗"))
    print("=" * 74)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
