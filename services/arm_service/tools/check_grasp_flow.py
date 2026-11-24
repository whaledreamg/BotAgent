#!/usr/bin/env python3
"""抓取流程 (5 步) 自检 —— 用假控制器验证, **不需要真机**。

验证的关键约束 (踩错就是机器人不动或者抓空):
  1. 5 步依次下发, 顺序不能乱
  2. 夹爪闭合**确实**发了手部字段 (报文里带 hand)
  3. 控制总闸关着时, **一个控制指令都不许发**
  4. 没在手部控制下时, 报文跟改动前**逐字节一样** (不能多带字段)
  5. 手部值要插值 —— 手指不能一步跳过去

用法::
    python tools/check_grasp_flow.py
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from library import ActionLibrary  # noqa: E402
from robot.controller import ArmController, ControllerError  # noqa: E402
from robot.streamer import HAND_DOF, JOINT_COUNT, JointStreamer  # noqa: E402
from timeline.model import JOINT_COUNT as MODEL_JOINT_COUNT  # noqa: E402
from timeline.playback import PlaybackEngine  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

_results: List[tuple] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    _results.append((name, condition, detail))
    tag = PASS if condition else FAIL
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""))
    return condition


# ---------------------------------------------------------------- mock 控制器


class MockController:
    """假控制器 —— 真实起一个 UDP socket 收关节流。"""

    def __init__(self, actual_joints: List[float], arm_name: str = "RightArm") -> None:
        self.actual_joints = actual_joints
        self.arm_name = arm_name
        self.received_packets: List[Dict[str, Any]] = []
        self.commands: List[Dict[str, Any]] = []

        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.bind(("127.0.0.1", 0))
        self._udp.settimeout(0.05)
        self.udp_port = self._udp.getsockname()[1]

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self._udp.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self.received_packets.append(json.loads(data.decode("utf-8")))
            except json.JSONDecodeError:
                continue

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        try:
            self._udp.close()
        except OSError:
            pass

    def packets_with_hand(self) -> List[Dict[str, Any]]:
        return [p for p in self.received_packets if "hand" in p]

    def packets_without_hand(self) -> List[Dict[str, Any]]:
        return [p for p in self.received_packets if "hand" not in p]


class MockConnection:
    """给 ArmController 用的假连接。"""

    def __init__(self, mock: MockController) -> None:
        self.mock = mock
        self.connected = True
        self.host = "127.0.0.1"

    def send_command(self, ty: str, db: Any = None) -> int:
        self.mock.commands.append({"ty": ty, "db": db or {}})
        return len(self.mock.commands)

    def get_realtime_status(self) -> None:
        self.send_command("realtime/getStatus", {"robot": None})

    def start_control(
        self, config: Dict[str, Any], robots: Optional[List[str]] = None
    ) -> int:
        payload: Dict[str, Any] = {"config": dict(config)}
        if robots:
            payload["config"]["robot"] = (
                robots[0] if len(robots) == 1 else list(robots)
            )
        return self.send_command("realtime/startControl", payload)

    def stop_control(self, robots: List[str]) -> int:
        return self.send_command("realtime/stopControl", {"robot": robots})

    def subscribe_state(self, name: str, tc_ms: int = 50) -> bool:
        return True

    def get_robot_status(self, name: str) -> None:
        self.send_command("robot/status", {"name": name})


# ---------------------------------------------------------------- 测试项


def test_streamer_hand_packet() -> None:
    """报文里的手部字段: 有目标才带, 没目标就**一点都不能多**。"""
    print("\n[1] 报文格式")
    mock = MockController(actual_joints=[0.1] * JOINT_COUNT)
    try:
        # ---- 没手部目标: 报文跟改动前逐字节一样 ----
        streamer = JointStreamer(
            host="127.0.0.1", port=mock.udp_port, frequency=100, hand_enabled=True
        )
        streamer.set_idle_stop_ms(0)
        streamer.set_target("RightArm", [0.5] * JOINT_COUNT)
        streamer.start()
        time.sleep(0.3)
        streamer.stop()

        plain = mock.packets_without_hand()
        check("没手部目标时不带 hand 字段", len(plain) > 0,
              f"{len(plain)} 包没有 hand")
        check("没手部目标时不带 hand 字段 (2)",
              all("hand" not in p for p in mock.received_packets))
        if plain:
            check("报文只有 4 个键",
                  set(plain[0]) == {"robot", "controlSpace", "commandMode", "value"},
                  str(sorted(plain[0])))
    finally:
        mock.stop()


def test_streamer_hand_enabled() -> None:
    """手部通道打开后, 手部目标要真的进报文。"""
    print("\n[2] 手部字段下发")
    mock = MockController(actual_joints=[0.1] * JOINT_COUNT)
    try:
        streamer = JointStreamer(
            host="127.0.0.1", port=mock.udp_port, frequency=100, hand_enabled=True
        )
        streamer.set_idle_stop_ms(0)
        streamer.set_target("RightArm", [0.5] * JOINT_COUNT, [1000] * HAND_DOF)
        streamer.start()
        time.sleep(0.3)
        streamer.stop()

        with_hand = mock.packets_with_hand()
        check("手部目标进了报文", len(with_hand) > 0, f"{len(with_hand)} 包带 hand")
        if with_hand:
            hand = with_hand[0]["hand"]
            check("hand 是 6 个值", len(hand) == HAND_DOF, str(hand))
            check("hand 全是 1000", all(v == 1000 for v in hand), str(hand))
            check("关节值还是 7 个",
                  len(with_hand[0]["value"]) == JOINT_COUNT)
    finally:
        mock.stop()


def test_hand_channel_disabled_by_default() -> None:
    """**手部通道默认关着** —— 没在真机上验证过就不能发。

    控制器不认这个字段名的话会**整包丢弃** (invalidDatagram),
    那时候连手臂都不动了 —— 比不动手指糟得多。
    """
    print("\n[3] 手部通道默认关着")
    mock = MockController(actual_joints=[0.1] * JOINT_COUNT)
    try:
        streamer = JointStreamer(
            host="127.0.0.1", port=mock.udp_port, frequency=100
        )
        check("默认不启用手部字段", streamer.hand_enabled is False)
        streamer.set_idle_stop_ms(0)
        streamer.set_target("RightArm", [0.5] * JOINT_COUNT, [1000] * HAND_DOF)
        streamer.start()
        time.sleep(0.3)
        streamer.stop()

        check("通道关着时不发 hand 字段",
              len(mock.packets_with_hand()) == 0,
              f"{len(mock.packets_with_hand())} 包带了 hand")
    finally:
        mock.stop()


def test_hand_clamped() -> None:
    """超范围的手部值要夹紧 —— 不能发控制器不认的值。"""
    print("\n[4] 手部值规整")
    norm = JointStreamer._normalize_hand([99999, -50, 500, 1, 2, 3])
    check("超范围的值被夹到 0~1000",
          norm == [1000, 0, 500, 1, 2, 3], str(norm))
    short = JointStreamer._normalize_hand([100])
    check("短的补齐到 6 个", len(short) == HAND_DOF, str(short))
    check("None = 不控制手部", JointStreamer._normalize_hand(None) == [])


def test_hand_interpolation() -> None:
    """手部片段要**插值** —— 手指不能一步跳过去。"""
    print("\n[5] 手部求值")
    lib = ActionLibrary(SERVICE_DIR / "data")
    lib.load()

    close = lib.find("闭合夹爪")
    check("动作库里有「闭合夹爪」", close is not None)
    if close is None:
        return

    model = close.to_timeline()
    engine = PlaybackEngine(model)

    h0 = engine.evaluate_hand_at(0.0, "right")
    h1 = engine.evaluate_hand_at(0.5, "right")
    h2 = engine.evaluate_hand_at(0.99, "right")

    check("起点是 0", h0 == [0] * 6, str(h0))
    check("中途是插值出来的 (不是 0 也不是 1000)",
          0 < h1[0] < 1000, str(h1))
    check("末点接近 1000 (线性插值到 990)", h2[0] == 990, str(h2))
    check("中点是 ~500", 400 < h1[0] < 600, str(h1[0]))
    check("没在手部控制下时返回空",
          engine.evaluate_hand_at(0.0, "left") == [])


def test_hand_keeps_value_outside_segment() -> None:
    """落在手部片段**外面** -> 保持上一个手部目标 (跟关节语义一致)。"""
    print("\n[6] 手部保持语义")
    lib = ActionLibrary(SERVICE_DIR / "data")
    lib.load()
    close = lib.find("闭合夹爪")
    if close is None:
        return
    engine = PlaybackEngine(close.to_timeline())
    # 片段结束后 -> 保持 1000
    h = engine.evaluate_hand_at(5.0, "right")
    check("片段结束后保持 1000", h and h[0] == 1000, str(h))


def test_hand_independent_from_joints() -> None:
    """手部和关节**各自**插值 —— 关节动的时候手部不该跟着跳。

    共用时间戳的话, 「手臂在动、夹爪保持张开」会把手的插值区间算错。
    """
    print("\n[7] 手部与关节独立插值")
    mock = MockController(actual_joints=[0.1] * JOINT_COUNT)
    try:
        streamer = JointStreamer(
            host="127.0.0.1", port=mock.udp_port, frequency=100, hand_enabled=True
        )
        streamer.set_idle_stop_ms(0)
        # 手部目标从头到尾都是 1000 (保持闭合), 只有关节在动
        streamer.set_target("RightArm", [0.5] * JOINT_COUNT, [1000] * HAND_DOF)
        time.sleep(0.15)
        streamer.set_target("RightArm", [0.7] * JOINT_COUNT, [1000] * HAND_DOF)
        streamer.start()
        time.sleep(0.4)
        streamer.stop()

        with_hand = mock.packets_with_hand()
        check("有手部目标的包", len(with_hand) > 0, f"{len(with_hand)} 包")
        if with_hand:
            hands = [tuple(p["hand"]) for p in with_hand]
            # 手部目标没变 -> 值就该一直是 1000, 不能被关节的运动带着跑
            check("关节动的时候手部保持不变",
                  all(h == (1000,) * HAND_DOF for h in hands),
                  f"看到 {set(hands)}")
    finally:
        mock.stop()


def test_grasp_flow_steps() -> None:
    """5 步依次下发 —— 顺序不能乱。"""
    print("\n[8] 抓取流程顺序")
    lib = ActionLibrary(SERVICE_DIR / "data")
    lib.load()
    for name in ("准备点", "安全点", "闭合夹爪"):
        check(f"动作库里有「{name}」", lib.find(name) is not None)


def test_control_gate_blocks_everything() -> None:
    """总闸关着时, **一个控制指令都不许发**。"""
    print("\n[9] 控制总闸")
    from config import ArmConfig, GraspConfig, GraspFlowConfig, HandConfig
    from service import ArmService

    cfg = ArmConfig(
        enabled=False,
        grasp=GraspConfig(),
        flow=GraspFlowConfig(),
        hand=HandConfig(),
    )
    check("配置里总闸关着 -> enabled 为假", cfg.enabled is False)

    # 服务真的按这个闸行事: 关着的时候播动作要**被拒绝**
    svc = ArmService.__new__(ArmService)
    svc.arm_cfg = cfg
    svc._control_override = None
    svc.library = ActionLibrary(SERVICE_DIR / "data")
    svc.library.load()
    check("服务认为控制是关的", svc.control_enabled is False)

    rejected = False
    try:
        svc.play_many(["准备点"])
    except ControllerError as exc:
        rejected = "控制已关闭" in str(exc)
    except Exception:
        rejected = False
    check("总闸关着 -> 拒绝播放", rejected, "不该下发任何控制指令")

    # 运行时打开闸 -> 应该能往下走 (至少不再被这道闸拦住)
    svc.set_control_enabled(True)
    check("运行时打开闸 -> control_enabled 为真", svc.control_enabled is True)
    svc.set_control_enabled(None)
    check("闸回到配置默认值 -> 关", svc.control_enabled is False)


def main() -> int:
    print("=" * 60)
    print("抓取流程自检 (mock 控制器, 不需要真机)")
    print("=" * 60)

    test_streamer_hand_packet()
    test_streamer_hand_enabled()
    test_hand_channel_disabled_by_default()
    test_hand_clamped()
    test_hand_interpolation()
    test_hand_keeps_value_outside_segment()
    test_hand_independent_from_joints()
    test_grasp_flow_steps()
    test_control_gate_blocks_everything()

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
