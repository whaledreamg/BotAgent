"""进程内事件总线 (只读监控用)。

Agent 服务内部各处把「思考过程」「分流结论」等事件发到这里,
监控端 (WS 客户端) 订阅后实时收到。

设计要点:
  - **不阻塞主链路**: publish 只做一次 `put_nowait`, 绝不等待订阅者。
  - **慢客户端不拖垮服务**: 每个订阅者一个有界队列, 满了丢最旧的一条。
  - **线程安全**: Agent 主逻辑在事件循环里, 但 ThinkingLogger 等可能在
    别的线程调用, publish 用 `call_soon_threadsafe` 跨线程投递。

事件信封 (统一格式):

    {
        "type": "turn_start | thinking | turn_end | error | hello",
        "ts": "2026-11-18T14:05:12.788123",
        "session_id": "robot-01",
        "turn_id": "3f2a9c1b7e04",     # 每轮对话唯一 (分轮用)
        "payload": { ... }        # 各事件自定义
    }

**分轮靠 `turn_id`, 不靠 `session_id`。**

`session_id` 是常驻的会话标识 (比如 `robot-01`), 用户说一百句话它也不变;
而 `turn_id` 每一轮对话生成一个新的。客户端要区分「这是第几轮」必须看
`turn_id` —— 只看 `session_id` 的话所有轮次会塌成第一轮。
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agent.event_bus")

#: 每个订阅者的队列容量 —— 满了丢最旧的, 不让慢客户端把内存吃光
DEFAULT_QUEUE_SIZE = 256


def _now_iso() -> str:
    return datetime.now().isoformat()


def new_turn_id() -> str:
    """生成一个本轮对话的唯一 id。

    每一轮对话生成一个, 客户端据此把事件归到对应的轮次。
    """
    return uuid.uuid4().hex[:12]


#: 当前协程/线程正在处理的那一轮的 turn_id。
#:
#: 用 contextvars 而不是普通全局变量 —— asyncio 里同一时刻可能有多个
#: 请求在跑, 普通全局变量会被别的请求串味。
#:
#: ``publish()`` 在没有显式传 turn_id 时会读它, 所以只要在一轮对话
#: 开始时 set 一次, 后续所有 publish 自动带上同一个 turn_id。
_current_turn: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "agent_current_turn", default=None
)


def set_current_turn(turn_id: str) -> None:
    """把当前轮次的 turn_id 记下来 (本轮后续 publish 都会带上它)。"""
    _current_turn.set(turn_id)


def clear_current_turn() -> None:
    """清掉当前轮次标记。"""
    _current_turn.set(None)


def get_current_turn() -> Optional[str]:
    """取当前轮次的 turn_id (没有则 None)。"""
    return _current_turn.get()


class EventBus:
    """极简的发布订阅中心。

    典型用法::

        from event_bus import bus

        queue = bus.subscribe()
        bus.publish("thinking", {"skill": "weather"}, session_id="robot-01")
        ...
        bus.unsubscribe(queue)
    """

    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: List[asyncio.Queue] = []
        self._lock = threading.Lock()
        #: publish 时记下当前事件循环, 供跨线程投递用
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------ 订阅

    def subscribe(self) -> asyncio.Queue:
        """订阅事件流, 返回一个只属于本订阅者的队列。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers.append(queue)
        logger.debug("[事件总线] 新增订阅者, 当前订阅数=%d", self.count)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """退订。传回 subscribe() 拿到的那个队列即可。"""
        with self._lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass
        logger.debug("[事件总线] 移除订阅者, 当前订阅数=%d", self.count)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # ------------------------------------------------------------ 发布

    def publish(
        self,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        session_id: str = "",
        turn_id: str = "",
        **extra: Any,
    ) -> Dict[str, Any]:
        """广播一个事件。

        Args:
            event_type: 事件类型 ("turn_start" / "thinking" / "turn_end" / ...)。
            payload:   事件内容, 原样放进信封的 payload 字段。
            session_id: 会话 id (常驻, 比如 robot-01) —— **不能用来分轮**。
            turn_id:    本轮对话的唯一 id。显式传就用它; 不传就取
                        当前轮次上下文里的 (set_current_turn 设的);
                        都没有则留空 (客户端会回落到 turn_start 边界分轮)。
            extra:     额外字段, 平铺到信封顶层。

        Returns:
            构造好的事件信封 (即使没有订阅者也照常构造, 方便打日志)。
        """
        resolved_turn = turn_id or get_current_turn() or ""

        event: Dict[str, Any] = {
            "type": event_type,
            "ts": _now_iso(),
            "session_id": session_id,
            "turn_id": resolved_turn,
            "payload": payload or {},
        }
        if extra:
            event.update(extra)

        with self._lock:
            subscribers = list(self._subscribers)
            loop = self._loop

        for queue in subscribers:
            self._put(queue, event, loop)
        return event

    def _put(
        self,
        queue: asyncio.Queue,
        event: Dict[str, Any],
        loop: Optional[asyncio.AbstractEventLoop],
    ) -> None:
        """把事件塞进订阅者的队列 (跨线程安全)。"""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        # 已经在事件循环线程里 —— 直接塞
        if running is not None:
            self._put_nowait(queue, event)
            return

        # 不在事件循环线程 —— 跨线程投递
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._put_nowait, queue, event)
                return
            except RuntimeError:  # pragma: no cover - loop 正在关闭
                pass
        # 没有可用的 loop (比如服务还没起来) —— 同步塞, asyncio.Queue 在
        # 没运行 loop 时 put_nowait 也不会炸 (它只在满了时抛 QueueFull)
        self._put_nowait(queue, event)

    @staticmethod
    def _put_nowait(queue: asyncio.Queue, event: Dict[str, Any]) -> None:
        """无阻塞入队; 队列满了丢最旧的一条。"""
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()   # 丢最旧
                queue.put_nowait(event)
            except Exception:  # pragma: no cover - 极端情况直接放弃
                logger.warning("[事件总线] 订阅队列已满, 丢弃一条事件")

    # ------------------------------------------------------------ 生命周期

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定事件循环 (服务启动时调用), 供跨线程 publish 使用。"""
        self._loop = loop

    def unbind_loop(self) -> None:
        self._loop = None


#: 进程内单例
bus = EventBus()


__all__ = [
    "EventBus",
    "bus",
    "DEFAULT_QUEUE_SIZE",
    "new_turn_id",
    "set_current_turn",
    "clear_current_turn",
    "get_current_turn",
]
