#!/usr/bin/env python3
"""视觉服务自检 —— 一次跑完所有环节。

检查项:
1. 配置能加载
2. 相机能枚举、打开、采帧 (彩色 + 深度 + 内参)
3. YOLO 模型能加载、能推理
4. 3D 解算: 深度反算 + 外参变换
5. HTTP 接口: /health, /v1/detect, /v1/objects/{name}
6. Agent 的 find_object tool 能问到位置

用法::

    conda run -n yolo python tools/check_vision.py
    conda run -n yolo python tools/check_vision.py --skip-agent
    conda run -n yolo python tools/check_vision.py --only solver
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from camera import CameraError, OrbbecCamera  # noqa: E402
from config import load_config  # noqa: E402
from detector import Detector, DetectorError  # noqa: E402
from solver import PoseSolver  # noqa: E402

PASSED: List[str] = []
FAILED: List[str] = []


def check(desc: str, ok: bool, detail: str = "") -> bool:
    if ok:
        PASSED.append(desc)
        print(f"  [PASS] {desc}" + (f"  {detail}" if detail else ""))
    else:
        FAILED.append(desc)
        print(f"  [FAIL] {desc}  {detail}")
    return ok


def section(title: str) -> None:
    print(f"\n[{title}]")


# ------------------------------------------------------------ HTTP
#
# 注意: 本机回环请求 (127.0.0.1) 不应该走代理。某些环境下 HTTP_PROXY 是
# 全局设着的, urllib 会照样把回环请求发给代理, 结果连不上自己。
# 所以这里显式建一个不带代理的 opener。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(url: str, timeout: float = 20.0) -> Tuple[int, Any]:
    try:
        with _OPENER.open(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return 0, str(exc)


def http_post(url: str, payload: Dict[str, Any], timeout: float = 30.0) -> Tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return 0, str(exc)


# ------------------------------------------------------------ 各项检查


def check_config(cfg: Any) -> None:
    section("配置")
    check("配置能加载", cfg is not None)
    check("端口是 8803", cfg.server.port == 8803, str(cfg.server.port))
    check("检测权重已配置", bool(cfg.detector.weights), cfg.detector.weights)
    check(
        "权重文件存在",
        Path(cfg.detector.weights).exists() if cfg.detector.weights else False,
        cfg.detector.weights,
    )


def check_camera(cfg: Any) -> Tuple[Optional[Any], Optional[OrbbecCamera]]:
    section("相机")
    cam = OrbbecCamera(cfg.camera)
    try:
        cam.open()
    except CameraError as exc:
        check("相机能打开", False, str(exc)[:200])
        return None, None
    except Exception as exc:
        # 相机被别的进程占着 (uvc_open failed) —— 不是代码问题
        check("相机能打开", False, f"被占用或异常: {str(exc)[:120]}")
        return None, None

    check("相机能打开", cam.opened)
    info = cam.device_info
    check("拿到设备信息", bool(info.get("name")), json.dumps(info, ensure_ascii=False))
    check(
        "是 Orbbec 设备",
        "orbbec" in str(info.get("name", "")).lower(),
        str(info.get("name")),
    )

    frame = None
    for _ in range(5):
        try:
            frame = cam.read()
        except CameraError:
            break
        if frame is not None:
            break

    if not check("能采到帧", frame is not None):
        cam.close()
        return None, cam

    check("拿到彩色帧", frame.color is not None,
          str(frame.color.shape) if frame.color is not None else "")
    check("彩色帧是 BGR", frame.color is not None
          and frame.color.ndim == 3 and frame.color.shape[2] == 3)
    check("拿到深度帧", frame.depth_raw is not None,
          str(frame.depth_raw.shape) if frame.depth_raw is not None else "")
    check("内参有效", frame.intrinsics.valid, json.dumps(frame.intrinsics.to_dict()))
    check("深度内参有效", frame.depth_intrinsics.valid,
          json.dumps(frame.depth_intrinsics.to_dict()))
    check("depth_scale 换算成米", 0.0001 < frame.depth_scale < 0.01,
          f"{frame.depth_scale} (原始值->米)")

    # ---- 深度/彩色是否对位 ----
    #
    # 这是最关键的一步: 深度图必须跟彩色图**逐像素对齐**, 否则拿彩色图上
    # 的检测框去查深度会查到完全无关的物体。
    #
    # Gemini 336L 不支持硬件 D2C, 靠 SDK 的 AlignFilter 做软件对齐。
    # 对齐后: 分辨率相同、内参相同、外参是单位阵。
    if frame.color is not None and frame.depth_raw is not None:
        ch, cw = frame.color.shape[:2]
        dh, dw = frame.depth_raw.shape
        check("深度图与彩色图同分辨率", (dh, dw) == (ch, cw),
              f"depth {dw}x{dh} vs color {cw}x{ch}")
        check("深度内参与彩色内参一致",
              abs(frame.depth_intrinsics.fx - frame.intrinsics.fx) < 1e-3
              and abs(frame.depth_intrinsics.cx - frame.intrinsics.cx) < 1e-3,
              f"depth fx={frame.depth_intrinsics.fx:.2f} "
              f"color fx={frame.intrinsics.fx:.2f}")
        check("对齐模式已生效", cam.align_mode in ("HW_MODE", "SW_MODE"),
              cam.align_mode)

    if frame.depth_raw is not None:
        d = frame.depth_raw
        valid = d[(d > 0) & (d < 65535)]
        check("深度有有效值", valid.size > 0, f"{valid.size}/{d.size} 像素")
        if valid.size:
            meters = valid.astype(np.float32) * frame.depth_scale
            print(f"       深度范围 {meters.min():.3f} ~ {meters.max():.3f} 米, "
                  f"中位 {np.median(meters):.3f} 米")

    return frame, cam


def check_detector(cfg: Any, frame: Any) -> None:
    section("检测器")
    det = Detector(cfg.detector)
    try:
        det.load()
    except DetectorError as exc:
        check("模型能加载", False, str(exc)[:200])
        return
    check("模型能加载", True, cfg.detector.weights)
    check("有类别表", len(det.class_names) > 0, f"{len(det.class_names)} 类")
    check("设备已确定", det.device in ("cpu", "0") or det.device.isdigit(),
          det.device)

    try:
        results = det.infer(frame.color, conf=0.05)
        check("推理能跑通", True, f"检测到 {len(results)} 个目标")
        for r in results[:5]:
            print(f"         {r.label:16s} conf={r.confidence:.3f} "
                  f"bbox={tuple(round(v) for v in r.bbox)}")
    except DetectorError as exc:
        check("推理能跑通", False, str(exc)[:200])


def check_solver(cfg: Any, frame: Any, cam: Optional[OrbbecCamera]) -> None:
    section("位姿解算")
    solver = PoseSolver(cfg.solver, cfg.extrinsic)
    depth_m = frame.depth_meters

    h, w = frame.color.shape[:2]
    # 选一块「深度一致」的区域做测试 —— 真实场景里框里往往前景背景混着,
    # 那样算出来的真值本身就没有意义。
    #
    # 做法: 在图上扫若干个小窗口, 挑**深度直方图最集中**的那个窗口
    # (也就是一块平整表面), 拿它的众数当真值。
    best = None
    for frac in (0.12, 0.16, 0.20, 0.25):
        bw, bh = int(w * frac), int(h * frac)
        for gy in range(0, h - bh, bh):
            for gx in range(0, w - bw, bw):
                patch = depth_m[gy:gy + bh, gx:gx + bw]
                ok = (patch > 0.1) & (patch < 5.0)
                n = int(ok.sum())
                if n < patch.size * 0.5:
                    continue
                vals = patch[ok]
                # 集中度: 众数附近的像素占比
                mode = float(np.median(vals))
                near = np.abs(vals - mode) <= max(0.02, mode * 0.05)
                focus = float(near.mean())
                if best is None or focus > best[0]:
                    best = (focus, mode, (gx, gy, gx + bw, gy + bh), n)

    if best is None:
        check("找到深度平整的区域", False, "场景里没有足够平整的表面")
        return

    focus, true_z, depth_bbox, n = best
    check("找到深度平整的区域", focus > 0.8,
          f"集中度 {focus * 100:.0f}%, 区域 {depth_bbox}, {n} 像素")
    print(f"       真值深度 {true_z:.3f} m (该区域众数)")

    target = solver.solve(
        depth_m,
        depth_bbox,
        frame.intrinsics,
        depth_intrinsics=frame.depth_intrinsics,
        depth_to_color=frame.depth_to_color,
    )
    print(f"       解算结果 xyz=({target.x:.4f}, {target.y:.4f}, {target.z:.4f}), "
          f"有效={target.depth_valid}, 占比={target.valid_ratio:.2f}")

    err = abs(target.z - true_z)
    check("Z 误差 < 5cm", err < 0.05, f"误差 {err * 100:.1f} cm")
    check("深度标记为有效", target.depth_valid is True)

    intr = frame.intrinsics
    u_c = (depth_bbox[0] + depth_bbox[2]) / 2.0
    v_c = (depth_bbox[1] + depth_bbox[3]) / 2.0
    exp_x = (u_c - intr.cx) * target.z / intr.fx
    exp_y = (v_c - intr.cy) * target.z / intr.fy
    check("X 与像素一致", abs(target.x - exp_x) < 2e-3,
          f"{target.x:.4f} vs {exp_x:.4f}")
    check("Y 与像素一致", abs(target.y - exp_y) < 2e-3,
          f"{target.y:.4f} vs {exp_y:.4f}")


def check_http(base_url: str) -> None:
    section("HTTP 接口")
    code, body = http_get(f"{base_url}/health")
    check("GET /health", code == 200, f"HTTP {code}")
    if code != 200:
        return

    check("相机已连接", bool(body.get("camera_connected")), str(body.get("camera_connected")))
    check("模型已加载", bool(body.get("detector_loaded")), str(body.get("detector_loaded")))

    code, body = http_post(f"{base_url}/v1/detect", {"conf": 0.05})
    check("POST /v1/detect", code == 200, f"HTTP {code}")
    if code == 200:
        objects = body.get("objects") or []
        check("返回目标列表", isinstance(objects, list), f"{len(objects)} 个")
        check("响应带坐标系", body.get("frame") in ("camera", "arm_base", "base", "world"),
              str(body.get("frame")))
        check("响应带延迟", isinstance(body.get("latency_ms"), int),
              f"{body.get('latency_ms')} ms")
        for obj in objects[:3]:
            pose = obj.get("pose") or {}
            print(f"         {obj.get('label')} "
                  f"xyz=({pose.get('x')}, {pose.get('y')}, {pose.get('z')}) "
                  f"frame={pose.get('frame')}")

    code, body = http_get(f"{base_url}/v1/objects")
    check("GET /v1/objects", code == 200, f"HTTP {code}")

    code, body = http_get(f"{base_url}/v1/objects/cup")
    check("GET /v1/objects/{name}", code == 200, f"HTTP {code}")
    if code == 200:
        check("返回 found 字段", "found" in body, str(body.get("found")))

    code, body = http_get(f"{base_url}/v1/status")
    check("GET /v1/status", code == 200, f"HTTP {code}")

    code, body = http_get(f"{base_url}/v1/calibration")
    check("GET /v1/calibration", code == 200, f"HTTP {code}")
    if code == 200:
        intr = body.get("intrinsics") or {}
        ext = body.get("extrinsic") or {}
        check("内参标定状态已报告", "calibrated" in intr, str(intr.get("calibrated")))
        check("外参配置状态已报告", "configured" in ext, str(ext.get("configured")))
        print(f"         内参: {intr.get('note')}")
        print(f"         外参: {ext.get('note')} (目标系 {ext.get('target_frame')})")


def check_arm_service(arm_url: str = "http://127.0.0.1:8802") -> None:
    """检查 arm_service 的运动学接口 (手眼标定要靠它)。"""
    section("arm_service 集成")

    code, body = http_get(f"{arm_url.rstrip('/')}/v1/kinematics", timeout=5.0)
    if code != 200:
        # arm_service 没跑不算失败 —— 标定时才需要它
        print(f"         (arm_service 没跑, 跳过: HTTP {code})")
        return

    check("GET /v1/kinematics", True)
    check("返回 arms 字段", isinstance(body.get("arms"), dict),
          f"{len(body.get('arms') or {})} 条臂")
    check("返回 controller_connected", "controller_connected" in body,
          str(body.get("controller_connected")))

    arms = body.get("arms") or {}
    for name, data in arms.items():
        cart = (data or {}).get("cartesian")
        base = (data or {}).get("base_frame")
        print(f"         {name}: "
              f"cartesian={'有' if cart else '无'} "
              f"base_frame={'有' if base else '无'} "
              f"kind={(data or {}).get('base_frame_kind', '-')}")

    # 手眼标定必需的量: TCP 的世界系位姿
    has_cartesian = any(
        isinstance((d or {}).get("cartesian"), dict) for d in arms.values()
    )
    check("至少一条臂有 cartesian (标定的量尺)", has_cartesian,
          "没有的话没法做手眼标定")

    lifting = body.get("lifting_column")
    print(f"         升降柱: {lifting if lifting else '无'}")


def check_agent() -> None:
    """检查 Agent 的 find_object tool。

    Agent 跑在 botagent 环境, 这里直接 import 它的模块来测。
    """
    section("Agent 集成")
    agent_dir = Path(__file__).resolve().parents[2] / "agent_service"
    if not agent_dir.exists():
        check("agent_service 目录存在", False, str(agent_dir))
        return

    sys.path.insert(0, str(agent_dir))
    try:
        from handlers.tool_handler import ToolHandler
    except Exception as exc:
        check("能 import ToolHandler", False, str(exc)[:200])
        return
    check("能 import ToolHandler", True)

    handler = ToolHandler()

    # 工具在 TOOLS 表里
    from handlers.tool_handler import TOOLS

    check("find_object 在 TOOLS 表里", "find_object" in TOOLS)
    if "find_object" in TOOLS:
        check("find_object 是输出型 (要朗读)",
              TOOLS["find_object"].get("kind") == "query",
              str(TOOLS["find_object"].get("kind")))

    # 规则匹配
    cases = [
        ("杯子在哪", "杯子"),
        ("找一下苹果", "苹果"),
        ("看看有没有瓶子", "瓶子"),
        ("苹果在什么位置", "苹果"),
        # 计数型提问: 答案只能靠相机数, 不能让模型编
        ("桌上有几个瓶子", "瓶子"),
        ("有几个苹果", "苹果"),
        ("现在有几个椅子", "椅子"),
        ("柜子上有几个瓶子", "瓶子"),
    ]
    for text, expect in cases:
        name, _, args = handler._match(text)
        ok = name == "find_object" and args.get("name") == expect
        check(f"规则匹配「{text}」", ok,
              f"-> {name} name={args.get('name')!r} (期望 {expect!r})")

    # 不该匹配的
    for text in ("左手在哪", "打开电灯", "现在几点了", "讲讲这个机器人"):
        name, _, _ = handler._match(text)
        check(f"不该匹配「{text}」", name != "find_object", f"-> {name}")

    # ---- router 的防幻觉 / 防截胡判定 ----
    #
    # 这两条是 LLM tool calling 路径上的护栏, 出问题时用户会听到
    # 「没有看到左手」这种明显不对的答案, 所以要单独守住。
    #
    # **坑**: agent_service 和 vision_service 都有叫 `config` 的模块,
    # 而 vision_service 的已经在 sys.modules 里了。直接 `from router import
    # Router` 会让 router 拿到 **vision 的 config**, 报
    # "cannot import name 'LLMConfig' from 'config'"。
    # 办法: 把 agent 的目录挪到 sys.path 最前面, 并把缓存的 config 换成
    # agent 的那份。
    saved_config = sys.modules.pop("config", None)
    sys.modules.pop("router", None)
    if agent_dir in sys.path:
        sys.path.remove(agent_dir)
    sys.path.insert(0, str(agent_dir))
    try:
        from router import Router
    except Exception as exc:
        check("能 import Router", False, str(exc)[:200])
        Router = None
    else:
        check("能 import Router", True)

    if Router is not None:
        r = Router.__new__(Router)
        # 需要查相机的提问
        for text in ("人在什么位置", "桌上有几个瓶子", "有几个苹果"):
            check(f"判定为需要查相机「{text}」",
                  r._needs_vision_lookup(text) is True)
        # 不该查相机的提问
        for text in ("左手在哪", "现在几点", "几点了", "今天天气怎么样",
                     "给我讲个笑话", "讲讲这个机器人"):
            check(f"判定为不需要查相机「{text}」",
                  r._needs_vision_lookup(text) is False)
        # 机械臂肢体问题
        for text in ("左手在哪", "右手呢", "机械臂在什么位置"):
            check(f"判定为机械臂问题「{text}」",
                  r._is_arm_query(text) is True)
        for text in ("人在什么位置", "杯子在哪"):
            check(f"判定为非机械臂问题「{text}」",
                  r._is_arm_query(text) is False)

    # 还原 sys.modules / sys.path, 别影响后面的检查
    sys.modules.pop("config", None)
    if saved_config is not None:
        sys.modules["config"] = saved_config

    # 真正调一次视觉服务
    async def _run() -> Tuple[str, str]:
        return await handler.execute("find_object", {"name": "cup"})

    try:
        status, detail = asyncio.run(_run())
        check("能调用视觉服务", True, f"{status}: {detail}")
        if status == "ok":
            check("返回了可播报的文案", len(detail) > 0, detail)
    except Exception as exc:
        check("能调用视觉服务", False, str(exc)[:200])


# ------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description="视觉服务自检")
    ap.add_argument("--base-url", default="http://127.0.0.1:8803")
    ap.add_argument("--arm-url", default="http://127.0.0.1:8802")
    ap.add_argument("--skip-http", action="store_true")
    ap.add_argument("--skip-agent", action="store_true")
    ap.add_argument("--only", default="", help="只跑某几项, 逗号分隔")
    args = ap.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()}

    def want(name: str) -> bool:
        return not only or name in only

    cfg = load_config()

    if want("config"):
        check_config(cfg)

    # 相机是**独占**的 —— 服务已经跑着的时候, 这个脚本再开一次会失败
    # (uvc_open failed)。所以先探一下服务在不在, 在的话就只走 HTTP 检查。
    #
    # 注意: 探测要**无条件**做 —— 即使用了 --skip-http 也得探,
    # 否则服务在跑的时候脚本会去抢相机, 白白报一个「相机被占用」的错。
    service_running = False
    code, _ = http_get(f"{args.base_url.rstrip('/')}/health", timeout=5.0)
    service_running = code == 200
    if service_running:
        print("\n[提示] 检测到视觉服务已在运行, "
              "相机归它用 —— 跳过直接开相机的检查")

    frame = None
    cam = None
    if (want("camera") or want("detector") or want("solver")) and not service_running:
        frame, cam = check_camera(cfg)

    if want("detector") and frame is not None:
        check_detector(cfg, frame)

    if want("solver") and frame is not None:
        check_solver(cfg, frame, cam)

    if cam is not None:
        cam.close()

    if want("http") and not args.skip_http:
        check_http(args.base_url.rstrip("/"))

    if want("arm"):
        check_arm_service(args.arm_url)

    if want("agent") and not args.skip_agent:
        check_agent()

    print(f"\n{'=' * 50}")
    print(f"通过 {len(PASSED)}, 失败 {len(FAILED)}")
    if FAILED:
        print("\n失败项:")
        for name in FAILED:
            print(f"  - {name}")
    print("=" * 50)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
