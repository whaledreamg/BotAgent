#!/usr/bin/env python3
"""探针: 控制器到底认不认 UDP 报文里的**手部字段**?

**为什么要有这个探针**: 动作库里已经能写 ``type: "hand"`` 的片段
(``library.py`` 会解析 ``hand.leftHand`` / ``hand.rightHand``, 6 自由度
0~1000), 但**从来没有真正下发过** —— ``robot/streamer.py`` 的 UDP 报文
只有 ``value: [7 个 rad]``, 没有手部字段。

所以「闭合夹爪」这件事到底能不能做, 取决于控制器认哪种报文格式。
这个脚本负责**先把这件事问清楚**, 别猜。

怎么用::

    # 真机探测 (需要能连到控制器)
    conda run -n yolo python tools/probe_hand.py --controller 192.168.100.16 --arm right

    # 离线自检 (不起控制器, 只验证本地报文长什么样)
    conda run -n yolo python tools/probe_hand.py --offline

真机探测的做法: 起一个 UDP socket 收包, 往控制器的实时指令端口发两种
报文 (一种带手部字段, 一种不带), 然后查 ``realtime/getStatus`` 看控制器
有没有报 ``invalidDatagram``。

**不发任何关节指令** —— 探针只发报文不做动作, 但仍然要小心:
真机上发的报文会让手臂往那个关节角动!
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from robot.controller import ArmController  # noqa: E402
from robot.streamer import JointStreamer  # noqa: E402
from timeline.model import JOINT_COUNT  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

_results: List[tuple] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    _results.append((name, condition, detail))
    tag = PASS if condition else FAIL
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""))
    return condition


# ---------------------------------------------------------------- 报文格式


def joint_packet(robot: str, joints: List[float]) -> Dict[str, Any]:
    """不带手部字段的报文 —— 现在 arm_service 一直在发的那种。"""
    return {
        "robot": robot,
        "controlSpace": 0,
        "commandMode": 0,
        "value": list(joints[:JOINT_COUNT]),
    }


def hand_packet(
    robot: str,
    joints: List[float],
    hand: List[int],
) -> Dict[str, Any]:
    """带手部字段的报文 —— 探针要验证控制器认不认这种。

    **字段名是猜的** —— 这正是要探的东西。可能的写法有:
    - ``hand``          (一个数组)
    - ``leftHand`` / ``rightHand`` (按臂分)
    - ``value`` 后面跟 6 个值 (关节+手拼一起)

    这个脚本会把三种都试一遍, 看哪种被接受。
    """
    return {
        "robot": robot,
        "controlSpace": 0,
        "commandMode": 0,
        "value": list(joints[:JOINT_COUNT]),
        "hand": list(hand),
    }


def hand_packet_split(
    robot: str,
    joints: List[float],
    hand: List[int],
) -> Dict[str, Any]:
    """另一种猜法: ``leftHand`` / ``rightHand`` 分开写。"""
    return {
        "robot": robot,
        "controlSpace": 0,
        "commandMode": 0,
        "value": list(joints[:JOINT_COUNT]),
        "leftHand": list(hand),
        "rightHand": list(hand),
    }


def hand_packet_extended_value(
    robot: str,
    joints: List[float],
    hand: List[int],
) -> Dict[str, Any]:
    """第三种猜法: value 数组后面直接接 6 个手部值。"""
    return {
        "robot": robot,
        "controlSpace": 0,
        "commandMode": 0,
        "value": list(joints[:JOINT_COUNT]) + list(hand),
    }


# ---------------------------------------------------------------- 离线自检


def check_offline() -> None:
    """不连控制器 —— 只看本地生成的报文长什么样。

    至少要保证: **不带手部字段的报文跟现在完全一致** (别把现有行为
    改坏了), 带手部字段的三种写法都能序列化。
    """
    print("========== 离线: 报文格式 ==========")
    joints = [0.0] * JOINT_COUNT

    plain = joint_packet("RightArm", joints)
    check(
        "纯关节报文只有 4 个键",
        set(plain) == {"robot", "controlSpace", "commandMode", "value"},
        str(sorted(plain)),
    )
    check("value 长度等于关节数", len(plain["value"]) == JOINT_COUNT)

    for name, pkt in (
        ("hand", hand_packet("RightArm", joints, [1000] * 6)),
        ("leftHand/rightHand", hand_packet_split("RightArm", joints, [1000] * 6)),
        ("扩展 value", hand_packet_extended_value("RightArm", joints, [1000] * 6)),
    ):
        try:
            text = json.dumps(pkt, separators=(",", ":"))
            ok = True
        except (TypeError, ValueError) as exc:
            text = str(exc)
            ok = False
        check(f"{name} 变体能序列化", ok, text[:80])

    print()
    print("========== 离线: streamer 现状 ==========")
    # 看现在 streamer 实际发什么 —— 确认「没有手部字段」这个判断
    streamer = JointStreamer(host="127.0.0.1", port=9999, frequency=100)
    check("streamer 没有 hand 相关的方法", not hasattr(streamer, "set_hand"))

    print()
    print("离线自检只能证明「本地能生成这种报文」,")
    print("**证明不了控制器认不认** —— 那需要真机: --controller <ip>")


# ---------------------------------------------------------------- 真机探测


class _UdpSink:
    """收 UDP 报的 socket。"""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def probe_controller(controller_ip: str, arm: str, timeout: float) -> None:
    """连真控制器, 试各种手部字段写法。

    **注意**: 这一步会真的往手臂发关节指令 —— 那个关节角就是当前
    实测角 (先读 robot/status), 所以理论上不动, 但仍要确保手臂周围
    没人。
    """
    from robot.connection import ControllerConnection

    print(f"========== 连控制器 {controller_ip} ==========")
    conn = ControllerConnection(f"ws://{controller_ip}:9000")
    if not conn.connect():
        print(f"  [FAIL] 连不上控制器 {controller_ip}:9000")
        _results.append(("连上控制器", False, controller_ip))
        return
    check("连上控制器", True, controller_ip)

    try:
        # ---- 1. 读实测关节角 ----
        print("\n[1] 读实测关节角 (发的报文就用这个角, 手臂不会动)")
        resp = conn.request_sync("robot/list", {}, timeout=3.0)
        arms = (resp or {}).get("db", {}).get("robots") or []
        names = [str(a.get("name")) for a in arms if isinstance(a, dict)]
        check("拿到臂列表", bool(names), str(names))
        if not names:
            return

        arm_name = ""
        for n in names:
            low = n.lower()
            if arm in low or (arm == "right" and low.endswith("r")):
                arm_name = n
                break
        if not arm_name:
            arm_name = names[0]
        check("选定臂", True, arm_name)

        resp = conn.request_sync("robot/status", {"name": arm_name}, timeout=3.0)
        db = (resp or {}).get("db", {})
        joints = db.get("joints") or []
        check("拿到关节角", len(joints) >= JOINT_COUNT,
              f"{len(joints)} 个")
        if len(joints) < JOINT_COUNT:
            return
        actual = [float(v) for v in joints[:JOINT_COUNT]]

        # ---- 2. 查实时指令端口 ----
        print("\n[2] 查实时指令端口")
        resp = conn.request_sync("realtime/getStatus", {}, timeout=3.0)
        rt = (resp or {}).get("db", {})
        port = rt.get("commandPort")
        check("拿到 commandPort", isinstance(port, int), str(port))
        if not isinstance(port, int):
            return
        check("commandPortReady", rt.get("commandPortReady") is not False)

        # ---- 3. 试各种手部字段写法 ----
        print("\n[3] 试各种手部字段写法")
        print("    (每种发 5 包, 然后查控制器有没有报 invalidDatagram)")
        variants = [
            ("基准: 不带手部字段", joint_packet(arm_name, actual)),
            ("写法 A: hand 数组", hand_packet(arm_name, actual, [1000] * 6)),
            ("写法 B: leftHand/rightHand",
             hand_packet_split(arm_name, actual, [1000] * 6)),
            ("写法 C: value 扩展", hand_packet_extended_value(arm_name, actual, [1000] * 6)),
        ]

        results: Dict[str, Any] = {}
        for label, pkt in variants:
            before = _invalid_count(rt)
            _send_packets(port, pkt, count=5)
            time.sleep(0.3)
            resp = conn.request_sync("realtime/getStatus", {}, timeout=3.0)
            now = (resp or {}).get("db", {})
            after = _invalid_count(now)
            results[label] = {
                "invalid_before": before,
                "invalid_after": after,
                "rejected": after > before,
            }
            status = "被拒" if after > before else "未报错"
            print(f"    {label}: {status} (invalidDatagram {before} -> {after})")

        print()
        print("========== 结论 ==========")
        baseline = results["基准: 不带手部字段"]
        check(
            "基准报文 (不带手部) 不被拒",
            not baseline["rejected"],
            f"invalid {baseline['invalid_before']} -> {baseline['invalid_after']}",
        )

        accepted = [
            label for label, r in results.items()
            if label != "基准: 不带手部字段" and not r["rejected"]
        ]
        rejected = [
            label for label, r in results.items()
            if label != "基准: 不带手部字段" and r["rejected"]
        ]

        if accepted:
            print(f"  控制器接受的手部写法: {accepted}")
            for label in accepted:
                check(f"{label} 未被拒", True)
        else:
            print("  **所有带手部字段的写法都被拒了**")
            for label in rejected:
                check(f"{label} 被拒", True, "控制器不认这种格式")

        if rejected and not accepted:
            print()
            print("  => 结论: 控制器不认手部字段。")
            print("     「闭合夹爪」做不了 —— 走「不闭夹爪」方案, 其余 4 步照做。")
        elif accepted:
            print()
            print("  => 结论: 可以做。用上面被接受的那种写法。")
    finally:
        conn.disconnect()


def _invalid_count(status_db: Dict[str, Any]) -> int:
    """数一下状态里有几个 invalidDatagram。"""
    count = 0
    for key in ("invalidDatagram", "invalidDatagrams", "invalid"):
        v = status_db.get(key)
        if isinstance(v, bool):
            count += int(v)
        elif isinstance(v, int):
            count += v
        elif isinstance(v, list):
            count += len(v)
    return count


def _send_packets(port: int, packet: Dict[str, Any], count: int = 5) -> None:
    """往实时指令端口发几包。"""
    payload = json.dumps(packet, separators=(",", ":")).encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _ in range(count):
            sock.sendto(payload, ("192.168.100.16", port))
            time.sleep(0.02)
    except OSError as exc:
        print(f"    发送失败: {exc}")
    finally:
        sock.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="探测控制器是否认手部字段")
    ap.add_argument("--controller", help="控制器 IP (如 192.168.100.16)")
    ap.add_argument("--arm", default="right", help="用哪条臂 (left/right)")
    ap.add_argument("--offline", action="store_true", help="只做离线自检")
    ap.add_argument("--timeout", type=float, default=5.0)
    args = ap.parse_args()

    if args.offline or not args.controller:
        check_offline()
    else:
        probe_controller(args.controller, args.arm, args.timeout)

    print()
    failed = [r for r in _results if not r[1]]
    print(f"共 {len(_results)} 项, 失败 {len(failed)} 项")
    for name, _ok, detail in failed:
        print(f"  [FAIL] {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
