"""末端 (笛卡尔) 指令发送器 (UDP)。

配合 ``realtime/startControl`` 的 ``cartesianIkPath: analytic`` 使用: 本会话额外
接受末端空间报文, 由控制器逆解成关节。报文格式 (API 说明 21.9):

    {"robot": "<臂名>", "controlSpace": 1, "poseType": 1, "commandMode": 0,
     "value": [x, y, z, qw, qx, qy, qz]}

两个阶段:
- **hold**: 入控响应回来之前就要开始发 (首包等待窗只有 2000ms)。这时还不知道
  当前位姿, 所以发「相对当前实测的零增量」(commandMode=2, 单位四元数), 机器人不动。
- **trajectory**: 拿到入控位姿后, 从起点到终点按 S 曲线 (位置线性 + 姿态球面插值)
  逐包发绝对位姿。到终点后继续发终点, 直到 stop()。

独立线程发送, 理由同 ``streamer.py``: 主线程卡顿不能让指令流断顿。
"""

from __future__ import annotations

import json
import logging
import math
import socket
import threading
import time
from typing import Optional, Sequence

from robot.geometry import Quat, Vec3, slerp

logger = logging.getLogger("arm.cartesian")


class CartesianStreamer:
    """按固定频率发末端位姿。"""

    def __init__(self, host: str, port: int, robot: str, frequency: int = 100) -> None:
        self._host = host
        self._port = port
        self._robot = robot
        self._frequency = max(10, min(1000, int(frequency)))

        self._lock = threading.Lock()
        self._mode = "hold"
        self._traj: Optional[tuple] = None
        self._t0 = 0.0
        self._duration = 0.0

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.packets = 0
        self.send_failures = 0
        #: 最近一次发出的目标 (base 系), 供诊断
        self.last_value: Optional[list] = None

    # ------------------------------------------------------------ 起停

    def start(self) -> None:
        if self._running:
            return
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._running = True
        # 立刻补发一包: 不等第一个 tick, 把首包时间压到接近零
        self._send(self._hold_packet())
        self._thread = threading.Thread(
            target=self._loop, name="arm-cartesian-streamer", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
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

    # ------------------------------------------------------------ 目标

    def set_trajectory(
        self,
        start_pos: Sequence[float],
        start_q: Quat,
        end_pos: Sequence[float],
        end_q: Quat,
        duration: float,
    ) -> None:
        """切到轨迹模式。时间从调用时刻起算。"""
        with self._lock:
            self._traj = (tuple(start_pos), start_q, tuple(end_pos), end_q)
            self._duration = max(0.05, float(duration))
            self._t0 = time.monotonic()
            self._mode = "trajectory"

    @property
    def finished(self) -> bool:
        """轨迹是否已经发完 (进入终点保持)。"""
        with self._lock:
            return (
                self._mode == "trajectory"
                and time.monotonic() - self._t0 >= self._duration
            )

    # ------------------------------------------------------------ 内部

    def _hold_packet(self) -> dict:
        return {
            "robot": self._robot,
            "controlSpace": 1,
            "poseType": 1,
            "commandMode": 2,   # 相对当前实测
            "value": [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        }

    def _trajectory_packet(self) -> dict:
        with self._lock:
            traj = self._traj
            elapsed = time.monotonic() - self._t0
            duration = self._duration
        assert traj is not None
        sp, sq, ep, eq = traj
        u = min(1.0, max(0.0, elapsed / duration))
        s = u * u * (3.0 - 2.0 * u)   # S 曲线, 起止速度为 0
        pos: Vec3 = (
            sp[0] + s * (ep[0] - sp[0]),
            sp[1] + s * (ep[1] - sp[1]),
            sp[2] + s * (ep[2] - sp[2]),
        )
        q = slerp(sq, eq, s)
        return {
            "robot": self._robot,
            "controlSpace": 1,
            "poseType": 1,
            "commandMode": 0,   # 绝对量
            "value": [pos[0], pos[1], pos[2], q[0], q[1], q[2], q[3]],
        }

    def _loop(self) -> None:
        period = 1.0 / self._frequency
        next_t = time.monotonic()
        while self._running:
            with self._lock:
                mode = self._mode
            packet = self._trajectory_packet() if mode == "trajectory" else self._hold_packet()
            self._send(packet)

            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -period * 2:
                next_t = time.monotonic()   # 落后太多就对齐, 不补发旧目标

    def _send(self, packet: dict) -> None:
        sock = self._sock
        if sock is None:
            return
        value = packet["value"]
        if not all(math.isfinite(v) for v in value):
            logger.warning("[末端流] 目标含非有限值, 已丢弃: %s", value)
            return
        payload = json.dumps(packet, separators=(",", ":")).encode("utf-8")
        try:
            sock.sendto(payload, (self._host, self._port))
        except OSError as exc:
            self.send_failures += 1
            if self.send_failures <= 3:
                logger.warning("[末端流] UDP 发送失败: %s", exc)
            return
        if self.packets == 0:
            logger.info("[末端流] 首包 -> %s:%d %s", self._host, self._port, payload.decode())
        self.packets += 1
        self.last_value = list(value)
