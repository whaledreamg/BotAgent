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
from typing import Any, Dict, List, Optional

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
    MoveJointsRequest,
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


@app.get("/v1/flows")
async def list_flows() -> Dict[str, Any]:
    """列出所有流程。

    流程定义在 ``configs/flows/*.yaml`` —— 每个文件一条流程, 文件名即
    流程名。抓取流程不再是写死的 5 步, 而是这里声明的步骤列表。

    **热重载**: 每次调用都会比对目录指纹 (mtime + 大小) —— yaml 改了
    就重读, 没改就用缓存。所以改完 yaml 直接调这个接口就能看到新版本。
    """
    svc = _svc()
    flows = svc.load_flows()
    items = []
    for name in sorted(flows):
        flow = flows[name]
        items.append({
            "name": flow.name,
            "description": flow.description,
            "step_count": len(flow.steps),
            "steps": flow.step_ids,
            "source": flow.source,
        })
    return {"count": len(items), "flows": items}


@app.post("/v1/flows/reload")
async def reload_flows() -> Dict[str, Any]:
    """**强制重载**流程定义 (忽略缓存)。

    通常**不用**手动调 —— 每次 ``GET /v1/flows`` 和每次抓取都会自动
    比对目录指纹, 改了 yaml 就自动生效。

    什么时候需要手动调:

    - yaml 的 mtime 和大小都没变 (同秒内改的), 自动重载可能漏掉
    - 想确认当前用的是哪一版
    """
    svc = _svc()
    flows = svc.load_flows(force=True)
    return {
        "ok": True,
        "count": len(flows),
        "flows": sorted(flows),
        "signature": [list(item) for item in svc.flows_signature()],
    }


@app.get("/v1/flows/{name}")
async def get_flow(name: str) -> Dict[str, Any]:
    """拿一条流程的完整定义 (含每一步的参数)。

    ``defaults`` 是从 ``configs/arm.yaml`` 来的兜底值 —— 步骤里没写
    ``action`` / ``duration`` 时用它们。
    """
    svc = _svc()
    flow = svc.get_flow(name)
    if flow is None:
        raise HTTPException(
            status_code=404,
            detail=f"没有流程 {name!r} (可用: {', '.join(svc.flow_names())})",
        )
    cfg = svc.arm_cfg
    return {
        **flow.to_dict(),
        "approach_distance": cfg.flow.above_distance,
        "defaults": {
            "close_action": cfg.flow.close_action,
            "open_action": cfg.flow.open_action,
            "move_duration": cfg.grasp.duration,
        },
    }


@app.get("/v1/flow")
async def flow_config() -> Dict[str, Any]:
    """默认流程 (``grasp``) 的配置。

    **保留这个旧端点** —— 早期客户端直接读这里的扁平字段
    (``above_distance`` / ``grasp_offset_*`` / ``close_action`` …)。

    新代码建议用 ``GET /v1/flows/{name}`` —— 那份带**完整的步骤定义**,
    而这里只有扁平参数 (没有步骤列表, 也就没法自定义流程)。

    返回的 ``flow`` 字段里带默认流程的步骤定义, 这样老客户端一次调用
    也能拿到, 不用多发一次请求。
    """
    svc = _svc()
    f = svc.arm_cfg.flow
    return {
        "ready_action": f.ready_action,
        "safe_action": f.safe_action,
        "above_distance": f.above_distance,
        "grasp_offset_x": f.grasp_offset_x,
        "grasp_offset_y": f.grasp_offset_y,
        "grasp_offset_z": f.grasp_offset_z,
        "close_action": f.close_action,
        "open_action": f.open_action,
        "above_duration": f.above_duration,
        "grasp_duration": f.grasp_duration,
        # 新增: 默认流程的完整定义
        "flow_name": svc.default_flow().name,
        "flow": svc.default_flow().to_dict(),
    }


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

    ``wait=true`` 时**等播完再返回** —— 抓取流程要按顺序走, 下一步必须
    等上一步真的动完了才能开始。
    """
    svc = _svc()
    actions = req.action_list()
    if not actions:
        raise HTTPException(status_code=400, detail="没指定动作")
    try:
        if req.wait:
            # 播放要几秒到几十秒, 必须放线程池, 否则事件循环被卡住
            return await await_run(
                svc.play_many, actions, req.speed, req.loop, True
            )
        return svc.play_many(actions, speed=req.speed, loop=req.loop)
    except ActionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ControllerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/v1/move_to", response_model=MoveToResponse)
async def move_to(req: MoveToRequest) -> Dict[str, Any]:
    """把 TCP 移到世界系目标点 (同步, 走完才返回)。

    流程: 安全门阀 -> 臂展粗检 -> **本地逆解 (pinocchio)** ->
    关节流下发 -> 读回 TCP 核对。被拒绝或没到位时 ``ok=false``,
    ``message`` 是可以直接播报的原因。HTTP 状态码恒为 200, 成败看 ``ok``。

    **不用控制器的逆解接口** —— 逆解在本地做, 控制器全程只吃关节角
    (``cartesianIkPath="none"``)。
    """
    svc = _svc()
    # 移动会阻塞到走完 (几秒), 必须放线程池, 否则事件循环被卡住
    return await await_run(
        svc.move_to,
        req.arm, req.x, req.y, req.z, req.a, req.b, req.c, req.duration, req.speak,
    )


@app.post("/v1/move_joints", response_model=MoveToResponse)
async def move_joints(req: MoveJointsRequest) -> Dict[str, Any]:
    """把某条臂**直接**移到给定的关节角 (同步, 走完才返回)。

    **不走逆解** —— 目标关节角是调用方直接给的。给流程里的
    ``arm`` + ``pose`` 片段用 (目标关节角写死在 YAML 里, 没必要再解)。

    流程: 安全门阀 -> 关节流下发 -> 读回实测角核对。
    HTTP 状态码恒为 200, 成败看 ``ok``。
    """
    svc = _svc()
    return await await_run(
        svc.move_joints, req.arm, req.joints, req.duration, req.speak,
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


class HandRequest(BaseModel):
    """灵巧手指令。"""

    #: 只动哪只手; 省略 = 两只都动
    side: Optional[str] = None
    #: 手位 (6 个 0~1000); 省略 = 用预设
    positions: Optional[List[int]] = None
    #: 预设手势名 (一把抓 / 释放 / 半抓 / 两指捏 / 点赞 / 指点)
    preset: Optional[str] = None


class SequenceRequest(BaseModel):
    """动作序列合成。"""

    #: 要播的动作名, 按执行顺序
    actions: List[str] = []
    #: 相邻动作之间的停顿 (秒)
    gap: float = 0.0
    #: 等播完再返回
    wait: bool = True


class GraspSequenceRequest(BaseModel):
    """识别之后合成抓取动作序列。"""

    #: 目标位置 (世界系, 米) —— 识别服务给的
    x: float
    y: float
    z: float
    #: 用哪条臂
    side: str = "right"
    #: 流程名 (configs/flows/ 里的)
    flow: str = "grasp"
    #: 上方点的高度 (米); 不给用配置值
    approach_distance: Optional[float] = None
    #: 统一覆盖所有移动步的时长 (秒)
    duration: Optional[float] = None
    #: 只合成不执行
    plan_only: bool = False
    #: 等播完再返回
    wait: bool = True


@app.post("/v1/sequence", response_model=PlayResponse)
async def play_sequence(req: SequenceRequest) -> Dict[str, Any]:
    """把几个动作合成**一条**时间轴, 一次入控播完。

    夹爪片段不在这条时间轴上直接下发 (实时指令包里没有手部字段),
    而是记成时间点, 播放到那个时刻再走 ``rs485/*`` 透传。

    ```bash
    curl -X POST localhost:8802/v1/sequence \\
      -d '{"actions": ["准备点", "闭合夹爪", "安全点"]}'
    ```
    """
    svc = _svc()
    if not req.actions:
        raise HTTPException(status_code=400, detail="没指定动作")
    try:
        if req.wait:
            return await await_run(
                lambda: svc.play_sequence(
                    req.actions, wait=True, gap=req.gap
                )
            )
        return svc.play_sequence(req.actions, wait=False, gap=req.gap)
    except ActionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ControllerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/v1/grasp_sequence")
async def grasp_sequence(req: GraspSequenceRequest) -> Dict[str, Any]:
    """识别之后, 把流程合成成一条动作序列并执行。

    **顺序**: 目标位置由调用方 (agent 侧的视觉 skill) 先算好传进来 ——
    服务这边只负责「合成 + 执行」。

    流程: 流程定义 -> 步骤清单 (移动步用**本地 pinocchio 逆解**算成关节角)
    -> 一条时间轴 (关节片段) + 若干夹爪时间点。

    ```bash
    curl -X POST localhost:8802/v1/grasp_sequence \\
      -d '{"x": 0.55, "y": -0.30, "z": -0.15, "side": "right"}'
    ```
    """
    svc = _svc()
    try:
        composed, ik_results, arm, arm_side = svc.plan_grasp_sequence(
            req.flow, (req.x, req.y, req.z),
            side=req.side,
            approach_distance=req.approach_distance,
            duration=req.duration,
        )
    except ControllerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    planned = {
        "ok": True,
        "sequence": composed.to_dict(),
        "ik": ik_results,
        "arm": arm,
        "side": arm_side,
    }

    if req.plan_only:
        return planned

    try:
        if req.wait:
            svc._play_composed(composed)
            planned["finished"] = True
        else:
            svc._enqueue_sequence(composed)
        return planned
    except ControllerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/v1/hand/status")
async def hand_status() -> Dict[str, Any]:
    """灵巧手状态。

    灵巧手走控制器的 ``rs485/*`` 透传 (Modbus RTU), **不在 UDP 关节包里**
    —— 控制器实时指令包没有 ``hand`` 字段 (见 API 说明 §21.9)。
    """
    return _svc().hand_status()


@app.post("/v1/hand/connect")
async def hand_connect() -> Dict[str, Any]:
    """连接灵巧手 (解析 rs485 通道 + 初始化串口)。

    **幂等** —— 已经连上就直接返回状态。
    """
    svc = _svc()
    try:
        # 走控制器要两三次往返, 放线程池
        return await await_run(svc.connect_hand, True)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/v1/hand/open")
async def hand_open(req: HandRequest) -> Dict[str, Any]:
    """张开手。

    默认用 ``configs/arm.yaml`` 的 ``arm.hand.open_pose``; 给了
    ``positions`` 就用给的那组 (6 个 0~1000)。

    ```bash
    curl -X POST localhost:8802/v1/hand/open -d '{"positions": [0,0,300,300,300,300]}'
    ```
    """
    svc = _svc()
    try:
        ok = await await_run(svc.send_hand_open, req.side, req.positions)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": bool(ok), "action": "open", "side": req.side,
            "positions": req.positions}


@app.post("/v1/hand/close")
async def hand_close(req: HandRequest) -> Dict[str, Any]:
    """握紧手。

    默认用 ``configs/arm.yaml`` 的 ``arm.hand.close_pose``; 给了
    ``positions`` 就用给的那组 (6 个 0~1000)。

    ```bash
    curl -X POST localhost:8802/v1/hand/close \\
        -d '{"positions": [800,0,700,700,700,700]}'
    ```
    """
    svc = _svc()
    try:
        ok = await await_run(svc.send_hand_close, req.side, req.positions)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": bool(ok), "action": "close", "side": req.side,
            "positions": req.positions}


@app.post("/v1/hand/preset")
async def hand_preset(req: HandRequest) -> Dict[str, Any]:
    """下发一个预设手势。

    ```bash
    curl -X POST localhost:8802/v1/hand/preset -d '{"preset": "一把抓"}'
    ```
    """
    if not req.preset:
        raise HTTPException(status_code=400, detail="没指定 preset")
    svc = _svc()
    try:
        ok = await await_run(svc.send_hand_preset, req.preset, req.side)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": bool(ok), "action": "preset", "preset": req.preset,
            "side": req.side}


@app.get("/v1/status", response_model=StatusResponse)
async def status() -> Dict[str, Any]:
    return _svc().status()


@app.post("/v1/stream_trace")
async def stream_trace(
    enable: bool = False, reset: bool = False
) -> Dict[str, Any]:
    """开关/读取关节流诊断记录。

    「轨迹抖动」这类问题要分清是**上游算出的目标在抖**还是**下发环节
    在抖** —— 这个接口记下每一帧实际下发的关节目标, 可以直接对比。

    ```bash
    curl -X POST 'localhost:8802/v1/stream_trace?enable=true&reset=true'
    # ... 跑一次抓取 ...
    curl localhost:8802/v1/stream_trace
    ```
    """
    svc = _svc()
    with svc._lock:
        if reset:
            svc._stream_trace = []
        svc._stream_trace_on = enable
        count = len(svc._stream_trace)
    return {"ok": True, "enabled": enable, "frames": count}


@app.get("/v1/stream_trace")
async def get_stream_trace() -> Dict[str, Any]:
    """取回记录的关节流。"""
    svc = _svc()
    with svc._lock:
        frames = list(svc._stream_trace)
    return {"ok": True, "count": len(frames), "frames": frames}


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
