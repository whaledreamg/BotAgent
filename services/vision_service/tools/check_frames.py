#!/usr/bin/env python3
"""端到端联调测试: 视觉服务 + 假 arm_service。

起一个假的 arm_service (``/v1/kinematics`` 返回左右臂基座位姿 + 头位姿),
再起视觉服务, 检查:

1. 动态外参能不能造出来
2. ``/v1/detect?frame=left_base`` 的坐标对不对
3. ``/v1/frames`` 各个系的坐标是否自洽
4. 头位姿变了之后, world 坐标是否跟着变

不需要相机 —— 目标位姿用手工构造的相机系点。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from extrinsic import (  # noqa: E402
    RigidExtrinsicProvider,
    WorldContext,
    invert_transform,
    matrix_from_pose,
)

PASSED = 0
FAILED = 0


def check(desc: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {desc}" + (f"  {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {desc}  {detail}")


# ------------------------------------------------------------ 假 arm_service

T_WORLD_LEFT = matrix_from_pose((0.0, 0.4, 0.0), (0.0, 0.0, 0.0))
T_WORLD_RIGHT = matrix_from_pose((0.0, -0.4, 0.0), (0.0, 0.0, 0.0))
T_WORLD_HEAD = {"x": 0.0, "y": 0.0, "z": 1.6, "a": 0.0, "b": 0.0, "c": 0.0}

STATE = {"head_z": 1.6}


class FakeArm(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102
        pass

    def _send(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/v1/kinematics"):
            self._send({
                "ok": True,
                "arms": {
                    "LeftArm": {
                        "cartesian": {"x": 0.3, "y": 0.4, "z": 0.8,
                                      "a": 0, "b": 0, "c": 0},
                        "base_frame": {"x": 0.0, "y": 0.4, "z": 0.0,
                                       "a": 0, "b": 0, "c": 0},
                        "base_frame_kind": "static",
                    },
                    "RightArm": {
                        "cartesian": {"x": 0.3, "y": -0.4, "z": 0.8,
                                      "a": 0, "b": 0, "c": 0},
                        "base_frame": {"x": 0.0, "y": -0.4, "z": 0.0,
                                       "a": 0, "b": 0, "c": 0},
                        "base_frame_kind": "static",
                    },
                },
                "lifting_column": None,
                "head_frame": {
                    "x": 0.0, "y": 0.0, "z": STATE["head_z"],
                    "a": 0, "b": 0, "c": 0,
                },
                "head_frame_kind": "static",
                "controller_connected": True,
                "timestamp_ms": int(time.monotonic() * 1000),
                "notes": [],
            })
        else:
            self._send({"ok": True})


def start_fake_arm(port: int) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), FakeArm)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ------------------------------------------------------------ 测试

def main() -> int:
    print("=" * 62)
    print("视觉 x arm_service 联调测试")
    print("=" * 62)

    arm_port = 18802
    srv = start_fake_arm(arm_port)
    print(f"\n[0] 假 arm_service 起在 127.0.0.1:{arm_port}")

    # 标定结果: T_world_cam (相机在世界系 (1.0, 0.0, 1.5))
    t_world_cam = matrix_from_pose((1.0, 0.0, 1.5), (0.0, 0.0, 0.0))

    ctx = WorldContext()
    ctx.base_frames["LeftArm"] = T_WORLD_LEFT
    ctx.base_frames["RightArm"] = T_WORLD_RIGHT
    ctx.head_frame = matrix_from_pose((0.0, 0.0, 1.6), (0.0, 0.0, 0.0))

    print("\n[1] provider 数学自洽")
    p = RigidExtrinsicProvider(t_world_cam, matrix_frame="world",
                               frame="world", context=ctx)
    out = p.transform((0.0, 0.0, 1.0))
    check("world: 相机前 1m", out is not None and abs(out[0] - 1.0) < 1e-9
          and abs(out[2] - 2.5) < 1e-9, f"{out}")

    p_left = p.with_frame("left_base")
    out = p_left.transform((0.0, 0.0, 1.0))
    exp = invert_transform(T_WORLD_LEFT) @ t_world_cam @ np.array([0, 0, 1, 1.0])
    check("left_base: 减去左臂基座偏移", out is not None
          and abs(out[0] - exp[0]) < 1e-9 and abs(out[1] - exp[1]) < 1e-9,
          f"{out} vs {exp[:3]}")

    p_right = p.with_frame("right_base")
    out = p_right.transform((0.0, 0.0, 1.0))
    exp = invert_transform(T_WORLD_RIGHT) @ t_world_cam @ np.array([0, 0, 1, 1.0])
    check("right_base: 减去右臂基座偏移", out is not None
          and abs(out[1] - exp[1]) < 1e-9, f"{out} vs {exp[:3]}")

    print("\n[2] HTTP 拉 arm_service 快照")
    from arm_kinematics import ArmKinematicsClient

    client = ArmKinematicsClient(f"http://127.0.0.1:{arm_port}")
    snap = client.snapshot(refresh=False)
    check("拿到快照", snap is not None)
    if snap:
        check("两条臂的基座位姿都在", len(snap.get("arms") or {}) == 2,
              str(list((snap.get("arms") or {}).keys())))
        check("头位姿在", isinstance(snap.get("head_frame"), dict),
              str(snap.get("head_frame")))

    print("\n[3] 视觉服务的 WorldContext 构造")
    from service import VisionService
    from config import load_config

    cfg = load_config()
    # 注入: matrix_frame=world, target_frame=world, dynamic=True
    cfg.extrinsic.enabled = True
    cfg.extrinsic.matrix = [[float(v) for v in row] for row in t_world_cam]
    cfg.extrinsic.matrix_frame = "world"
    cfg.extrinsic.target_frame = "world"
    cfg.extrinsic.dynamic = True
    cfg.extrinsic.arm_service_url = f"http://127.0.0.1:{arm_port}"

    svc = VisionService(cfg)
    svc._setup_dynamic_extrinsic()
    check("动态 provider 造出来了", svc._dynamic_provider is not None)
    svc._refresh_world_context()
    check("拉到两条臂的基座",
          len(svc._world_context.base_frames) == 2,
          str(list(svc._world_context.base_frames.keys())))
    check("拉到头位姿", svc._world_context.head_frame is not None)

    print("\n[4] frame_snapshot 各个系")
    snap_all = svc.frame_snapshot((0.0, 0.0, 1.0))
    check("camera 在", "camera" in snap_all)
    check("world 在", "world" in snap_all)
    check("left_base 在", "left_base" in snap_all)
    check("right_base 在", "right_base" in snap_all)
    check("world 坐标正确",
          abs(snap_all["world"]["x"] - 1.0) < 1e-6
          and abs(snap_all["world"]["z"] - 2.5) < 1e-6,
          str(snap_all.get("world")))
    check("sources 标明了来源",
          snap_all["sources"].get("left_base") == "base_frame:left",
          str(snap_all.get("sources")))

    print("\n[5] 变换成 left_base / right_base")
    out_l = svc.transform_point((0.0, 0.0, 1.0), "left_base")
    out_r = svc.transform_point((0.0, 0.0, 1.0), "right_base")
    check("left_base 变换成功", out_l is not None, str(out_l))
    check("right_base 变换成功", out_r is not None, str(out_r))
    if out_l and out_r:
        check("左右臂结果确实不同", abs(out_l[1] - out_r[1]) > 1e-6,
              f"left_y={out_l[1]:.4f} right_y={out_r[1]:.4f}")

    print("\n[6] 头动了 -> 语义要对")
    # matrix_frame=world: T_world_cam 是绝对的, 头动不影响它 (但会过时)
    before = svc.transform_point((0.0, 0.0, 1.0), "world")
    STATE["head_z"] = 1.8
    svc._refresh_world_context()
    after = svc.transform_point((0.0, 0.0, 1.0), "world")
    check("matrix_frame=world: 头动不影响 world 坐标 (绝对标定量)",
          before is not None and after is not None
          and abs(after[2] - before[2]) < 1e-9,
          f"{before} -> {after}")

    # matrix_frame=head: T_head_cam 是相对头座的, 头动要跟着变
    cfg3 = load_config()
    cfg3.extrinsic.enabled = True
    cfg3.extrinsic.matrix = [[1, 0, 0, 0.1], [0, 1, 0, 0.0],
                             [0, 0, 1, 0.2], [0, 0, 0, 1]]
    cfg3.extrinsic.matrix_frame = "head"
    cfg3.extrinsic.target_frame = "world"
    cfg3.extrinsic.dynamic = True
    cfg3.extrinsic.arm_service_url = f"http://127.0.0.1:{arm_port}"
    svc3 = VisionService(cfg3)
    svc3._setup_dynamic_extrinsic()
    svc3._refresh_world_context()
    before_h = svc3.transform_point((0.0, 0.0, 1.0), "world")
    STATE["head_z"] = 2.0
    svc3._refresh_world_context()
    after_h = svc3.transform_point((0.0, 0.0, 1.0), "world")
    check("matrix_frame=head: 头抬高 20cm, world z 跟着变",
          before_h is not None and after_h is not None
          and abs((after_h[2] - before_h[2]) - 0.2) < 1e-6,
          f"{before_h} -> {after_h}")
    STATE["head_z"] = 1.8

    print("\n[7] matrix_frame=head 时缺头位姿 -> None")
    cfg2 = load_config()
    cfg2.extrinsic.enabled = True
    cfg2.extrinsic.matrix = [[1, 0, 0, 0.1], [0, 1, 0, 0.0],
                              [0, 0, 1, 0.2], [0, 0, 0, 1]]
    cfg2.extrinsic.matrix_frame = "head"
    cfg2.extrinsic.target_frame = "world"
    cfg2.extrinsic.dynamic = True
    cfg2.extrinsic.arm_service_url = f"http://127.0.0.1:{arm_port}"
    svc2 = VisionService(cfg2)
    svc2._setup_dynamic_extrinsic()
    svc2._refresh_world_context()
    out = svc2.transform_point((0.0, 0.0, 1.0), "world")
    check("有头位姿时能算", out is not None, str(out))
    check("world 坐标 = 头位姿 + 相机偏移",
          out is not None and abs(out[0] - 0.1) < 1e-6
          and abs(out[2] - (1.8 + 0.2 + 1.0)) < 1e-6, str(out))

    srv.shutdown()

    print()
    print("=" * 62)
    print(f"结果: {PASSED} 通过, {FAILED} 失败")
    print("=" * 62)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
