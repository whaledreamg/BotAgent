"""机器人控制面 (WebSocket) 客户端。

移植自 ActionPlanner 的 src/robot/RobotConnection.cpp, 去掉 Qt 依赖。

协议:
- 控制面是 WebSocket, 报文信封 {"id": n, "ty": "动作名", "db": {...}}
- 订阅报文 (robotState/sub) 的订阅字段放**顶层**, 不在 db 内
- 状态推送形如 {"ty": "publish/robot/<臂名>/status", "db": {...}}

本模块是同步阻塞的 (基于 websockets.sync.client), 调用方放在自己的线程里跑。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("arm.connection")


class ControllerError(RuntimeError):
    """控制器通信错误。"""


class RobotConnection:
    """机器人控制面连接。"""

    def __init__(
        self,
        url: str,
        timeout: float = 5.0,
        on_state: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self._on_state = on_state
        self._on_event = on_event

        self._ws = None
        self._message_id = 1
        self._connected = False
        self._host = ""
        self._lock = threading.Lock()
        self._pending: Dict[int, str] = {}
        self._reader: Optional[threading.Thread] = None
        self._closing = False
        #: 同步请求用的应答槽: msg_id -> {"event": Event, "response": dict}
        self._sync_slots: Dict[int, Dict[str, Any]] = {}

    # ------------------------------------------------------------ 连接

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def host(self) -> str:
        """控制器主机名。UDP 指令面要发到同一台控制器上。"""
        return self._host

    def connect(self) -> bool:
        """建立连接。失败返回 False (不抛异常, 让上层降级)。"""
        try:
            from websockets.sync.client import connect
        except ImportError as exc:  # pragma: no cover
            raise ControllerError(f"缺少 websockets 依赖: {exc}") from exc

        # 明确禁用代理。
        #
        # 控制器在局域网内 (默认 192.168.100.16), 环境里的 HTTP_PROXY /
        # SOCKS_PROXY 把它当外网地址, websockets 会尝试走代理并报
        # 「connecting through a SOCKS proxy requires python-socks」。
        # 即使代理装上了, 把内网流量绕出去也是错的 —— 而且控制器的 UDP
        # 指令面本来就直连, 两边路径不一致。
        kwargs = {"open_timeout": self.timeout}
        if _supports_proxy_arg(connect):
            kwargs["proxy"] = None

        try:
            self._ws = connect(self.url, **kwargs)
        except Exception as exc:
            logger.warning("[控制面] 连接失败 %s: %s", self.url, exc)
            self._ws = None
            return False

        # 记住主机: realtime 的 UDP 指令面要发到同一台控制器
        self._host = _host_from_url(self.url)
        self._connected = True
        self._closing = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        logger.info("[控制面] 已连接 %s", self.url)
        return True

    def disconnect(self) -> None:
        self._closing = True
        self._connected = False
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        logger.info("[控制面] 已断开")

    def _read_loop(self) -> None:
        """后台读线程: 分发响应与状态推送。"""
        while not self._closing:
            ws = self._ws
            if ws is None:
                break
            try:
                message = ws.recv(timeout=1.0)
            except TimeoutError:
                continue
            except Exception as exc:
                if not self._closing:
                    logger.warning("[控制面] 读取失败: %s", exc)
                    self._connected = False
                break

            if isinstance(message, bytes):
                continue
            # 可能一次收到多行 JSON
            for line in message.split("\n"):
                line = line.strip()
                if not line:
                    continue
                self._handle_message(line)

    def _handle_message(self, raw: str) -> None:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[控制面] 收到非 JSON: %s", raw[:200])
            return
        if not isinstance(obj, dict):
            return

        ty = obj.get("ty", "")

        # 状态推送: {"ty": "publish/robot/<臂名>/status", "db": {...}}
        if ty.startswith("publish/"):
            db = obj.get("db") or {}
            if isinstance(db, dict):
                if "/status" in ty:
                    topic = ty[len("publish/") :]
                    if topic.startswith("robot/") and topic.endswith("/status"):
                        name = topic[len("robot/") : -len("/status")]
                        db = dict(db)
                        db["name"] = name
                if self._on_state is not None:
                    self._on_state(db)
            return

        # 请求-响应: 用 id 匹配在途请求
        if "id" in obj:
            msg_id = int(obj["id"])
            # 同步请求: 唤醒等待方 (安全门阀用)
            with self._lock:
                slot = self._sync_slots.pop(msg_id, None)
            if slot is not None:
                slot["response"] = obj
                slot["event"].set()
                return
            with self._lock:
                action = self._pending.pop(msg_id, "")
            if action and self._on_event is not None:
                self._on_event(action, obj)

    # ------------------------------------------------------------ 同步请求

    def request_sync(
        self,
        ty: str,
        db: Optional[Dict[str, Any]] = None,
        timeout: float = 3.0,
    ) -> Optional[Dict[str, Any]]:
        """发一条命令并**等应答**。

        返回完整的响应报文 (含 ``db`` / ``err``); 超时或未连接返回 None。

        安全门阀要用它拿到「当下的」状态快照 —— 状态推送是异步的, 且只在
        订阅周期内到达, 不能用来做即时判定。
        """
        if not self._connected or self._ws is None:
            logger.warning("[控制面] 未连接, 同步请求 %s 失败", ty)
            return None

        event = threading.Event()
        with self._lock:
            msg_id = self._message_id
            self._message_id += 1
            slot: Dict[str, Any] = {"event": event, "response": None}
            self._sync_slots[msg_id] = slot

        payload = {"id": msg_id, "ty": ty, "db": db or {}}
        try:
            self._ws.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        except Exception as exc:
            with self._lock:
                self._sync_slots.pop(msg_id, None)
            logger.warning("[控制面] 同步发送 %s 失败: %s", ty, exc)
            return None

        if not event.wait(timeout):
            with self._lock:
                self._sync_slots.pop(msg_id, None)
            logger.warning("[控制面] 同步请求 %s 超时 (%.1fs)", ty, timeout)
            return None

        return slot["response"]
    # ------------------------------------------------------------ 发命令

    def send_command(self, ty: str, db: Optional[Dict[str, Any]] = None) -> int:
        """发送一条命令, 返回消息 id (失败返回 -1)。"""
        if not self._connected or self._ws is None:
            logger.warning("[控制面] 未连接, 丢弃命令 %s", ty)
            return -1

        with self._lock:
            msg_id = self._message_id
            self._message_id += 1
            self._pending[msg_id] = ty

        payload = {"id": msg_id, "ty": ty, "db": db or {}}
        try:
            self._ws.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        except Exception as exc:
            logger.warning("[控制面] 发送 %s 失败: %s", ty, exc)
            with self._lock:
                self._pending.pop(msg_id, None)
            return -1
        logger.debug("[控制面] -> %s", payload)
        return msg_id

    def subscribe_state(self, robot_name: str, tc_ms: int = 50) -> bool:
        """订阅某臂的状态推送。订阅字段放顶层, 不在 db 内。"""
        if not self._connected or self._ws is None:
            return False
        payload = {
            "id": self._next_id(),
            "ty": "robotState/sub",
            "topics": f"robot/{robot_name}/status",
            "tc": tc_ms,
        }
        try:
            self._ws.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        except Exception as exc:
            logger.warning("[控制面] 订阅 %s 失败: %s", robot_name, exc)
            return False
        logger.info("[控制面] 已订阅 %s 状态 (tc=%dms)", robot_name, tc_ms)
        return True

    def _next_id(self) -> int:
        with self._lock:
            msg_id = self._message_id
            self._message_id += 1
        return msg_id

    # ------------------------------------------------------------ 便捷动作

    def list_robots(self) -> None:
        self.send_command("robot/list", {})

    def get_robot_status(self, name: str) -> None:
        self.send_command("robot/status", {"name": name})

    def enable_robot(self, name: str) -> None:
        self.send_command("robot/enable", {"name": name})

    def disable_robot(self, name: str) -> None:
        self.send_command("robot/disable", {"name": name})

    def get_realtime_status(self) -> None:
        """查询 realtime 会话状态 (端口 / 会话列表 / 推送目标)。"""
        self.send_command("realtime/getStatus", {"robot": None})

    def start_control(
        self, config: Dict[str, Any], robots: Optional[List[str]] = None
    ) -> int:
        """入控。config 是 realtime/startControl 的 config 段。

        ``robots`` 是要入控的机器人名。多臂机**必须**显式指定 ——
        控制器对多台机器人省略 ``config.robot`` 直接拒绝 (24050)。
        """
        payload: Dict[str, Any] = {"config": dict(config)}
        if robots:
            payload["config"]["robot"] = (
                robots[0] if len(robots) == 1 else list(robots)
            )
        return self.send_command("realtime/startControl", payload)

    def stop_control(self, robots: List[str]) -> int:
        """停控并交还规划通道。只有 stopControl 才交还通道。"""
        db: Dict[str, Any] = {}
        if len(robots) == 1:
            db["robot"] = robots[0]
        elif robots:
            db["robot"] = robots
        else:
            db["robot"] = None
        return self.send_command("realtime/stopControl", db)


def _host_from_url(url: str) -> str:
    """从 ws://host:port 里取 host。"""
    try:
        rest = url.split("://", 1)[1]
        return rest.split("/", 1)[0].rsplit(":", 1)[0]
    except Exception:
        return ""


def _supports_proxy_arg(connect) -> bool:
    """判断当前 websockets 版本的 connect() 是否支持 proxy 参数。

    websockets >= 14 才有这个参数; 老版本传了会 TypeError。
    """
    try:
        import inspect

        return "proxy" in inspect.signature(connect).parameters
    except Exception:
        return False
