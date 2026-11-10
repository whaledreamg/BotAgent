"""关节流发送器 (UDP)。

移植自 ActionPlanner 的 src/robot/JointStreamer.cpp, 去掉 Qt 依赖。

为什么必须独立线程:
控制器的缺帧保持窗只有 5 个锚点周期 (下限 30ms), 任何卡顿 (Python 的 GIL、
GC、HTTP 处理) 都会让关节流断顿, 被判定为通道陈旧并回收直达通道。
把发送放在专用线程, 主线程卡顿不会让关节流断顿。

节流用「绝对下一次发送时刻」推进, 不做累加复位, 长时间运行不会漂移。

报文格式 (控制器指令面, 不是 TCP 动作也不是 WebSocket 主题):
    扁平 JSON, 无 {id, ty, db} 包裹, 一包一个目标, 不分片, 不回包。
    {"robot": "<臂名>", "controlSpace": 0, "commandMode": 0, "value": [7 个 rad]}
"""

from __future__ import annotations

import json
import logging
import math
import socket
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("arm.streamer")

#: 七轴关节数量
JOINT_COUNT = 7

#: 判定「目标变了」的阈值 (rad)。小于它算没动。
IDLE_EPSILON = 1e-6


class _ArmStream:
    """单条臂的锚点状态。"""

    __slots__ = ("from_v", "to_v", "from_ns", "to_ns", "last_change_ns", "interval_ms", "primed", "idle")

    def __init__(self) -> None:
        self.from_v: List[float] = []
        self.to_v: List[float] = []
        self.from_ns = 0
        self.to_ns = 0
        self.last_change_ns = 0
        self.interval_ms = 10.0
        self.primed = False
        self.idle = False


class JointStreamer:
    """按固定频率把关节目标发到控制器。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 9030, frequency: int = 100) -> None:
        self._host = host
        self._port = port
        self._frequency = max(10, min(1000, frequency))

        self._targets: Dict[str, _ArmStream] = {}
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()

        self._total_packets = 0
        self._send_failures = 0
        self._logged_first = False
        self._idle_stop_ms = 1000
        #: 每包的实际发出时刻 (time.monotonic())。用于诊断与自检:
        #: 协议要求起流早于 startControl, 需要精确的首包时间。
        self.sent_times: List[float] = []

        self._next_send_ns = 0
        self._t0 = time.monotonic_ns()

    # ------------------------------------------------------------ 配置

    @property
    def frequency(self) -> int:
        return self._frequency

    def set_frequency(self, hz: int) -> None:
        self._frequency = max(10, min(1000, hz))

    def set_host(self, host: str) -> None:
        if host:
            self._host = host

    def set_port(self, port: int) -> None:
        if 0 < port <= 65535:
            self._port = port

    def set_idle_stop_ms(self, ms: int) -> None:
        """静止停发门限。配合控制器的 holdOnIdle=true, 会话保持 active。"""
        self._idle_stop_ms = max(0, min(60000, ms))
        if self._idle_stop_ms == 0:
            with self._lock:
                for s in self._targets.values():
                    s.idle = False

    # ------------------------------------------------------------ 起停

    def start(self) -> None:
        if self._running:
            return

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        except OSError:
            pass

        self._t0 = time.monotonic_ns()
        self._next_send_ns = 0
        self._total_packets = 0
        self._send_failures = 0
        self._logged_first = False
        self._running = True

        now_ns = self._elapsed_ns()
        with self._lock:
            for s in self._targets.values():
                # 入控时 set_target 先于 start 被调用, 那时的时间戳基于旧时钟。
                # restart 后必须重新打点, 否则 from_ns/to_ns 落在未来或远早于现在,
                # 采样点的区间判定不成立, 插值等于没开。
                s.from_ns = now_ns
                s.to_ns = now_ns
                s.last_change_ns = now_ns
                s.idle = False
                s.from_v = list(s.to_v)
                s.primed = bool(s.to_v)

        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, name="arm-joint-streamer", daemon=True)
        self._thread.start()

        # 立即补发一包, 不等第一个 tick。
        #
        # 协议要求 startControl 与首包并行: 首包等待窗只有 2000ms, 而定时器
        # 的第一个 tick 要等到半个周期之后才触发 (100Hz 下是 5ms)。
        # 在系统刚起流、正处于最忙的时刻, 这 5ms 完全可能拖到首包窗的尾巴上。
        # 这里主动先发一包, 把首包时间压到接近零。
        self._send_all()

        logger.info(
            "[关节流] 启动 %dHz -> %s:%d", self._frequency, self._host, self._port
        )

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        logger.info("[关节流] 已停止 (累计 %d 包)", self._total_packets)

    def remove_arm(self, arm_name: str) -> None:
        with self._lock:
            self._targets.pop(arm_name, None)

    # ------------------------------------------------------------ 目标

    def set_target(self, arm_name: str, joints: List[float]) -> None:
        """更新某臂的目标关节角。

        上游在静止时会持续送来同一个值, 这些「没有位移的更新」不该被当成运动,
        否则永远判不出静止。
        """
        if not arm_name or len(joints) < JOINT_COUNT:
            return

        now = self._elapsed_ns()
        with self._lock:
            s = self._targets.get(arm_name)
            if s is None:
                s = _ArmStream()
                self._targets[arm_name] = s

            if not s.primed:
                # 首帧: 上下锚点同值, 避免刚起流就从一个空值插值过去
                s.from_v = list(joints)
                s.to_v = list(joints)
                s.from_ns = now
                s.to_ns = now
                s.last_change_ns = now
                s.idle = False
                s.primed = True
                return

            moved = False
            for i in range(JOINT_COUNT):
                if abs(joints[i] - s.to_v[i]) > IDLE_EPSILON:
                    moved = True
                    break

            # 统计锚点间隔, 用于决定插值延迟该给多少。
            # 用 EMA 而不是取最近一次: 上游偶尔卡一帧不会让延迟突然抖一下。
            dt_ms = (now - s.to_ns) / 1e6
            if 1.0 < dt_ms < 200.0:
                s.interval_ms = 0.85 * s.interval_ms + 0.15 * dt_ms

            if moved:
                s.from_v = list(s.to_v)
                s.from_ns = s.to_ns
                s.to_v = list(joints)
                s.to_ns = now
                s.last_change_ns = now
                # 一旦重新动起来就立刻恢复下发
                s.idle = False
            # 没动时什么都不做。
            #
            # 特别注意不能把 to_ns 推到 now: 那会把插值区间越拉越长,
            # 采样点被延迟后落在区间内部, 输出就会朝 to 缓慢爬行 ——
            # 本该静止的关节反而在持续微动, 正好是要消除的那种噪声。
            # 保持 to_ns 不动, 采样时刻自然越过它, _sample_at 直接返回 to。

    # ------------------------------------------------------------ 主循环

    def _elapsed_ns(self) -> int:
        return time.monotonic_ns() - self._t0

    def _loop(self) -> None:
        period_ns = 1_000_000_000 // max(1, self._frequency)
        while self._running:
            now = self._elapsed_ns()

            if now < self._next_send_ns:
                # 睡到下一个发送时刻。用 wait 而不是 sleep, 这样 stop() 能立刻唤醒。
                wait_ms = (self._next_send_ns - now) / 1e6
                self._wake.wait(timeout=max(0.001, min(wait_ms, 20.0) / 1000.0))
                continue

            # 如果落后超过一整个周期 (例如系统卡了一下), 直接对齐到当前时刻,
            # 不去补发积压的包 —— 补发只会把旧目标灌给控制器。
            if now - self._next_send_ns > period_ns * 2:
                self._next_send_ns = now
            self._next_send_ns += period_ns

            if self._all_idle(now):
                with self._lock:
                    first_stop = False
                    for s in self._targets.values():
                        if not s.idle:
                            s.idle = True
                            first_stop = True
                if first_stop:
                    logger.info(
                        "[关节流] 已静止 %dms, 暂停下发 (holdOnIdle 保持会话)",
                        self._idle_stop_ms,
                    )
                continue

            self._send_all()

    def _all_idle(self, now: int) -> bool:
        if self._idle_stop_ms <= 0:
            return False
        with self._lock:
            if not self._targets:
                return False
            hold_ns = self._idle_stop_ms * 1_000_000
            for s in self._targets.values():
                if not s.primed:
                    return False
                if s.idle:
                    continue  # 已经停发的臂不再参与判定
                if now - s.last_change_ns < hold_ns:
                    return False  # 还没到静止门限
        return True

    def _sample_at(self, s: _ArmStream, now: int) -> List[float]:
        if not s.primed:
            return []

        # 延迟一整个锚点间隔。
        #
        # 必须是一整个而不是半个: 锚点在 [from_ns, to_ns] 之间才有插值区间,
        # 若只延迟半个间隔, 则每个 16ms 窗口里只有前一半落在区间内、
        # 后一半 t 已经越过 to_ns 被钉住, 仍然是「走一下、停一下」的阶梯。
        # 延迟一整个间隔, t 扫过的正好是完整的 [from_ns, to_ns],
        # 每个发包周期都取到不同的值, 速度连续。
        delay_ns = int(s.interval_ms * 1e6)
        t = now - delay_ns

        span = s.to_ns - s.from_ns
        if span <= 0:
            return list(s.to_v)

        # 超出当前区间 (上游还没送来新锚点): 钉在端点, 不外推。
        # 外推会让机械臂朝一个尚未确定的方向继续走。
        if t >= s.to_ns:
            return list(s.to_v)
        if t <= s.from_ns:
            return list(s.from_v)

        u = (t - s.from_ns) / span
        return [
            s.from_v[i] + u * (s.to_v[i] - s.from_v[i]) for i in range(JOINT_COUNT)
        ]

    def _send_all(self) -> None:
        now = self._elapsed_ns()
        with self._lock:
            items = list(self._targets.items())
        for arm_name, s in items:
            values = self._sample_at(s, now)
            if not values:
                continue
            if self._write_one(arm_name, values):
                self._total_packets += 1

    def _write_one(self, arm_name: str, joints: List[float]) -> bool:
        if len(joints) < JOINT_COUNT:
            return False

        # 非有限值 (NaN/Inf) 会被控制器整包丢弃并置 invalidDatagram。
        # 某些插值边界可能产生 NaN, 这里挡掉并提示, 避免「发了很多包但全被丢弃」。
        for v in joints:
            if not math.isfinite(v):
                logger.warning("[关节流] %s 关节角含非有限值, 控制器会整包丢弃", arm_name)
                return False

        packet = {
            "robot": arm_name,
            "controlSpace": 0,   # 关节空间
            "commandMode": 0,    # 绝对量
            "value": list(joints[:JOINT_COUNT]),
        }
        payload = json.dumps(packet, separators=(",", ":")).encode("utf-8")

        sock = self._sock
        if sock is None:
            return False
        try:
            sock.sendto(payload, (self._host, self._port))
        except OSError as exc:
            self._send_failures += 1
            if self._send_failures <= 3:
                logger.warning("[关节流] UDP 发送失败: %s -> %s:%d", exc, self._host, self._port)
            return False

        self.sent_times.append(time.monotonic())
        if not self._logged_first:
            self._logged_first = True
            logger.info("[关节流] 首包 -> %s:%d %s", self._host, self._port, payload.decode())
        return True
