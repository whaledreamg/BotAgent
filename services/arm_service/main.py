"""机械臂动作服务 (FastAPI)。

启动::

    conda run -n botagent python main.py
    # 或
    conda run -n botagent uvicorn main:app --host 0.0.0.0 --port 8802

接口:
    GET  /health        服务与控制器状态
    GET  /v1/actions    动作库清单
    POST /v1/play       下发播放 (异步)
    POST /v1/stop       停止并交还通道
    GET  /v1/status     当前播放状态
    WS   /ws            阶段与进度推送
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from typing import Any, Dict, Optional

now_dir = os.getcwd()
if now_dir not in sys.path:
    sys.path.append(now_dir)

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from config import load_config  # noqa: E402
from library import ActionNotFound  # noqa: E402
from models import (  # noqa: E402
    ActionInfo,
    ActionListResponse,
    HealthResponse,
    PlayRequest,
    PlayResponse,
    SafetyResponse,
    StatusResponse,
    StopResponse,
)
from robot.controller import ControllerError  # noqa: E402
from service import ArmService  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("arm.main")

service: Optional[ArmService] = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global service
    cfg = load_config()
    service = ArmService(cfg)
    await service.start()
    logger.info(
        "[服务] 已启动, 动作库 %d 个动作, 控制器 %s (%s)",
        service.library.count,
        cfg.arm.controller_ws,
        "已连接" if service._conn.connected else "未连接",
    )
    try:
        yield
    finally:
        if service is not None:
            service.shutdown()
            logger.info("[服务] 已关闭")


app = FastAPI(title="Arm Action Service", version="1.0.0", lifespan=lifespan)


def _svc() -> ArmService:
    if service is None:
        raise HTTPException(status_code=503, detail="服务尚未初始化")
    return service


@app.get("/health", response_model=HealthResponse)
async def health() -> Dict[str, Any]:
    return _svc().health()


@app.get("/v1/actions", response_model=ActionListResponse)
async def list_actions() -> Dict[str, Any]:
    svc = _svc()
    items = svc.library.list_actions()
    return {
        "count": svc.library.count,
        "files": svc.library.files,
        "labels": svc.library.labels(),
        "actions": [ActionInfo(**item) for item in items],
    }


@app.post("/v1/play", response_model=PlayResponse)
async def play(req: PlayRequest) -> Dict[str, Any]:
    """下发播放。

    传 ``action`` 播一个动作, 传 ``actions`` 播一串 (按顺序, 只入控一次)。
    """
    svc = _svc()
    actions = req.action_list()
    if not actions:
        raise HTTPException(status_code=400, detail="没指定动作")
    try:
        return svc.play_many(actions, speed=req.speed, loop=req.loop)
    except ActionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ControllerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/v1/stop", response_model=StopResponse)
async def stop() -> Dict[str, Any]:
    return _svc().stop()


@app.get("/v1/status", response_model=StatusResponse)
async def status() -> Dict[str, Any]:
    return _svc().status()


@app.get("/v1/safety", response_model=SafetyResponse)
async def safety() -> Dict[str, Any]:
    """跑一次安全门阀检查 (不执行任何动作)。

    供上层在动手前先探一下 —— 比如 agent 收到「挥手」时先问一句,
    有异常就直接播报原因, 不用等真的下发失败。
    """
    report = _svc().check_safety()
    return report.to_dict()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """推送阶段与进度。"""
    await websocket.accept()

    queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
    svc = _svc()
    svc.subscribe(queue)

    async def sender() -> None:
        while True:
            message = await queue.get()
            await websocket.send_json(message)

    sender_task = asyncio.create_task(sender())

    try:
        await websocket.send_json(
            {
                "type": "ready",
                "phase": svc.status()["phase"],
                "actions": svc.library.count,
            }
        )
        while True:
            raw = await websocket.receive_text()
            # 客户端可以发 {"type":"ping"} 保持连接
            if raw.strip() == '{"type":"ping"}':
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover
        logger.info("[WS] 连接结束: %s", exc)
    finally:
        svc.unsubscribe(queue)
        sender_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender_task
        with contextlib.suppress(Exception):
            await websocket.close()


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
    )
