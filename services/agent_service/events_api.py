"""实时事件推送 API (只读)。

给监控端 (Qt APP) 订阅用: 连接上之后, Agent 每处理一轮对话就会把
「分流结论」「模型的思考」「参数提取的推理」实时推过来。

**只读** —— 本接口不接收任何客户端指令, 也下发不了任何控制动作。
WS 只往外推。

协议:
    连接:  ws://<host>:8800/v1/events/
    握手:  服务端先发一条 {"type": "hello", ...}
    之后:  每次 Agent 处理一轮, 推若干条事件

事件信封:

    {
        "type": "hello | turn_start | thinking | turn_end | error",
        "ts": "2026-11-18T14:05:12.788123",
        "session_id": "robot-01",
        "turn_id": "3f2a9c1b7e04",   # 每轮对话唯一 (分轮靠它)
        "payload": { ... }          # 各事件自定义
    }
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from event_bus import bus

logger = logging.getLogger("agent.events_api")

router = APIRouter(prefix="/v1/events", tags=["events"])

#: 客户端多久没收到任何东西就算掉线 (秒)
WS_IDLE_TIMEOUT = 300.0


def _now_iso() -> str:
    return datetime.now().isoformat()


@router.websocket("/")
async def events_socket(websocket: WebSocket) -> None:
    """事件推送。

    连上之后一直保持, 客户端断开就退订。不接受客户端发来的任何消息
    (收到也只当作心跳忽略)。
    """
    await websocket.accept()
    queue = bus.subscribe()
    logger.info("[事件推送] 客户端已连接, 当前订阅数=%d", bus.count)

    try:
        # 握手 —— 让客户端确认连的是对的服务
        await websocket.send_json(
            {
                "type": "hello",
                "ts": _now_iso(),
                "session_id": "",
                "turn_id": "",
                "payload": {
                    "server": "agent",
                    "protocol": 1,
                    "subscribers": bus.count,
                },
            }
        )

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=WS_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                # 长时间没事件 —— 发个心跳, 让客户端知道连接还活着
                await websocket.send_json(
                    {
                        "type": "heartbeat",
                        "ts": _now_iso(),
                        "session_id": "",
                        "turn_id": "",
                        "payload": {},
                    }
                )
                continue

            await websocket.send_json(event)

    except WebSocketDisconnect:
        logger.info("[事件推送] 客户端断开")
    except Exception as exc:  # pragma: no cover - 网络异常兜底
        logger.warning("[事件推送] 连接异常: %s", exc)
    finally:
        bus.unsubscribe(queue)
        logger.info("[事件推送] 已退订, 当前订阅数=%d", bus.count)


__all__ = ["router"]
