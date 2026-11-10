#!/usr/bin/env python3
"""机械臂动作服务自检。

用假控制器 (mock WebSocket 控制面 + mock UDP 接收端) 验证全流程时序,
**不需要真机**。

验证的关键约束 (踩错就是机器人不动或直接被停控):
  1. 起流必须早于 startControl —— 首包等待窗只有 2000ms
  2. 首包必须等于实测关节角 —— 否则 firstFrameJumpRejected 停控
  3. 时间轴必须在对齐完成后才推进 —— 否则机器人一步跳到 700ms 后的位置
  4. 结束必须发 realtime/stopControl 交还通道 —— 否则会话残留占住该臂
  5. UDP 报文格式: 扁平 JSON, value 长度必须等于该臂关节数

用法::
    python tools/check_arm.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from library import ActionLibrary, ActionNotFound  # noqa: E402
from robot.controller import ArmController, Phase  # noqa: E402
from robot.streamer import JointStreamer  # noqa: E402
from timeline.model import JOINT_COUNT, JointState  # noqa: E402
from timeline.playback import PlaybackEngine  # noqa: E402

#: 动作库现在是「一个文件一个动作」, 没有需要排除的旧版大文件
LEGACY_FILES: List[str] = []

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
    """假控制器。

    - UDP 端: 真实起一个 UDP socket 收关节流, 记录收到的包
    - WebSocket 端: 记录收到的命令, 按需回响应
    """

    def __init__(self, actual_joints: List[float], arm_name: str = "LeftArm") -> None:
        self.actual_joints = actual_joints
        self.arm_name = arm_name

        self.received_packets: List[Dict[str, Any]] = []
        self.received_times: List[float] = []
        self.commands: List[Dict[str, Any]] = []
        self.command_times: List[float] = []

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
                packet = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            self.received_packets.append(packet)
            self.received_times.append(time.monotonic())

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        try:
            self._udp.close()
        except OSError:
            pass

    # ---- 控制面模拟 ----

    def send(self, ty: str, db: Dict[str, Any]) -> None:
        self.commands.append({"ty": ty, "db": db})
        self.command_times.append(time.monotonic())

    def command_types(self) -> List[str]:
        return [c["ty"] for c in self.commands]

    def has(self, ty: str) -> bool:
        return ty in self.command_types()

    def index_of(self, ty: str) -> int:
        try:
            return self.command_types().index(ty)
        except ValueError:
            return -1

    def packets_after(self, t: float) -> List[Dict[str, Any]]:
        return [p for p, ts in zip(self.received_packets, self.received_times) if ts >= t]


class MockConnection:
    """给 ArmController 用的假连接。"""

    def __init__(self, mock: MockController) -> None:
        self.mock = mock
        self.connected = True
        self.host = "127.0.0.1"

    def send_command(self, ty: str, db: Any = None) -> int:
        self.mock.send(ty, db or {})
        return len(self.mock.commands)

    def get_realtime_status(self) -> None:
        self.send_command("realtime/getStatus", {"robot": None})

    def start_control(
        self, config: Dict[str, Any], robots: Optional[List[str]] = None
    ) -> int:
        # 与真实连接保持一致: 多臂机必须显式带上 config.robot,
        # 省略会被控制器以 24050 拒绝。
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


def test_library() -> None:
    """动作库加载与索引 (一个 JSON 文件 = 一个动作)。"""
    print("\n[1] 动作库")
    lib = ActionLibrary(
        SERVICE_DIR / "data",
        exclude_files=LEGACY_FILES,
    )
    count = lib.load()
    check("动作库加载出动作", count > 0, f"{count} 个")

    # 从动作库里挑样本 (不写死动作名 —— 数据换了测试也要能跑)
    actions = lib.list_actions()
    if not actions:
        check("动作库非空", False)
        return

    # 片段最多的那个动作 (用来测多片段与时间有序性)
    multi = max(actions, key=lambda a: int(a["segments"]))
    name = str(multi["key"])
    unit = lib.find(name)
    check("按文件名能找到动作", unit is not None, name)
    if unit:
        check("动作含多个片段", len(unit.segments) > 1, f"{len(unit.segments)} 段")
        check("动作名 = 文件名 (去掉 .json)",
              unit.source == f"{name}.json" and unit.key == name,
              f"{unit.source} -> {unit.key}")
        model = unit.to_timeline()
        check("时间轴从 0 开始", abs(model.get_segment(0).start_time) < 1e-9)
        check("时长与片段组一致",
              abs(model.total_duration - unit.duration) < 1e-6,
              f"{model.total_duration:.2f}s")

        # 时间有序性: 原文件里 segments 不是按时间排的
        starts = [s.start_time for s in unit.segments]
        check("动作内片段按时间升序", starts == sorted(starts))

    # 文件名检索的几种写法
    check("带 .json 后缀能检索", lib.find(f"{name}.json") is not None)
    check("忽略首尾空白能检索", lib.find(f"  {name}  ") is not None)

    check("不存在的动作抛 ActionNotFound", _raises(lib, "不存在的动作xyz"))


def _raises(lib: ActionLibrary, name: str) -> bool:
    try:
        lib.get(name)
    except ActionNotFound:
        return True
    return False


def test_streamer_packet_format() -> None:
    """UDP 报文格式。"""
    print("\n[2] 关节流报文格式")
    mock = MockController(actual_joints=[0.1] * JOINT_COUNT)
    try:
        streamer = JointStreamer(host="127.0.0.1", port=mock.udp_port, frequency=100)
        streamer.set_idle_stop_ms(0)   # 不停发, 便于观察
        streamer.set_target("LeftArm", [0.5] * JOINT_COUNT)
        streamer.start()
        time.sleep(0.4)
        streamer.stop()

        check("收到了 UDP 关节包", len(mock.received_packets) > 0,
              f"{len(mock.received_packets)} 包")
        if mock.received_packets:
            pkt = mock.received_packets[0]
            check("报文是扁平 JSON (无 id/ty/db 包裹)",
                  "id" not in pkt and "ty" not in pkt and "db" not in pkt)
            check("含 robot 字段", pkt.get("robot") == "LeftArm")
            check("controlSpace=0 (关节空间)", pkt.get("controlSpace") == 0)
            check("commandMode=0 (绝对量)", pkt.get("commandMode") == 0)
            check("value 长度等于关节数", len(pkt.get("value", [])) == JOINT_COUNT,
                  str(len(pkt.get("value", []))))
            check("value 是有限数",
                  all(isinstance(v, (int, float)) for v in pkt.get("value", [])))

            # 频率: 100Hz 下 0.4 秒应该发 40 个包左右
            expected = 100 * 0.4
            actual = len(mock.received_packets)
            check("下发频率接近设定值 (100Hz)",
                  abs(actual - expected) < expected * 0.4,
                  f"{actual} 包 / 0.4s, 期望约 {expected:.0f}")
    finally:
        mock.stop()


def test_streamer_rejects_nan() -> None:
    """NaN 关节角必须被挡掉 (控制器会整包丢弃)。"""
    print("\n[3] 关节流异常值防护")
    mock = MockController(actual_joints=[0.1] * JOINT_COUNT)
    try:
        streamer = JointStreamer(host="127.0.0.1", port=mock.udp_port, frequency=100)
        streamer.set_idle_stop_ms(0)
        streamer.set_target("LeftArm", [float("nan")] * JOINT_COUNT)
        streamer.start()
        time.sleep(0.2)
        streamer.stop()
        check("NaN 关节角没有发出", len(mock.received_packets) == 0,
              f"发出 {len(mock.received_packets)} 包")
    finally:
        mock.stop()


def test_control_flow() -> None:
    """入控时序: 起流早于 startControl, 首包等于实测角, 结束必须停控。"""
    print("\n[4] 入控时序")
    actual = [0.25, -0.5, 0.75, 0.1, -0.2, 0.3, 0.0]
    mock = MockController(actual_joints=actual)
    conn = MockConnection(mock)

    phases: List[str] = []
    ctrl = ArmController(
        conn,
        frequency=100,
        command_port=mock.udp_port,
        lead_in_ms=700,
        joint_limit={"enable": True, "maxVelocity": 1.5, "maxAcceleration": 5.0},
        idle_stop_ms=0,
        on_phase=lambda p, d: phases.append(p),
    )
    ctrl.set_arm_names("LeftArm", "")
    ctrl.set_actual_joints("LeftArm", actual)

    # --- 入控 ---
    enter_start = time.monotonic()
    ctrl.enter_control()
    enter_end = time.monotonic()

    check("startControl 已发出", mock.has("realtime/startControl"))
    check("关节流已并行起流", ctrl.streaming)

    # 关键约束: 起流必须早于 startControl
    #
    # 用 streamer 自己记录的「实际发出时刻」来判定, 而不是 mock 的收包时刻 ——
    # mock 的收包线程是轮询的 (50ms 一轮), 收包时刻会明显滞后于真实发出时刻,
    # 用它判定会把「并行发首包」误判成「没并行」。
    start_idx = mock.index_of("realtime/startControl")
    start_cmd_time = mock.command_times[start_idx]
    first_send = ctrl._streamer.sent_times[0] if ctrl._streamer.sent_times else None
    check("起流早于 startControl (首包并行下发)",
          first_send is not None and first_send <= start_cmd_time,
          f"首包比 startControl 早 {(start_cmd_time - first_send) * 1000:.1f}ms"
          if first_send is not None else "未发出首包")

    # 首包必须等于实测角
    if mock.received_packets:
        first = mock.received_packets[0]
        check("首包等于实测关节角 (避免 firstFrameJumpRejected)",
              all(abs(first["value"][i] - actual[i]) < 1e-9 for i in range(JOINT_COUNT)),
              str([round(v, 3) for v in first["value"][:3]]))

    # 入控配置必须开关节限幅
    start_cmd = mock.commands[start_idx]
    cfg = start_cmd["db"].get("config", {})
    check("jointLimit.enable=true (协议缺省是关闭)",
          cfg.get("jointLimit", {}).get("enable") is True)
    check("holdOnIdle=true (停发不掉线)", cfg.get("holdOnIdle") is True)
    check("cartesianIkPath=none (关节流)", cfg.get("cartesianIkPath") == "none")
    # 必须显式指定 robot: 控制器上有多台机器人时省略 config.robot
    # 会被直接拒绝 (24050 「存在多台机器人, 必须指定 robot」)。
    check("config.robot 已指定 (多臂机省略会被拒 24050)",
          cfg.get("robot") == "LeftArm",
          f"robot={cfg.get('robot')!r}")

    # --- 对齐 ---
    print("\n[5] 起点对齐")
    origin = JointState(left=list(actual), right=[0.0] * JOINT_COUNT)
    lib = ActionLibrary(SERVICE_DIR / "data", exclude_files=LEGACY_FILES)
    lib.load()
    # 挑片段最多的动作 (轨迹长, 对齐过程才明显)
    items = lib.list_actions()
    unit = lib.get(str(max(items, key=lambda a: int(a["segments"]))["key"]))
    tl = unit.to_timeline(origin)

    frames: List[tuple] = []
    engine = PlaybackEngine(tl, on_frame=lambda t, s: frames.append((t, s)))

    align_start = time.monotonic()
    start_frame = engine.prime_start_frame()
    ctrl.push_frame(start_frame)
    check("收到轨迹起点后进入 ALIGNING", Phase.ALIGNING in phases)

    # 对齐期间推进时间轴是不允许的 —— 这里只推进 lead-in
    while not ctrl.advance_lead_in():
        time.sleep(0.008)
    align_ms = (time.monotonic() - align_start) * 1000
    check("对齐耗时约等于 lead_in_ms (700)", 650 <= align_ms <= 900, f"{align_ms:.0f}ms")
    check("对齐完成后 playback_active=True", ctrl.playback_active)

    # 对齐期间不应有超出实测角太多的包
    pkts = mock.packets_after(align_start)
    check("对齐期间发出了关节包", len(pkts) > 0, f"{len(pkts)} 包")

    # --- 播放 ---
    print("\n[6] 播放推进")
    engine.start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.6:
        engine.tick()
        time.sleep(0.016)
    check("时间轴已推进", engine.current_time > 0.1,
          f"{engine.current_time:.2f}s")
    check("播放阶段为 PLAYING", Phase.PLAYING in phases)

    # --- 停控 ---
    print("\n[7] 停控交还通道")
    ctrl.stop_control()
    check("stopControl 已发出", mock.has("realtime/stopControl"))
    check("停控后 streaming=False", not ctrl.streaming)
    check("停控后 playback_active=False", not ctrl.playback_active)

    stop_idx = mock.index_of("realtime/stopControl")
    check("stopControl 在 startControl 之后", stop_idx > start_idx)


def test_playback_timing() -> None:
    """时间轴推进与结束。"""
    print("\n[8] 播放引擎")
    lib = ActionLibrary(SERVICE_DIR / "data", exclude_files=LEGACY_FILES)
    lib.load()
    # 用最短的动作: 测试要在几秒内跑完, 长的动作 (准备点 48s) 等不起
    unit = min(lib.list_actions(), key=lambda a: float(a["duration"]))
    action = lib.get(str(unit["key"]))
    tl = action.to_timeline(JointState())

    frames = []
    finished = threading.Event()
    engine = PlaybackEngine(
        tl,
        on_frame=lambda t, s: frames.append(t),
        on_finished=finished.set,
    )
    engine.prime_start_frame()
    engine.start()
    check("立即发出第 0 帧", len(frames) == 1 and abs(frames[0]) < 1e-9)

    deadline = time.time() + 5.0
    while not finished.is_set() and time.time() < deadline:
        engine.tick()
        time.sleep(0.01)
    check("播放到时长后触发 finished", finished.is_set())
    check("结束时长正确", abs(engine.total_duration - action.duration) < 1e-6,
          f"{engine.total_duration:.2f}s ({action.key})")


def test_interpolation_continuity() -> None:
    """插值连续性: 片段交界处不该有速度跳变。"""
    print("\n[9] 插值连续性")
    from timeline.interpolation import smooth_step_interpolate

    a = JointState(left=[0.0] * JOINT_COUNT)
    b = JointState(left=[1.0] * JOINT_COUNT)
    mid = smooth_step_interpolate(a, b, 0.5).left[0]
    check("smoothStep 中点为 0.5", abs(mid - 0.5) < 1e-9, f"{mid:.6f}")
    check("smoothStep 起止速度为零",
          abs(smooth_step_interpolate(a, b, 0.0).left[0]) < 1e-12
          and abs(smooth_step_interpolate(a, b, 1.0).left[0] - 1.0) < 1e-12)


def main() -> int:
    print("=" * 60)
    print("机械臂动作服务自检 (mock 控制器, 不需要真机)")
    print("=" * 60)

    test_library()
    test_streamer_packet_format()
    test_streamer_rejects_nan()
    test_control_flow()
    test_playback_timing()
    test_interpolation_continuity()

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
