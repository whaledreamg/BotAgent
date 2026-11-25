"""Revo2 灵巧手协议层 (Modbus RTU over 控制器 rs485 接口)。

**灵巧手不挂在 EtherCAT 上** —— 它接在末端通信板的 RS485 口, 要经控制器
的 ``rs485/*`` 接口转发::

    rs485/list   -> 按从站名 (EtherCAT485L / EtherCAT485R) 找到 channel
    rs485/init   -> 配置串口 (115200 8N1)
    rs485/write  -> 发 Modbus RTU 帧
    rs485/read   -> 读应答

6 个自由度 (归一化 0~1000):

    0 拇指弯曲   1 拇指侧摆   2 食指   3 中指   4 无名指   5 小指

**协议移植自** ActionPlanner 的 ``src/robot/HandController.{h,cpp}`` ——
那是这套设备上跑通过的权威实现, 本模块保持逐字节一致。

.. important::
   **不要用 UDP 实时指令包里的 ``hand`` 字段**。控制器文档 (API 说明
   §21.9) 规定实时指令包的字段只有 ``robot`` / ``controlSpace`` /
   ``commandMode`` / ``poseType`` / ``value`` —— **没有 ``hand`` 这个
   字段**。多写一个字段进去, 轻则被忽略, 重则整包被判 ``invalidDatagram``
   连手臂都不动。灵巧手只能走 ``rs485/*`` 这条路。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("arm.hand")

#: 灵巧手自由度 (6 个手指)
DOF_COUNT = 6

#: 各 DOF 的最大角度 (度) —— 0~1000 归一化值与之线性对应
MAX_DEG = (59, 90, 81, 81, 81, 81)

#: 各 DOF 的中文名
DOF_NAMES_CN = ("拇指弯曲", "拇指侧摆", "食指", "中指", "无名指", "小指")

#: 寄存器: 写全部 6 DOF (1010, 12 个寄存器)
REG_WRITE_ALL = 0x03F2
#: 寄存器: 读全部 6 DOF 位置 (2000)
REG_READ_POS = 0x07D0

#: 写动作时长 (ms) —— 驱动器按这个时间走完目标, 避免瞬间打到
WRITE_TIME_MS_MIN = 1
WRITE_TIME_MS_MAX = 2000
DEFAULT_WRITE_TIME_MS = 100

#: 预设手势 (归一化 0~1000)
#:
#: 与 ActionPlanner 的 ``HandProtocol::preset`` 一致。
PRESETS: Dict[str, List[int]] = {
    "一把抓": [1000, 0, 1000, 1000, 1000, 1000],
    "释放": [0, 0, 0, 0, 0, 0],
    "半抓": [500, 0, 500, 500, 500, 500],
    "两指捏": [800, 0, 200, 0, 0, 0],
    "点赞": [0, 0, 1000, 1000, 1000, 1000],
    "指点": [1000, 0, 0, 1000, 1000, 1000],
    # 英文别名
    "grab": [1000, 0, 1000, 1000, 1000, 1000],
    "release": [0, 0, 0, 0, 0, 0],
    "half": [500, 0, 500, 500, 500, 500],
    "pinch": [800, 0, 200, 0, 0, 0],
}

# ---- 控制器错误码 ----
ERR_BUSY = 20003            # 资源正忙 (可重试)
ERR_OCCUPIED = 23252        # 通道正被厂商夹爪协议占用
ERR_NOT_CONFIGURED = 23254  # 串口未配置: 需要重新 rs485/init
ERR_IO = 23258              # 串口 I/O 失败: 文档要求错误关闭后重新 init
ERR_NEED_REINIT = (ERR_NOT_CONFIGURED, ERR_IO)


class HandError(RuntimeError):
    """灵巧手通信 / 配置失败。"""

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


# ======================================================================
# Modbus RTU
# ======================================================================

def crc16(data: bytes) -> int:
    """Modbus RTU CRC16 (多项式 0xA001)。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def build_frame(slave: int, func: int, payload: Sequence[int]) -> List[int]:
    """构造 Modbus RTU 帧 (含帧尾 CRC, 低字节在前)。"""
    body = [slave & 0xFF, func & 0xFF] + [b & 0xFF for b in payload]
    c = crc16(bytes(body))
    return body + [c & 0xFF, (c >> 8) & 0xFF]


def verify_crc(frame: Sequence[int]) -> bool:
    """校验帧尾 CRC。"""
    if not frame or len(frame) < 4:
        return False
    expect = ((int(frame[-1]) & 0xFF) << 8) | (int(frame[-2]) & 0xFF)
    return crc16(bytes(int(b) & 0xFF for b in frame[:-2])) == expect


def build_write_all_frame(
    slave: int, positions: Sequence[int], time_ms: int
) -> List[int]:
    """写全部 6 DOF 的帧 (功能码 0x10, 寄存器 1010~1021)。

    寄存器按 ``(位置, 时长)`` 交替排列, 每个 2 字节。
    """
    if len(positions) != DOF_COUNT:
        raise ValueError(f"positions 长度必须为 {DOF_COUNT}")
    payload = [
        (REG_WRITE_ALL >> 8) & 0xFF, REG_WRITE_ALL & 0xFF,
        0x00, 0x0C,   # 寄存器数 = 12
        0x18,         # 字节数 = 24
    ]
    for p in positions:
        p = max(0, min(1000, int(p)))
        t = max(WRITE_TIME_MS_MIN, min(WRITE_TIME_MS_MAX, int(time_ms)))
        payload += [(p >> 8) & 0xFF, p & 0xFF, (t >> 8) & 0xFF, t & 0xFF]
    return build_frame(slave, 0x10, payload)


def build_read_positions_frame(slave: int) -> List[int]:
    """读全部 6 DOF 位置的帧 (功能码 0x04)。"""
    payload = [
        (REG_READ_POS >> 8) & 0xFF, REG_READ_POS & 0xFF,
        0x00, DOF_COUNT,
    ]
    return build_frame(slave, 0x04, payload)


def parse_positions(frame: Sequence[int]) -> List[int]:
    """从读应答里解出 6 个 DOF 位置。

    应答格式: 从站(1) + 功能码(1) + 字节数(1) + 12 字节数据 + CRC(2)
    """
    if len(frame) < 17:
        raise HandError(f"读应答太短: {len(frame)} 字节 (要 17)")
    out: List[int] = []
    for i in range(DOF_COUNT):
        hi = int(frame[3 + i * 2]) & 0xFF
        lo = int(frame[4 + i * 2]) & 0xFF
        out.append(max(0, min(1000, (hi << 8) | lo)))
    return out


def check_write_ack(rx: Sequence[int]) -> None:
    """校验 0x10 的写应答, 异常则抛 :class:`HandError`。

    应答格式: 从站(1) + 功能码(1) + 起始寄存器(2) + 数量(2) + CRC(2)
    """
    if not rx:
        raise HandError("无应答")
    if not verify_crc(rx):
        raise HandError("应答 CRC 错误")
    if len(rx) >= 3 and (int(rx[1]) & 0xFF) == 0x90:
        raise HandError(f"从站异常码 0x{int(rx[2]) & 0xFF:02X}")
    if len(rx) >= 2 and (int(rx[1]) & 0xFF) != 0x10:
        raise HandError(f"非预期功能码 0x{int(rx[1]) & 0xFF:02X}")


def deg_to_raw(deg: float, dof: int) -> int:
    """角度 -> 归一化 0~1000。"""
    if dof < 0 or dof >= DOF_COUNT:
        return 0
    return max(0, min(1000, round(deg / MAX_DEG[dof] * 1000.0)))


def raw_to_deg(raw: int, dof: int) -> float:
    """归一化 0~1000 -> 角度。"""
    if dof < 0 or dof >= DOF_COUNT:
        return 0.0
    return max(0.0, min(float(MAX_DEG[dof]), raw / 1000.0 * MAX_DEG[dof]))


# ======================================================================
# 单只手
# ======================================================================

class Revo2Hand:
    """一只 Revo2 灵巧手 (经控制面 ``rs485`` 透传)。

    **线程不安全** —— 由一个线程独占。
    """

    def __init__(
        self,
        conn: Any,
        side: str,
        slave_name: str,
        addr: int,
        baud_rate: int = 115200,
    ) -> None:
        self._conn = conn
        self.side = side
        #: 通信板从站名 (如 EtherCAT485L) —— 用来在 rs485/list 里找通道
        self.slave_name = slave_name
        #: rs485 通道标识 —— init() 时从 rs485/list 取
        self.channel = ""
        self.addr = addr
        self.baud_rate = baud_rate

    # ---- 通道定位 ----

    def _find_channel(self) -> str:
        """在 ``rs485/list`` 里按从站名找通道。

        文档要求通道标识**原样取自 list、不自行拼接** (里面含主站名)。
        """
        resp = self._conn.request_sync("rs485/list", {}, timeout=5.0)
        if resp is None:
            raise HandError("rs485/list 无响应")
        if resp.get("err"):
            err = resp["err"]
            raise HandError(
                f"rs485/list 失败 ({err.get('code')}): {err.get('message')}",
                code=err.get("code"),
            )
        channels = resp.get("db") or []
        for c in channels:
            if not isinstance(c, dict) or c.get("backend") != "dualEc2485":
                continue
            slave = (c.get("backendStatus") or {}).get("slave")
            if slave != self.slave_name:
                continue
            if c.get("supported") is False:
                raise HandError(f"通道 {c.get('channel')} 当前平台不支持")
            if c.get("occupiedBy"):
                raise HandError(self._occupied_msg(str(c.get("channel"))))
            return str(c["channel"])
        names = [
            (c.get("channel"), (c.get("backendStatus") or {}).get("slave"))
            for c in channels if isinstance(c, dict)
        ]
        raise HandError(
            f"rs485/list 里没有从站 {self.slave_name} 对应的 dualEc2485 通道, "
            f"现有通道: {names}"
        )

    def _occupied_msg(self, channel: str) -> str:
        return (
            f"通道 {channel} 正被厂商夹爪协议占用 (23252), 无法透传。"
            f"该手的 gripper 设备型号需为 Generic485, 或先用 "
            f"peripheral/setDeviceEnabled 停用该通道的夹爪协议"
            f" (停用需要现场确认夹爪静止, 本程序不会替你确认)"
        )

    # ---- 初始化 ----

    def init(self) -> None:
        """找通道 -> 初始化串口。失败抛 :class:`HandError`。"""
        self.channel = self._find_channel()
        self._init_channel()

    def _init_channel(self) -> None:
        """初始化串口 —— 串行 + 20003 (资源正忙) 重试。

        左右手的 ``rs485/init`` 必须串行: 文档 §33 总则写「不同通道可独立
        进行」, 但上机实测两只手同时 init 必有一只返回 20003。
        """
        params = {
            "channel": self.channel,
            "baudRate": int(self.baud_rate),
            "stopBits": 1,
            "parity": 0,
        }
        with _init_lock:
            for attempt in range(INIT_BUSY_RETRIES + 1):
                resp = self._conn.request_sync("rs485/init", params, timeout=8.0)
                if resp is None:
                    raise HandError("rs485/init 无响应")
                err = resp.get("err")
                if not err:
                    return
                code = err.get("code")
                if code == ERR_OCCUPIED:
                    raise HandError(self._occupied_msg(self.channel), code=code)
                if code != ERR_BUSY or attempt == INIT_BUSY_RETRIES:
                    raise HandError(
                        f"rs485/init 失败 ({code}): {err.get('message')}",
                        code=code,
                    )
                time.sleep(INIT_BUSY_WAIT_S)

    def reinit_channel(self) -> None:
        """重新初始化串口 (I/O 错误后要重来)。"""
        self._init_channel()

    # ---- 收发 ----

    def _write_read(
        self, frame: Sequence[int], read_length: int
    ) -> List[int]:
        """写一帧再读应答。"""
        resp = self._conn.request_sync(
            "rs485/write",
            {"channel": self.channel, "data": [int(b) & 0xFF for b in frame]},
            timeout=5.0,
        )
        if resp is None:
            raise HandError("rs485/write 无响应")
        if resp.get("err"):
            err = resp["err"]
            raise HandError(
                f"rs485/write 失败 ({err.get('code')}): {err.get('message')}",
                code=err.get("code"),
            )
        if read_length <= 0:
            return [0]

        resp = self._conn.request_sync(
            "rs485/read",
            {
                "channel": self.channel,
                "length": int(read_length),
                "timeout": 400,
            },
            timeout=8.0,
        )
        if resp is None:
            raise HandError("rs485/read 无响应")
        if resp.get("err"):
            err = resp["err"]
            raise HandError(
                f"rs485/read 失败 ({err.get('code')}): {err.get('message')}",
                code=err.get("code"),
            )
        data = resp.get("db")
        if not isinstance(data, list):
            raise HandError("rs485/read 没有返回字节数组")
        return [int(b) & 0xFF for b in data]

    def write_all(self, positions: Sequence[int], time_ms: int) -> None:
        """写 6 个自由度并校验应答。"""
        frame = build_write_all_frame(self.addr, positions, time_ms)
        rx = self._write_read(frame, 8)
        check_write_ack(rx)

    def read_positions(self) -> List[int]:
        """读回 6 个自由度的当前位置。"""
        frame = build_read_positions_frame(self.addr)
        rx = self._write_read(frame, 17)
        if not verify_crc(rx):
            raise HandError("读应答 CRC 错误")
        return parse_positions(rx)


#: ``rs485/init`` 的进程内锁 —— 左右手的 init 必须串行
_init_lock = threading.Lock()
INIT_BUSY_RETRIES = 10
INIT_BUSY_WAIT_S = 0.15


# ======================================================================
# 双手客户端
# ======================================================================

class HandController:
    """双手灵巧手客户端。

    **与机械臂控制相互独立** —— 机械臂 WebSocket 通了, 灵巧手还要单独
    「连接」 (解析通道 + 初始化串口) 才能真正下发。

    灵巧手的通信**比臂控慢得多** (一次写+读是两次控制面往返, 还要等通信板
    发送、等手应答), 所以它跑在**独立线程**里, **不能**在臂控循环里同步调用。
    """

    def __init__(
        self,
        conn: Any,
        *,
        left_slave: str = "EtherCAT485L",
        right_slave: str = "EtherCAT485R",
        left_addr: int = 126,
        right_addr: int = 127,
        baud_rate: int = 115200,
        write_time_ms: int = DEFAULT_WRITE_TIME_MS,
        enabled: bool = True,
    ) -> None:
        self._conn = conn
        self.enabled = enabled
        self.write_time_ms = max(
            WRITE_TIME_MS_MIN, min(WRITE_TIME_MS_MAX, int(write_time_ms))
        )
        self.left = Revo2Hand(conn, "left", left_slave, left_addr, baud_rate)
        self.right = Revo2Hand(conn, "right", right_slave, right_addr, baud_rate)
        self._ready = {"left": False, "right": False}
        self._positions = {"left": [0] * DOF_COUNT, "right": [0] * DOF_COUNT}
        self._lock = threading.RLock()

    # ---- 状态 ----

    def is_ready(self, side: Optional[str] = None) -> bool:
        """(某只) 手是否已就绪。"""
        with self._lock:
            if side is None:
                return self._ready["left"] and self._ready["right"]
            return self._ready.get(side, False)

    def positions(self, side: str = "right") -> List[int]:
        """最近一次已知的手位 (归一化 0~1000)。"""
        with self._lock:
            return list(self._positions.get(side, [0] * DOF_COUNT))

    def status(self) -> Dict[str, Any]:
        """给 HTTP 接口看的状态。"""
        with self._lock:
            return {
                "enabled": self.enabled,
                "ready": self._ready["left"] and self._ready["right"],
                "left": {
                    "ready": self._ready["left"],
                    "channel": self.left.channel,
                    "slave": self.left.slave_name,
                    "addr": self.left.addr,
                    "positions": list(self._positions["left"]),
                },
                "right": {
                    "ready": self._ready["right"],
                    "channel": self.right.channel,
                    "slave": self.right.slave_name,
                    "addr": self.right.addr,
                    "positions": list(self._positions["right"]),
                },
                "write_time_ms": self.write_time_ms,
                "presets": list(PRESETS.keys()),
            }

    # ---- 连接 ----

    def connect(self) -> Dict[str, Any]:
        """解析通道 + 初始化串口。

        **一只手失败不影响另一只** —— 左手坏了右手还能用。
        """
        if not self.enabled:
            raise HandError("灵巧手通道未启用 (configs/arm.yaml 的 arm.hand.enabled)")
        errors: Dict[str, str] = {}
        for side, hand in (("left", self.left), ("right", self.right)):
            try:
                hand.init()
                with self._lock:
                    self._ready[side] = True
                logger.info(
                    "[灵巧手] %s 手就绪 (通道 %s, 从站 %d)",
                    "左" if side == "left" else "右",
                    hand.channel, hand.addr,
                )
            except HandError as exc:
                with self._lock:
                    self._ready[side] = False
                errors[side] = str(exc)
                logger.error("[灵巧手] %s 手初始化失败: %s", side, exc)
        if not self.is_ready():
            raise HandError(
                "灵巧手初始化失败: "
                + "; ".join(f"{k}={v}" for k, v in errors.items())
            )
        return self.status()

    def disconnect(self) -> None:
        """只清本地状态, 不向控制器发任何东西。

        串口保持控制器那边的状态不动 —— 灵巧手是独立外设, 断开只表示
        本程序不再下发, 没必要 reset 通道 (否则下次连接又要重走一遍 init)。
        """
        with self._lock:
            self._ready["left"] = False
            self._ready["right"] = False

    # ---- 下发 ----

    def send_side(self, side: str, positions: Sequence[int]) -> bool:
        """给一只手下发一组目标。成功返回 True。"""
        if side not in ("left", "right"):
            raise HandError(f"side 只能是 left / right, 得到 {side!r}")
        hand = self.left if side == "left" else self.right
        with self._lock:
            ready = self._ready[side]
        if not ready:
            raise HandError(f"{side} 手未就绪 (先连灵巧手)")
        try:
            hand.write_all(positions, self.write_time_ms)
        except HandError as exc:
            logger.warning("[灵巧手] %s 手下发失败: %s", side, exc)
            # 串口错了一次就废了 —— 文档要求重新 init
            if exc.code in ERR_NEED_REINIT:
                try:
                    hand.reinit_channel()
                except HandError as exc2:
                    logger.warning("[灵巧手] %s 手重新 init 失败: %s", side, exc2)
            raise
        with self._lock:
            self._positions[side] = [
                max(0, min(1000, int(p))) for p in positions
            ]
        return True

    def send_both(
        self, left: Optional[Sequence[int]] = None,
        right: Optional[Sequence[int]] = None,
    ) -> bool:
        """给双手下发目标 (没给的那只不动)。"""
        ok = True
        if left is not None:
            try:
                self.send_side("left", left)
            except HandError as exc:
                logger.warning("[灵巧手] 左手: %s", exc)
                ok = False
        if right is not None:
            try:
                self.send_side("right", right)
            except HandError as exc:
                logger.warning("[灵巧手] 右手: %s", exc)
                ok = False
        return ok

    def send_preset(self, name: str, side: Optional[str] = None) -> bool:
        """下发一个预设手势。"""
        key = str(name or "").strip()
        pose = PRESETS.get(key)
        if pose is None:
            raise HandError(
                f"未知手势 {name!r} (可用: {', '.join(PRESETS.keys())})"
            )
        if side == "left":
            return self.send_side("left", pose)
        if side == "right":
            return self.send_side("right", pose)
        return self.send_both(left=pose, right=pose)

    def refresh_positions(self) -> Dict[str, List[int]]:
        """读回双手当前位置。"""
        out: Dict[str, List[int]] = {}
        for side, hand in (("left", self.left), ("right", self.right)):
            with self._lock:
                if not self._ready[side]:
                    continue
            try:
                pos = hand.read_positions()
            except HandError as exc:
                logger.warning("[灵巧手] 读 %s 手位置失败: %s", side, exc)
                continue
            with self._lock:
                self._positions[side] = pos
            out[side] = pos
        return out


__all__ = [
    "DEFAULT_WRITE_TIME_MS",
    "DOF_COUNT",
    "DOF_NAMES_CN",
    "HandController",
    "HandError",
    "MAX_DEG",
    "PRESETS",
    "REG_READ_POS",
    "REG_WRITE_ALL",
    "Revo2Hand",
    "build_frame",
    "build_read_positions_frame",
    "build_write_all_frame",
    "check_write_ack",
    "crc16",
    "deg_to_raw",
    "parse_positions",
    "raw_to_deg",
    "verify_crc",
]
