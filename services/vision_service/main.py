"""视觉服务 (FastAPI)。

用 Orbbec 相机采集 RGBD, YOLO 做物体识别与姿态估计,
再通过深度反算出目标的三维位置。

启动::

    conda run -n yolo python main.py
    # 或
    conda run -n yolo uvicorn main:app --host 0.0.0.0 --port 8803

接口:
    GET  /health             服务、相机、模型状态
    GET  /v1/status          最近一次检测摘要
    POST /v1/detect          触发一次检测
    GET  /v1/objects         最近一次检测结果 (缓存)
    GET  /v1/objects/{name}  按类别名查目标
    GET  /v1/summary         按类别聚合的摘要
    WS   /ws                 持续推送检测结果
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

from camera import CameraError  # noqa: E402
from config import load_config  # noqa: E402
from detector import DetectorError  # noqa: E402
from models import (  # noqa: E402
    DetectRequest,
    DetectResponse,
    HealthResponse,
    StatusResponse,
)
from service import VisionService  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vision.main")

service: Optional[VisionService] = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/关闭时管理服务。"""
    global service

    cfg = load_config()
    service = VisionService(cfg)
    # 相机/模型失败不阻止服务启动 —— /health 会如实反映状态
    await asyncio.to_thread(service.start)
    logger.info("视觉服务就绪 (端口 %s)", cfg.server.port)

    try:
        yield
    finally:
        if service is not None:
            await asyncio.to_thread(service.stop)
        logger.info("视觉服务已停止")


app = FastAPI(
    title="视觉服务",
    description="Orbbec RGBD + YOLO: 物体识别与位姿估计",
    version="1.0.0",
    lifespan=lifespan,
)


def _svc() -> VisionService:
    if service is None:
        raise HTTPException(status_code=503, detail="服务还没启动")
    return service


def _extrinsic_warnings(st: Dict[str, Any], ext: Any) -> list:
    """外参配置里的「坑」提醒 —— 让问题在 /v1/calibration 里一眼看到。

    **头会动但没开头部补偿**是最典型的坑: 标定出来的 T_world_cam 是
    「头在标定那一刻的位置」下的世界坐标, 头一转就过时了, 而且
    **不会报错** —— 只会默默给错坐标。
    """
    warnings: list = []
    if not st.get("extrinsic_configured"):
        return warnings

    matrix_frame = st.get("extrinsic_matrix_frame", "world")
    head_available = bool(st.get("head_frame_available"))
    head_comp = bool(st.get("head_compensation"))

    if matrix_frame == "world" and head_available and not head_comp:
        warnings.append(
            "标定矩阵是 T_world_cam (绝对量), 但控制器给了头部位姿 —— "
            "如果头会动, 改用 matrix_frame=head 并重新标定 T_head_cam, "
            "否则头一转坐标就过时了 (且不会报错)"
        )
    if matrix_frame == "head" and not head_available:
        warnings.append(
            "matrix_frame=head 但控制器没给头部位姿 (headFrame) —— "
            "坐标算不出来, 会退回相机系"
        )
    if not st.get("calibrated_intrinsics"):
        warnings.append(
            "内参没标定 —— 用的是 SDK 标称值, 1.5 m 处可能有 15 mm 误差"
        )
    if matrix_frame == "world" and head_comp:
        warnings.append("matrix_frame=world 与 head_compensation 矛盾")
    return warnings


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """健康检查。"""
    return HealthResponse(**_svc().status())


@app.get("/v1/status", response_model=StatusResponse)
async def status() -> StatusResponse:
    """当前状态 + 最近一次检测摘要。"""
    svc = _svc()
    st = svc.status()
    st["summary"] = svc.summarize()
    return StatusResponse(**st)


@app.post("/v1/detect", response_model=DetectResponse)
async def detect(req: DetectRequest) -> DetectResponse:
    """触发一次检测。"""
    svc = _svc()
    try:
        result = await asyncio.to_thread(
            svc.detect,
            classes=req.classes,
            conf=req.conf,
            pose=req.pose,
            frame=req.frame,
        )
    except (CameraError, DetectorError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return DetectResponse(**result)


@app.get("/v1/objects")
async def objects() -> Dict[str, Any]:
    """最近一次检测结果 (缓存)。"""
    svc = _svc()
    return {
        "objects": svc.last_objects(),
        "summary": svc.summarize(),
    }


@app.get("/v1/objects/{name}")
async def find_object(name: str, frame: str = "camera") -> Dict[str, Any]:
    """按类别名查目标。

    `name` 可以是 YOLO 的英文类名 ("cup"), 也可以是中文 ("杯子")。

    `frame` 指定坐标系:
    - `camera`      相机坐标系 (默认, 总是可用)
    - `world`       世界坐标系 (需要完成手眼标定)
    - `arm_base`    机械臂基座坐标系 (按配置里的 arm 选左/右)
    - `left_base`   左臂基座坐标系 (需要运行时基座位姿)
    - `right_base`  右臂基座坐标系

    变换不了时会退回相机系并如实标注 —— **不假装是基座坐标**。

    缓存里没有时会现场跑一次检测 —— 服务刚起时缓存是空的。
    """
    svc = _svc()
    hits = await asyncio.to_thread(svc.find, name, frame)
    return {
        "name": name,
        "found": bool(hits),
        "count": len(hits),
        "objects": hits,
        "summary": svc.summarize(hits),
    }


@app.get("/v1/summary")
async def summary() -> Dict[str, Any]:
    """按类别聚合的摘要。"""
    svc = _svc()
    return {"summary": svc.summarize()}


@app.get("/v1/frames")
async def frames(x: float = 0.0, y: float = 0.0, z: float = 1.0) -> Dict[str, Any]:
    """一次给出所有坐标系下的同一个点 —— 调试标定用。

    默认取相机系正前方 1 米的点; 也可以直接传 x/y/z。

    返回::

        {
          "camera":    {"x":.., "y":.., "z":..},
          "world":     {"x":.., "y":.., "z":..},
          "left_base": {"x":.., "y":.., "z":..},
          "sources":   {"world": "static_matrix", "left_base": "base_frame:left"},
          "unavailable": ["right_base"]
        }

    **变换不了的系会进 `unavailable`** —— 宁可少给一个系, 也不给错的。
    """
    svc = _svc()
    await asyncio.to_thread(svc._refresh_world_context)
    return svc.frame_snapshot((x, y, z))


@app.get("/v1/calibration")
async def calibration() -> Dict[str, Any]:
    """标定状态。

    看一眼就知道「现在报的坐标可不可信」:
    - ``intrinsics.calibrated``: 内参是不是标定过的 (没标的话外参也不准)
    - ``extrinsic.configured``: 外参有没有配
    - ``extrinsic.ready``: 现在能不能真的变换过去 (动态外参要看基座位姿拿到没)
    """
    svc = _svc()
    st = svc.status()
    ext = getattr(svc.cfg, "extrinsic", None)
    return {
        "intrinsics": {
            "calibrated": bool(st.get("calibrated_intrinsics")),
            "note": (
                "内参未标定 —— 用的是 SDK 标称值, 1.5 m 处可能有 15 mm 误差"
                if not st.get("calibrated_intrinsics")
                else "已用标定内参"
            ),
        },
        "extrinsic": {
            "configured": bool(st.get("extrinsic_configured")),
            "dynamic": bool(st.get("extrinsic_dynamic")),
            "ready": bool(st.get("extrinsic_dynamic_ready", True)),
            "target_frame": getattr(ext, "target_frame", "camera"),
            "matrix_frame": st.get("extrinsic_matrix_frame", "world"),
            "arms_available": st.get("arms_available", []),
            "head_compensation": bool(st.get("head_compensation")),
            "head_frame_available": bool(st.get("head_frame_available")),
            "note": (
                "外参未配置 —— 只能报相机坐标系"
                if not st.get("extrinsic_configured")
                else "外参已配置"
            ),
            "warnings": _extrinsic_warnings(st, ext),
        },
        "frame": st.get("frame", "camera"),
    }


@app.websocket("/ws")
async def ws_stream(websocket: WebSocket) -> None:
    """持续推送检测结果。

    客户端连上后, 服务按固定间隔检测并推送; 客户端断开就停。
    """
    await websocket.accept()
    svc = _svc()
    interval = 1.0

    async def push_loop() -> None:
        while True:
            try:
                result = await asyncio.to_thread(svc.detect)
                await websocket.send_json(
                    {
                        "type": "detection",
                        "objects": result["objects"],
                        "latency_ms": result["latency_ms"],
                        "frame": result["frame"],
                    }
                )
            except (CameraError, DetectorError) as exc:
                await websocket.send_json({"type": "error", "message": str(exc)})
            except Exception as exc:  # pragma: no cover
                await websocket.send_json({"type": "error", "message": str(exc)})
            await asyncio.sleep(interval)

    task = asyncio.create_task(push_loop())
    try:
        while True:
            # 客户端可以发消息来改间隔
            msg = await websocket.receive_text()
            if msg == "stop":
                break
            try:
                interval = max(0.2, float(msg))
            except ValueError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@app.exception_handler(Exception)
async def unhandled(request, exc: Exception) -> JSONResponse:  # pragma: no cover
    logger.exception("未处理异常")
    return JSONResponse(
        status_code=500,
        content={"status": "error", "detail": str(exc)},
    )


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "main:app",
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
    )
