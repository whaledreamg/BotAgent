"""机械臂动作服务 (FastAPI)。

启动::

    conda run -n botagent python main.py
    # 或
    conda run -n botagent uvicorn main:app --host 0.0.0.0 --port 8802

接口:
    GET  /health        服务与控制器状态
    GET  /v1/actions    动作库清单
    POST /v1/play       下发播放 (异步)
    POST /v1/move_to    TCP 移到世界系目标点 (同步, 经门阀与控制器逆解)
    POST /v1/stop       停止并交还通道
    GET  /v1/status     当前播放状态
    GET  /v1/safety     跑一次安全门阀检查 (不动作)
    GET  /v1/kinematics 运动学快照 (TCP 世界系位姿, 手眼标定用)
    GET  /v1/control_enabled  看控制指令总闸状态
    POST /v1/control_enabled  切换控制指令总闸 (false = 只算不动)
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
from pydantic import BaseModel  # noqa: E402

from config import load_config  # noqa: E402
from library import ActionNotFound  # noqa: E402
from models import (  # noqa: E402
    ActionInfo,
    ActionListResponse,
    HealthResponse,
    KinematicsResponse,
    MoveToRequest,
    MoveToResponse,
    PlayRequest,
    PlayResponse,
    SafetyResponse,
    StatusResponse,
    StopResponse,
)
from robot.controller import ControllerError  # noqa: E402
from service import ArmService, await_run  # noqa: E402

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


@app.post("/v1/move_to", response_model=MoveToResponse)
async def move_to(req: MoveToRequest) -> Dict[str, Any]:
    """把 TCP 移到世界系目标点 (同步, 走完才返回)。

    流程: 安全门阀 -> 臂展粗检 -> 控制器逆解 (cartesianIkPath=analytic) ->
    S 曲线 ``duration`` 秒 -> 读回 TCP 核对。被拒绝或没到位时 ``ok=false``,
    ``message`` 是可以直接播报的原因。HTTP 状态码恒为 200, 成败看 ``ok``。
    """
    svc = _svc()
    # 移动会阻塞到走完 (几秒), 必须放线程池, 否则事件循环被卡住
    return await await_run(
        svc._mover.move_to,
        req.arm, req.x, req.y, req.z, req.a, req.b, req.c, req.duration, req.speak,
    )


@app.post("/v1/stop", response_model=StopResponse)
async def stop() -> Dict[str, Any]:
    return _svc().stop()


class ControlEnabledRequest(BaseModel):
    """控制指令总闸的运行时设置。"""

    #: true = 允许下发控制指令; false = 只算不动; None = 回到配置默认值
    enabled: Optional[bool] = None


@app.get("/v1/control_enabled")
async def get_control_enabled() -> Dict[str, Any]:
    """看当前的控制指令总闸状态。"""
    svc = _svc()
    return {
        "enabled": svc.control_enabled,
        "mode": "control" if svc.control_enabled else "plan_only",
        "description": (
            "会下发控制指令，机械臂会动"
            if svc.control_enabled
            else "不下发任何控制指令，只计算位置"
        ),
    }


@app.post("/v1/control_enabled")
async def set_control_enabled(req: ControlEnabledRequest) -> Dict[str, Any]:
    """切换控制指令总闸 (运行时生效, 不用重启服务)。

    # 只算不动
    curl -X POST localhost:8802/v1/control_enabled -d '{"enabled": false}'

    # 回到配置默认值
    curl -X POST localhost:8802/v1/control_enabled -d '{"enabled": null}'
    """
    svc = _svc()
    svc.set_control_enabled(req.enabled)
    return {
        "enabled": svc.control_enabled,
        "mode": "control" if svc.control_enabled else "plan_only",
    }


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


@app.get("/v1/kinematics", response_model=KinematicsResponse)
async def kinematics(refresh: bool = False) -> Dict[str, Any]:
    """运动学快照 —— 手眼标定要用的位姿数据。

    每条臂给出 TCP 的**世界系**位姿 (`cartesian`)、臂基座在世界系下的
    位姿 (`base_frame`) 和实测关节角。

    `refresh=true` 会**主动**向控制器拉一次最新状态再返回 —— 标定脚本
    每采一个位姿都该用这个, 因为状态推送有 50ms 周期, 快速运动时
    推送值跟不上实际。
    """
    svc = _svc()
    if refresh:
        # `refresh_kinematics` 会阻塞 (等控制器应答), 放到线程池里跑,
        # 别把 FastAPI 的事件循环卡住。
        #
        # **注意**: 必须用 `await_run` (async 版), 不能用 `run_blocking`
        # (同步版) —— 后者直接返回结果, await 它会报
        # "object dict can't be used in 'await' expression"。
        return await await_run(svc.refresh_kinematics)
    return svc.kinematics()


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
