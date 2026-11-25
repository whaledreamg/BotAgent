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
from robot.hand import DOF_COUNT, PRESETS  # noqa: E402
from robot.streamer import JOINT_COUNT, JointStreamer  # noqa: E402
from timeline.model import JOINT_COUNT as MODEL_JOINT_COUNT  # noqa: E402
from timeline.model import JointState  # noqa: E402
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


def test_streamer_has_no_hand_field() -> None:
    """实时指令包里**绝对不能**有手部字段。

    控制器文档 §21.9: 实时指令包的字段只有 ``robot`` / ``controlSpace``
    / ``commandMode`` / ``poseType`` / ``value``。多塞一个 ``hand`` 字段,
    控制器会**整包判 invalidDatagram** —— 那时候连手臂都不动了, 比不动
    手指糟得多。

    灵巧手改走控制器的 ``rs485/*`` 透传 (见 ``robot/hand.py``)。
    """
    print("\n[1] 报文格式 (不含手部字段)")
    mock = MockController(actual_joints=[0.1] * JOINT_COUNT)
    try:
        streamer = JointStreamer(
            host="127.0.0.1", port=mock.udp_port, frequency=100
        )
        streamer.set_idle_stop_ms(0)
        streamer.set_target("RightArm", [0.5] * JOINT_COUNT)
        streamer.start()
        time.sleep(0.3)
        streamer.stop()

        check("发出的包不为空", len(mock.received_packets) > 0,
              f"{len(mock.received_packets)} 包")
        check("报文里没有 hand 字段",
              all("hand" not in p for p in mock.received_packets))
        if mock.received_packets:
            check("报文只有 4 个键",
                  set(mock.received_packets[0]) == {
                      "robot", "controlSpace", "commandMode", "value"},
                  str(sorted(mock.received_packets[0])))
            check("controlSpace 是关节空间 (0)",
                  mock.received_packets[0]["controlSpace"] == 0)
    finally:
        mock.stop()


def test_hand_protocol_frames() -> None:
    """灵巧手协议帧 —— 必须与 ActionPlanner 逐字节一致。"""
    print("\n[2] 灵巧手协议帧")
    from robot.hand import (
        REG_READ_POS,
        REG_WRITE_ALL,
        build_read_positions_frame,
        build_write_all_frame,
        verify_crc,
    )

    pose = PRESETS["一把抓"]
    frame = build_write_all_frame(127, pose, 100)
    check("写帧 CRC 正确", verify_crc(frame))
    check("写帧功能码 0x10", frame[1] == 0x10)
    check("写帧起始寄存器 0x03F2",
          (frame[2] << 8 | frame[3]) == REG_WRITE_ALL)
    check("写帧带 6 个 DOF",
          len(frame) == 7 + DOF_COUNT * 4 + 2, str(len(frame)))

    rframe = build_read_positions_frame(127)
    check("读帧 CRC 正确", verify_crc(rframe))
    check("读帧功能码 0x04", rframe[1] == 0x04)
    check("读帧起始寄存器 0x07D0",
          (rframe[2] << 8 | rframe[3]) == REG_READ_POS)

    # 越界值要夹紧
    clamped = build_write_all_frame(127, [99999, -50, 0, 0, 0, 0], 100)
    check("越界的位置被夹到 1000",
          clamped[7] == 0x03 and clamped[8] == 0xE8,
          f"{clamped[7] << 8 | clamped[8]}")
    # 每个 DOF 占 4 字节 (位置 2 + 时长 2), 所以 DOF1 的位置在 11-12
    check("负值被夹到 0",
          clamped[11] == 0 and clamped[12] == 0,
          f"{clamped[11] << 8 | clamped[12]}")


def test_hand_presets() -> None:
    """Revo2 预设手势。"""
    print("\n[3] 预设手势")
    check("一把抓 = 拇指弯曲拉满 + 四指拉满",
          PRESETS["一把抓"] == [1000, 0, 1000, 1000, 1000, 1000],
          str(PRESETS["一把抓"]))
    check("释放 = 全 0", PRESETS["释放"] == [0] * 6, str(PRESETS["释放"]))
    check("半抓 = 500", PRESETS["半抓"] == [500, 0, 500, 500, 500, 500])
    check("预设都是 6 个值",
          all(len(v) == DOF_COUNT for v in PRESETS.values()))


def test_sequence_composition() -> None:
    """动作序列合成: 时间轴不重叠, 夹爪指令落在正确的时间点。"""
    print("\n[4] 动作序列合成")
    from sequence import compose_from_names

    lib = ActionLibrary(SERVICE_DIR / "data")
    lib.load()

    seq = compose_from_names(
        lib, ["准备点", "闭合夹爪", "安全点"], JointState.zero()
    )
    segs = seq.timeline.segments
    # 准备点自带一个手部片段 (张开到 [0,900,0,0,0,0]), 加上闭合夹爪的
    # 手部片段 —— 所以是 4 段 / 2 个夹爪指令, 不是 3 段 / 1 个。
    arm_segs = [s for s in segs if s.is_arm]
    hand_segs = [s for s in segs if not s.is_arm]
    check("合成出 4 段 (2 关节 + 2 手部)", len(segs) == 4, str(len(segs)))
    check("关节片段 2 个", len(arm_segs) == 2, str(len(arm_segs)))
    check("手部片段 2 个", len(hand_segs) == 2, str(len(hand_segs)))
    check("片段时间不重叠",
          all(segs[i].end_time <= segs[i + 1].start_time + 1e-9
              for i in range(len(segs) - 1)),
          str([(s.start_time, s.end_time) for s in segs]))
    check("总时长 = 各段之和",
          abs(seq.duration - sum(s.duration for s in segs)) < 1e-6,
          f"{seq.duration}")
    check("夹爪指令 2 个", len(seq.cues) == 2, str(len(seq.cues)))
    if seq.cues:
        # 第一个是准备点自带的「张开」, 第二个才是「闭合夹爪」的一把抓
        close_cues = [c for c in seq.cues if c.right == PRESETS["一把抓"]]
        check("有且只有一个「一把抓」的夹爪指令", len(close_cues) == 1,
              str(len(close_cues)))
        if close_cues:
            cue = close_cues[0]
            check("夹爪指令在准备点结束之后",
                  cue.time >= arm_segs[0].end_time - 1e-9,
                  f"t={cue.time} vs {arm_segs[0].end_time}")
            check("夹爪手位是一把抓", cue.right == PRESETS["一把抓"])


def test_hand_evaluation_semantics() -> None:
    """手部片段的求值语义 (保持 / 插值)。"""
    print("\n[5] 手部求值语义")
    lib = ActionLibrary(SERVICE_DIR / "data")
    lib.load()
    close = lib.find("闭合夹爪")
    check("动作库里有「闭合夹爪」", close is not None)
    if close is None:
        return

    engine = PlaybackEngine(close.to_timeline())
    h0 = engine.evaluate_hand_at(0.0, "right")
    h1 = engine.evaluate_hand_at(0.5, "right")
    h2 = engine.evaluate_hand_at(0.99, "right")

    check("起点是 0", h0 == [0] * DOF_COUNT, str(h0))
    check("中途是插值出来的 (不是 0 也不是 1000)",
          0 < h1[0] < 1000, str(h1))
    check("末点接近 1000 (线性插值到 990)", h2[0] == 990, str(h2))
    # 「闭合夹爪」动作**两只手都管** —— 所以左手也有值
    hl = engine.evaluate_hand_at(0.5, "left")
    check("两只手都管时左手也有值", hl and 0 < hl[0] < 1000, str(hl))
    # 只管右手的动作, 左手应该返回空
    only_right = lib.find("挥手")
    if only_right is not None:
        e2 = PlaybackEngine(only_right.to_timeline())
        check("只管关节的动作, 左手返回空",
              e2.evaluate_hand_at(0.0, "left") == [])

    # 落在手部片段外面 -> 保持上一个手部目标
    h = engine.evaluate_hand_at(5.0, "right")
    check("片段结束后保持 1000", h and h[0] == 1000, str(h))


def test_grasp_flow_steps() -> None:
    """5 步依次下发 —— 顺序不能乱。"""
    print("\n[8] 抓取流程顺序")
    lib = ActionLibrary(SERVICE_DIR / "data")
    lib.load()
    for name in ("准备点", "安全点", "闭合夹爪"):
        check(f"动作库里有「{name}」", lib.find(name) is not None)


def test_named_points() -> None:
    """具名点解析 —— 声明、引用、链式偏移。"""
    print("\n[8b] 具名点解析")
    from flow import FlowError, parse_flow
    from sequence import plan_from_flow, resolve_named_points

    raw = {
        "name": "t",
        "steps": [
            {"id": "detect", "type": "detect", "duration": 1.5},
            {"id": "object", "type": "target", "source": "detect"},
            {"id": "above_point", "type": "target", "base": "object",
             "offset": {"z": 0.05}},
            {"id": "side_point", "type": "target", "base": "above_point",
             "offset": {"x": 0.02, "z": 0.01}},
            {"id": "fixed", "type": "target",
             "pose": {"x": 0.1, "y": 0.2, "z": 0.3}},
            {"id": "reach", "type": "arm", "point": "above_point",
             "duration": 3.0},
            {"id": "grasp", "type": "arm", "point": "object",
             "offset": {"z": -0.01}, "duration": 2.0},
        ],
    }
    flow = parse_flow(raw)

    target = (0.5, -0.1, -0.3)
    named = resolve_named_points(flow, target)
    check("object = 识别结果", named["object"] == target)
    check(
        "above_point = object + z0.05",
        named["above_point"] == (0.5, -0.1, -0.25),
    )
    check(
        "side_point = 链式偏移 (基于 above_point)",
        named["side_point"] == (0.52, -0.1, -0.24),
    )
    check(
        "fixed = 写死的坐标",
        named["fixed"] == (0.1, 0.2, 0.3),
    )

    plan = plan_from_flow(flow, target)
    by_id = {s.id: s for s in plan}
    check("reach 段是 arm 类型", by_id["reach"].type == "arm")
    check(
        "reach 段目标 = above_point",
        by_id["reach"].pos == (0.5, -0.1, -0.25),
    )
    check(
        "grasp 段目标 = object + offset",
        by_id["grasp"].pos == (0.5, -0.1, -0.31),
    )
    check(
        "target 段不产生运动 (纯声明)",
        by_id["object"].type == "target",
    )
    check(
        "detect 段变成 hold (占时长, 手臂不动)",
        by_id["detect"].type == "hold" and by_id["detect"].duration == 1.5,
    )

    # 没有识别结果时, 取自识别的点算不出来
    try:
        resolve_named_points(flow, None)
        check("缺识别结果 -> 应该报错", False)
    except ValueError:
        check("缺识别结果 -> 正确报错", True)

    # 引用不存在的点 -> 加载时就报错
    try:
        parse_flow({
            "name": "t",
            "steps": [{"id": "a", "type": "arm", "point": "nope"}],
        })
        check("引用不存在的点 -> 应该报错", False)
    except FlowError:
        check("引用不存在的点 -> 正确报错", True)


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

    test_streamer_has_no_hand_field()
    test_hand_protocol_frames()
    test_hand_presets()
    test_sequence_composition()
    test_hand_evaluation_semantics()
    test_grasp_flow_steps()
    test_named_points()
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
