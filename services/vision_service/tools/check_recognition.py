#!/usr/bin/env python3
"""相机到底识别到了什么? —— 一次性看清检测结果。

**为什么需要这个脚本**: 视觉服务在跑的时候相机是**独占**的, 别的进程
开不了相机。所以想「看看相机看到了啥」有两条路:

1. 走 HTTP 问服务 (``--http``, 服务在跑时用这个)
2. 先停服务, 用这个脚本直接开相机 (``--live``, 能看标注图)

**默认模式来自配置** (``configs/vision.yaml`` 的 ``vision.recognition.mode``),
默认是 ``live``。命令行参数可以覆盖配置。

用法::

    # 默认 (按配置走, 默认 live)
    conda run -n yolo python tools/check_recognition.py

    # 服务在跑 —— 走 HTTP
    conda run -n yolo python tools/check_recognition.py --http

    # 服务没跑 —— 直接开相机 (会存标注图到 logs/)
    conda run -n yolo python tools/check_recognition.py --live

    # 只看某几类, 放宽置信度
    conda run -n yolo python tools/check_recognition.py --classes cup,bottle --conf 0.1

    # 连续盯 30 秒 (看是不是偶尔才认出来)
    conda run -n yolo python tools/check_recognition.py --http --watch 30
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config  # noqa: E402

LOG_DIR = Path(__file__).resolve().parents[3] / "logs"
DEFAULT_URL = "http://127.0.0.1:8803"


# ------------------------------------------------------------ HTTP 通路


def _get(url: str, timeout: float = 15.0):
    """发 GET 请求 —— **必须清掉代理**, 否则回环请求会被代理拦掉。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.reason}
    except Exception as exc:
        return 0, {"error": str(exc)}


def _post(url: str, payload: dict, timeout: float = 20.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.reason}
    except Exception as exc:
        return 0, {"error": str(exc)}


# ------------------------------------------------------------ 打印


def print_objects(objects: list, frame: str = "") -> None:
    if not objects:
        print("  (没检测到任何物体)")
        return

    print(f"  {'类别':<16} {'置信度':>7}  {'bbox (x1,y1,x2,y2)':<26} "
          f"{'3D 位置 (x,y,z)':<26} 深度")
    print("  " + "-" * 92)
    for o in objects:
        bb = o.get("bbox") or {}
        p = o.get("pose") or {}
        bbox = (f"({bb.get('x1', 0):.0f},{bb.get('y1', 0):.0f},"
                f"{bb.get('x2', 0):.0f},{bb.get('y2', 0):.0f})")
        xyz = (f"({p.get('x', 0):.3f},{p.get('y', 0):.3f},{p.get('z', 0):.3f})"
               f" {p.get('frame', '?')}")
        depth = "有效" if p.get("depth_valid", True) else "**测不准**"
        print(f"  {o.get('label', '?'):<16} {o.get('confidence', 0):>7.3f}  "
              f"{bbox:<26} {xyz:<26} {depth}")


def summarize(objects: list) -> None:
    if not objects:
        return
    counts = Counter(o.get("label", "?") for o in objects)
    print()
    print("  按类别统计:")
    for label, n in counts.most_common():
        confs = [o.get("confidence", 0) for o in objects
                 if o.get("label") == label]
        print(f"    {label:<16} x{n}   置信度 {min(confs):.2f}~{max(confs):.2f}")


# ------------------------------------------------------------ HTTP 模式


def run_http(base_url: str, conf: float, classes: list, watch: int) -> int:
    print(f"[HTTP] 连 {base_url}")

    code, health = _get(f"{base_url}/health")
    if code != 200:
        print(f"[FAIL] 连不上视觉服务 (HTTP {code}): {health.get('error', '')}")
        print("       服务没起? 先跑 ./scripts/start_vision.sh")
        return 1

    print(f"       相机: {health.get('camera_name')} "
          f"(SN={health.get('camera_serial')})")
    print(f"       模型: {health.get('weights')} device={health.get('device')}")
    if not health.get("camera_connected"):
        print("[FAIL] 相机没连上 —— 检测不了")
        for e in health.get("errors") or []:
            print(f"       {e}")
        return 1

    payload = {"conf": conf}
    if classes:
        payload["classes"] = classes

    deadline = time.time() + watch if watch > 0 else 0
    round_no = 0

    while True:
        round_no += 1
        code, body = _post(f"{base_url}/v1/detect", payload)
        if code != 200:
            print(f"[FAIL] /v1/detect 返回 HTTP {code}: {body.get('error')}")
            return 1

        objects = body.get("objects") or []
        print()
        print(f"=== 第 {round_no} 次检测 "
              f"(耗时 {body.get('latency_ms', 0)} ms, 坐标系 {body.get('frame')}) ===")
        print_objects(objects)
        summarize(objects)

        if watch <= 0:
            break
        if time.time() >= deadline:
            print()
            print(f"[done] 盯了 {watch} 秒, 共 {round_no} 次")
            break
        time.sleep(2)

    return 0


# ------------------------------------------------------------ 直连相机


def run_live(conf: float, classes: list, frames: int = 5,
             output_dir: str = "logs") -> int:
    """服务没跑时用 —— 直接开相机跑一次 YOLO。

    相机是**独占**的: 视觉服务在跑的话这里会开失败 (uvc_open failed -6)。
    """
    from camera import CameraError, OrbbecCamera
    from detector import Detector, DetectorError

    cfg = load_config()
    cfg.detector.conf = conf

    print("[live] 打开相机 ...")
    cam = OrbbecCamera(cfg.camera)
    try:
        cam.open()
    except CameraError as exc:
        print(f"[FAIL] 打开相机失败: {exc}")
        print("       视觉服务在跑? 相机是独占的 —— 先 ./scripts/stop_all.sh")
        print("       (或者用 --http 走服务接口)")
        return 1

    try:
        print("[live] 加载模型 ...")
        det = Detector(cfg.detector)
        try:
            det.load()
        except DetectorError as exc:
            print(f"[FAIL] {exc}")
            return 1
        print(f"       设备 {det.device}, 类别数 {len(det.class_names)}")

        # 采几帧, 挑检测到东西最多的一帧 —— 单帧可能刚好没看到
        best = None
        for i in range(max(1, frames)):
            frame = cam.read()
            if frame is None or frame.color is None:
                continue
            results = det.infer(frame.color, conf=conf)
            if classes:
                wanted = {c.strip().lower() for c in classes}
                results = [r for r in results if r.label.lower() in wanted]
            print(f"       第 {i + 1} 帧: {len(results)} 个目标")
            if best is None or len(results) > len(best[1]):
                best = (frame, results)
            if len(results) > 0:
                break

        if best is None:
            print("[FAIL] 没采到帧")
            return 1

        frame, results = best
        print()
        print(f"=== 检测到 {len(results)} 个目标 ===")
        for r in results:
            print(f"  {r.label:<16} conf={r.confidence:.3f} "
                  f"bbox={tuple(round(v) for v in r.bbox)}")

        # 画图存下来 —— 框画得对不对, 肉眼看最快
        try:
            import cv2

            out_dir = _resolve_output_dir(output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            canvas = frame.color.copy()
            for r in results:
                x1, y1, x2, y2 = [int(round(v)) for v in r.bbox]
                cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(canvas, f"{r.label} {r.confidence:.2f}",
                            (x1, max(15, y1 - 5)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            out = out_dir / "vision_recognition.png"
            cv2.imwrite(str(out), canvas)
            raw = out_dir / "vision_color.png"
            cv2.imwrite(str(raw), frame.color)
            print()
            print(f"  标注图: {out}")
            print(f"  原始图: {raw}")
        except Exception as exc:
            print(f"  (画图失败: {exc})")

        return 0 if results else 2
    finally:
        cam.close()


# ------------------------------------------------------------ main


def _resolve_output_dir(output_dir: str) -> Path:
    """把配置里的输出目录解析成绝对路径。

    相对路径按**项目根**算 (不是按 cwd) —— 不然在不同目录下跑脚本
    会把图存到奇怪的地方。
    """
    raw = (output_dir or "logs").strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = LOG_DIR.parent / path
    return path


def main() -> int:
    cfg = load_config()
    rec = cfg.recognition

    ap = argparse.ArgumentParser(description="测试相机识别到了什么")
    ap.add_argument("--http", action="store_true",
                    help="走 HTTP (服务在跑时用)")
    ap.add_argument("--live", action="store_true",
                    help="直接开相机 (服务没跑时用)")
    ap.add_argument("--url", default=DEFAULT_URL, help="视觉服务地址")
    ap.add_argument("--conf", type=float, default=rec.conf,
                    help=f"置信度阈值 (默认取配置 {rec.conf})")
    ap.add_argument("--classes", default="", help="只看这些类, 逗号分隔")
    ap.add_argument("--watch", type=int, default=0, help="连续盯 N 秒")
    ap.add_argument("--frames", type=int, default=rec.frames,
                    help=f"live 模式试几帧 (默认取配置 {rec.frames})")
    ap.add_argument("--output-dir", default=rec.output_dir,
                    help=f"标注图保存目录 (默认取配置 {rec.output_dir})")
    args = ap.parse_args()

    classes = [c for c in args.classes.split(",") if c.strip()]

    # ---- 定模式 ----
    #
    # 优先级: 命令行显式指定 > 配置 > 自动探测。
    #
    # 配置默认是 live —— 但 live 在服务跑着的时候会开相机失败, 所以
    # 自动探测这一步不能省: 服务活着就退回 http, 而不是直接报错退出。
    if args.http:
        mode = "http"
    elif args.live:
        mode = "live"
    elif rec.mode in ("http", "live"):
        mode = rec.mode
    else:
        mode = "live"

    fallback_reason = ""
    if mode == "live":
        # 先探一下服务在不在线 —— 在线就别抢相机了
        code, _ = _get(f"{args.url}/health", timeout=3)
        if code == 200:
            fallback_reason = (
                f"视觉服务在 {args.url} 上跑着, 相机是独占的 —— "
                f"自动切到 HTTP 模式"
            )
            mode = "http"

    print("=" * 70)
    print("相机识别测试")
    print("=" * 70)
    print()
    print(f"模式: {mode}   置信度: {args.conf}   "
          f"来源: 配置 vision.recognition (mode={rec.mode})")
    if fallback_reason:
        print(f"      {fallback_reason}")
    print()

    if mode == "http":
        return run_http(args.url, args.conf, classes, args.watch)
    return run_live(args.conf, classes, args.frames, args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
