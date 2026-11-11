#!/usr/bin/env python3
"""位姿解算验证 —— 检查 3D 坐标是否合理。

验证点:
1. 深度采样能避开空洞 (取中位数)
2. 像素 + 深度 -> 3D 的公式正确 (拿已知距离的平面校验)
3. 外参变换的数学正确性

用法::

    conda run -n yolo python tools/check_solver.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from config import load_config  # noqa: E402
from solver import PoseSolver, Target3D  # noqa: E402

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


def make_solver(extrinsic=None):
    cfg = load_config()
    return PoseSolver(cfg.solver, extrinsic)


def main() -> int:
    print("\n[1] 3D 反算公式")

    # 造一个理想针孔相机
    class FakeIntr:
        fx = 600.0
        fy = 600.0
        cx = 640.0
        cy = 400.0
        width = 1280
        height = 800
        valid = True

    solver = make_solver()
    intr = FakeIntr()

    # 正前方 2 米, 像素在主点上 -> X=Y=0, Z=2
    xyz = PoseSolver._pixel_to_3d(640.0, 400.0, 2.0, intr)
    check("主点像素 -> (0, 0, 2)", abs(xyz[0]) < 1e-6 and abs(xyz[1]) < 1e-6
          and abs(xyz[2] - 2.0) < 1e-6, str(xyz))

    # 右移 300 像素, 深度 2 米 -> X = 300*2/600 = 1.0
    xyz = PoseSolver._pixel_to_3d(940.0, 400.0, 2.0, intr)
    check("右移 300px @2m -> X=1.0", abs(xyz[0] - 1.0) < 1e-6, str(xyz))

    # 下移 300 像素, 深度 2 米 -> Y = 300*2/600 = 1.0
    xyz = PoseSolver._pixel_to_3d(640.0, 700.0, 2.0, intr)
    check("下移 300px @2m -> Y=1.0", abs(xyz[1] - 1.0) < 1e-6, str(xyz))

    print("\n[2] 深度采样 (从中心向外螺旋找锚点, 避开背景)")

    # 造一张深度图 (单位: 米): 中心区域是 2.0, 边缘是 0.5 (模拟边缘跳变),
    # 中间挖几个空洞
    depth = np.full((480, 848), 2.0, dtype=np.float32)
    depth[:, :50] = 0.5      # 左边一圈浅的背景
    depth[:, -50:] = 0.5     # 右边一圈
    depth[100:110, 400:420] = 0.0    # 挖个空洞
    depth[200:210, 300:320] = 65.0   # 挖个跳变 (超出量程)

    bbox = (300, 150, 600, 350)
    d, ratio, ok = solver.sample_depth(depth, bbox, grid=5,
                                        depth_min=0.1, depth_max=5.0)
    check("采到深度接近真值 2.0m", abs(d - 2.0) < 0.05, f"{d:.3f}m")
    # 占比 = 物体表面 / 全部有效深度。框里除了 0.5 的边缘跳变, 其余都是 2.0,
    # 所以有效深度里大部分(中心那块)属于物体表面, 占比应该不低
    check("有效占比合理", ratio > 0.5, f"{ratio:.2f}")
    check("锚点可靠", ok is True)

    # 细长物体: 框里大部分是远景背景 (超出量程)
    # 中心是物体 (1.0m), 两侧是背景 (4.5m)
    thin = np.full((200, 200), 4.5, dtype=np.float32)
    thin[:, 95:105] = 1.0
    d2, r2, ok2 = solver.sample_depth(thin, (0, 0, 200, 200), grid=5,
                                       depth_min=0.1, depth_max=5.0)
    check("细长物体取到物体本身的深度", abs(d2 - 1.0) < 0.05, f"{d2:.3f}m")
    check("细长物体锚点可靠", ok2 is True)

    # 全部空洞 -> 采不到
    hole = np.zeros((100, 100), dtype=np.float32)
    d3, r3, ok3 = solver.sample_depth(hole, (0, 0, 50, 50), grid=5,
                                       depth_min=0.1, depth_max=5.0)
    check("全空洞时深度为 0", d3 == 0.0, f"{d3}")
    check("全空洞时占比为 0", r3 == 0.0, f"{r3}")
    check("全空洞时锚点不可靠", ok3 is False)

    # ---- 稀疏深度 (真实相机的常态: 有效率只有 30%~40%) ----
    sparse = np.zeros((200, 200), dtype=np.float32)
    rng = np.random.default_rng(42)
    mask = rng.random((200, 200)) < 0.35
    sparse[mask] = 1.5            # 物体表面, 零散分布
    sparse[150:, :] = 3.0         # 下方是背景
    d4, r4, ok4 = solver.sample_depth(sparse, (0, 0, 200, 150), grid=5,
                                       depth_min=0.1, depth_max=5.0)
    check("稀疏深度能采到值", d4 > 0, f"{d4:.3f}m")
    check("稀疏深度采到的值在合理范围", 1.0 < d4 < 2.5, f"{d4:.3f}m")
    check("稀疏深度锚点可靠", ok4 is True)

    # 中心大片空洞, 但周围一圈有物体表面 -> 螺旋搜索应该能绕过空洞
    ring = np.full((200, 200), 1.8, dtype=np.float32)
    ring[60:140, 60:140] = 0.0    # 中心 80x80 空洞
    d5, r5, ok5 = solver.sample_depth(ring, (0, 0, 200, 200), grid=5,
                                       depth_min=0.1, depth_max=5.0)
    check("中心空洞时螺旋搜索能绕过去", abs(d5 - 1.8) < 0.05, f"{d5:.3f}m")

    print("\n[3] 深度不可信时标记 invalid")

    # 全框空洞 -> 采不到深度, 必须标 invalid
    glass = np.full((480, 848), 2.0, dtype=np.float32)
    glass[150:350, 300:600] = 0.0   # 整个框都是空洞
    target = solver.solve(glass, (300, 150, 600, 350), intr)
    check("整框空洞 -> depth_valid=False", target.depth_valid is False,
          f"ratio={target.valid_ratio:.3f}")
    check("整框空洞 -> 不报假坐标",
          not (target.x != 0.0 or target.y != 0.0), f"({target.x:.2f},{target.y:.2f})")

    # 正常情况
    good = np.full((480, 848), 2.0, dtype=np.float32)
    target = solver.solve(good, (300, 150, 600, 350), intr)
    check("正常深度 -> depth_valid=True", target.depth_valid is True)
    check("正常深度 -> Z≈2.0", abs(target.z - 2.0) < 0.05, f"{target.z:.3f}")

    print("\n[4] 外参变换")

    from config import ExtrinsicConfig

    # 没启用外参 -> to_base_frame 返回 None
    ext = ExtrinsicConfig(enabled=False)
    s2 = make_solver(ext)
    check("外参未启用 -> None", s2.to_base_frame((1.0, 2.0, 3.0)) is None)

    # 启用但没填 -> None
    ext = ExtrinsicConfig(enabled=True, matrix=[], rotation=[])
    s3 = make_solver(ext)
    check("外参启用但没填 -> None", s3.to_base_frame((1.0, 2.0, 3.0)) is None)

    # 单位旋转 + 平移
    ext = ExtrinsicConfig(
        enabled=True,
        rotation=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        translation=[0.5, 0.0, 0.0],
        flip_axes=False,
    )
    s4 = make_solver(ext)
    out = s4.to_base_frame((1.0, 2.0, 3.0))
    check("单位旋转+平移", out is not None and abs(out[0] - 1.5) < 1e-6
          and abs(out[1] - 2.0) < 1e-6 and abs(out[2] - 3.0) < 1e-6, str(out))

    # 90 度绕 Z 旋转: (1,0,0) -> (0,1,0)
    import math
    c90 = math.cos(math.pi / 2)
    s90 = math.sin(math.pi / 2)
    ext = ExtrinsicConfig(
        enabled=True,
        rotation=[[c90, -s90, 0], [s90, c90, 0], [0, 0, 1]],
        translation=[0.0, 0.0, 0.0],
        flip_axes=False,
    )
    s5 = make_solver(ext)
    out = s5.to_base_frame((1.0, 0.0, 0.0))
    check("绕Z转90度", out is not None and abs(out[0]) < 1e-6
          and abs(out[1] - 1.0) < 1e-6, str(out))

    # flip_axes: 相机系 (X右Y下Z前) -> 基座系 (X前Y左Z上)
    #
    # 这是一个**轴置换** (行列式 +1, 是旋转不是镜像):
    #   相机前 -> 基座前, 相机右 -> 基座 -Y, 相机下 -> 基座 -Z
    #
    # 历史包袱: 老版本的 flip_axes 做的是 (-x, -y, z) —— 那是绕光轴转
    # 180 度, 跟注释里写的语义对不上。现已改成正确的轴置换。
    ext = ExtrinsicConfig(
        enabled=True,
        rotation=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        translation=[0.0, 0.0, 0.0],
        flip_axes=True,
    )
    s6 = make_solver(ext)
    out = s6.to_base_frame((1.0, 2.0, 3.0))
    # 轴置换: X_base = Z_cam, Y_base = -X_cam, Z_base = -Y_cam
    check("flip_axes: 相机前 -> 基座前", out is not None and abs(out[0] - 3.0) < 1e-6,
          str(out))
    check("flip_axes: 相机右 -> 基座 -Y", out is not None and abs(out[1] + 1.0) < 1e-6,
          str(out))
    check("flip_axes: 相机下 -> 基座 -Z", out is not None and abs(out[2] + 2.0) < 1e-6,
          str(out))

    # flip_axes 后的旋转部分必须还是旋转 (行列式 +1)
    m = s6.solver_provider_matrix() if hasattr(s6, "solver_provider_matrix") else None
    if m is not None:
        det = float(np.linalg.det(m[:3, :3]))
        check("flip_axes 后行列式 = +1", abs(det - 1.0) < 1e-9, f"det={det:+.9f}")

    # 4x4 矩阵优先
    ext = ExtrinsicConfig(
        enabled=True,
        matrix=[
            [1, 0, 0, 10.0],
            [0, 1, 0, 20.0],
            [0, 0, 1, 30.0],
            [0, 0, 0, 1],
        ],
        rotation=[[0, 0, 0], [0, 0, 0], [0, 0, 0]],
        translation=[0, 0, 0],
        flip_axes=False,
    )
    s7 = make_solver(ext)
    out = s7.to_base_frame((1.0, 2.0, 3.0))
    check("4x4 矩阵优先", out is not None and abs(out[0] - 11.0) < 1e-6
          and abs(out[1] - 22.0) < 1e-6 and abs(out[2] - 33.0) < 1e-6, str(out))

    print("\n[5] 深度相机 -> 彩色相机 坐标变换")

    # 没有外参 -> 原样
    out = PoseSolver._to_color_frame((1.0, 2.0, 3.0), None)
    check("无外参 -> 恒等", abs(out[0] - 1.0) < 1e-9 and abs(out[2] - 3.0) < 1e-9,
          str(out))

    # 单位旋转 + 平移
    rot = np.eye(3)
    trans = np.array([0.1, 0.0, 0.0])
    out = PoseSolver._to_color_frame((1.0, 2.0, 3.0), (rot, trans))
    check("单位旋转+平移", abs(out[0] - 1.1) < 1e-6 and abs(out[1] - 2.0) < 1e-6
          and abs(out[2] - 3.0) < 1e-6, str(out))

    # 90 度绕 Z
    c90 = math.cos(math.pi / 2)
    s90 = math.sin(math.pi / 2)
    rot2 = np.array([[c90, -s90, 0], [s90, c90, 0], [0, 0, 1]], dtype=float)
    out = PoseSolver._to_color_frame((1.0, 0.0, 0.0), (rot2, np.zeros(3)))
    check("绕Z转90度", abs(out[0]) < 1e-6 and abs(out[1] - 1.0) < 1e-6, str(out))

    print(f"\n通过 {PASSED}, 失败 {FAILED}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
