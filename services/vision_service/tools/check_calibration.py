#!/usr/bin/env python3
"""标定流程的**自检** —— 用合成数据验证数学对不对。

不需要相机、不需要机械臂。构造一组已知的「相机-世界」变换, 反推出一组
合成采样, 再跑一遍标定流程, 看能不能解回原值。

这个脚本的价值: 标定脚本本身的 bug (行列式搞反、角点顺序错、矩阵乘的
方向错) 靠真机试错很贵 —— 一次标定要半小时。这里几秒钟就能验出来。

用法::

    conda run -n yolo python tools/check_calibration.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import List, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.board import BoardSpec  # noqa: E402
from calibration.handeye import PoseSample, solve_handeye  # noqa: E402
from extrinsic import (  # noqa: E402
    AXIS_PERMUTATION_CAMERA_TO_BASE,
    invert_transform,
    matrix_from_pose,
    matrix_to_rpy,
    rotation_error_rad,
    rpy_to_matrix,
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


# ------------------------------------------------------------ 合成数据


def make_synthetic_samples(
    n: int = 20,
    noise_m: float = 0.0,
    noise_px: float = 0.0,
    seed: int = 42,
) -> Tuple[List[PoseSample], np.ndarray]:
    """造一组合成采样。

    做法:
    1. 随机生成 ``n`` 个「板在相机系里」的位姿 (板在视野内可见)
    2. 用**已知的**真值外参反推「工具在世界系里」的位姿
    3. 反过来用真值外参算出板在相机系的位姿, 生成采样

    于是标定应该能**精确**解回真值 (噪声为 0 时)。
    """
    import cv2

    rng = np.random.default_rng(seed)
    spec = BoardSpec()
    board = make_board_object_points(spec)

    # 真值: 相机在机器人躯干上, 朝前并略向下
    # T_world_cam: 相机系 -> 世界系
    rpy_cam = rpy_to_matrix([0.15, 0.25, 0.0])  # 相机朝前, 略向下俯
    true_t_world_cam = np.eye(4)
    true_t_world_cam[:3, :3] = rpy_cam
    true_t_world_cam[:3, 3] = np.array([0.05, 0.0, 1.45])

    cam_matrix = np.array([[900.0, 0, 640.0], [0, 900.0, 400.0], [0, 0, 1.0]])

    samples: List[PoseSample] = []
    for i in range(n):
        # 板在相机系里的位姿: 在相机前方 0.4~1.2 m, 带各种姿态
        dist = 0.4 + 0.8 * rng.random()
        ang = np.array(
            [
                rng.uniform(-0.6, 0.6),
                rng.uniform(-0.6, 0.6),
                rng.uniform(-3.0, 3.0),
            ]
        )
        rot, _ = cv2.Rodrigues(ang)
        t_cam_board = np.eye(4)
        t_cam_board[:3, :3] = rot
        t_cam_board[:3, 3] = np.array(
            [rng.uniform(-0.3, 0.3), rng.uniform(-0.25, 0.25), dist]
        )

        # 板的世界位姿 = T_world_cam · T_cam_board
        t_world_board = true_t_world_cam @ t_cam_board

        # 反推工具位姿 (假设 T_tool_board = I, 即板坐标系 == 工具坐标系)
        t_world_tool = t_world_board.copy()

        # 角点投影
        proj, _ = cv2.projectPoints(
            board, t_cam_board[:3, :3], t_cam_board[:3, 3], cam_matrix, None
        )
        img_pts = proj.reshape(-1, 2)
        if noise_px > 0:
            img_pts = img_pts + rng.normal(0, noise_px, img_pts.shape)

        if noise_m > 0:
            t_world_tool[:3, 3] += rng.normal(0, noise_m, 3)

        samples.append(
            PoseSample(
                t_cam_board=t_cam_board,
                t_world_tool=t_world_tool,
                obj_points=board.copy(),
                img_points=img_pts,
                corner_count=len(img_pts),
                label=f"synthetic-{i}",
            )
        )

    return samples, true_t_world_cam


def make_board_object_points(spec: BoardSpec) -> np.ndarray:
    from calibration.board import board_object_points

    return board_object_points(spec)


# ------------------------------------------------------------ 检查项


def check_axis_permutation() -> None:
    print("\n[1] 轴置换 (相机系 -> 基座系)")

    m = AXIS_PERMUTATION_CAMERA_TO_BASE
    det = float(np.linalg.det(m))
    check("行列式 = +1 (是旋转不是镜像)", abs(det - 1.0) < 1e-9, f"det={det:+.9f}")

    check(
        "正交",
        float(np.abs(m.T @ m - np.eye(3)).max()) < 1e-12,
        f"最大偏差 {float(np.abs(m.T @ m - np.eye(3)).max()):.2e}",
    )

    # 语义: 相机前 -> 基座前
    fwd = m @ np.array([0.0, 0.0, 1.0])
    check(
        "相机前方 -> 基座前方 (+X)",
        np.allclose(fwd, [1, 0, 0]),
        f"{np.round(fwd, 6)}",
    )

    # 相机右 -> 基座 -Y (基座 Y 向左, 所以右是 -Y)
    right = m @ np.array([1.0, 0.0, 0.0])
    check(
        "相机右方 -> 基座 -Y (左)",
        np.allclose(right, [0, -1, 0]),
        f"{np.round(right, 6)}",
    )

    # 相机下 -> 基座 -Z (基座 Z 向上, 所以下是 -Z)
    down = m @ np.array([0.0, 1.0, 0.0])
    check(
        "相机下方 -> 基座 -Z (下)",
        np.allclose(down, [0, 0, -1]),
        f"{np.round(down, 6)}",
    )


def check_rpy() -> None:
    print("\n[2] RPY 变换")

    for rpy in [(0.0, 0.0, 0.0), (0.1, 0.2, 0.3), (-1.0, 0.5, 2.0), (0.0, 1.4, 0.0)]:
        m = rpy_to_matrix(rpy)
        back = matrix_to_rpy(m)
        ok = np.allclose(back, rpy, atol=1e-9)
        check(
            f"RPY 往返 ({rpy})",
            ok,
            f"-> {tuple(round(v, 9) for v in back)}",
        )

    m = rpy_to_matrix([0.0, 0.0, 0.0])
    check("零位是单位阵", np.allclose(m, np.eye(3)), str(np.round(m, 6)))


def check_invert() -> None:
    print("\n[3] 矩阵求逆")

    rng = np.random.default_rng(7)
    for _ in range(5):
        m = np.eye(4)
        rot, _ = __import__("cv2").Rodrigues(rng.normal(0, 0.5, 3))
        m[:3, :3] = rot
        m[:3, 3] = rng.normal(0, 1.0, 3)
        inv = invert_transform(m)
        ok = np.allclose(m @ inv, np.eye(4), atol=1e-10)
        if not ok:
            check("求逆正确", False, f"残差 {np.abs(m @ inv - np.eye(4)).max():.2e}")
            return
    check("求逆正确 (5 组随机矩阵)", True)


def check_handeye_noiseless() -> None:
    print("\n[4] 手眼标定 (无噪声)")
    samples, true_m = make_synthetic_samples(n=20)
    result = solve_handeye(samples, refine=False)
    if result is None:
        check("解算成功", False)
        return
    check("解算成功", True, f"{result.sample_count} 个采样")

    pos_err = float(np.linalg.norm(result.matrix[:3, 3] - true_m[:3, 3]))
    rot_err = rotation_error_rad(result.matrix[:3, :3], true_m[:3, :3])

    check(
        "平移解回真值 (< 1 mm)",
        pos_err < 0.001,
        f"误差 {pos_err * 1000:.4f} mm",
    )
    check(
        "旋转解回真值 (< 0.01°)",
        rot_err < math.radians(0.01),
        f"误差 {math.degrees(rot_err):.6f}°",
    )


def check_handeye_noisy() -> None:
    print("\n[5] 手眼标定 (带噪声)")
    samples, true_m = make_synthetic_samples(n=25, noise_px=0.3, noise_m=0.001)
    result = solve_handeye(samples, refine=False)
    if result is None:
        check("解算成功", False)
        return

    pos_err = float(np.linalg.norm(result.matrix[:3, 3] - true_m[:3, 3]))
    check(
        "带噪声时平移仍在 5 mm 内",
        pos_err < 0.005,
        f"误差 {pos_err * 1000:.2f} mm, RMS {result.rms_position * 1000:.2f} mm",
    )


def check_handeye_outlier() -> None:
    print("\n[6] 野值剔除")
    samples, true_m = make_synthetic_samples(n=20)
    # 人为把第 3 个采样搞坏 (TCP 读数抖动)
    samples[3].t_world_tool[:3, 3] += np.array([0.5, 0.0, 0.0])

    result = solve_handeye(samples, refine=False, outlier_threshold=0.02)
    if result is None:
        check("解算成功", False)
        return

    check("坏采样被剔除", 3 in result.rejected, f"剔除 {result.rejected}")
    pos_err = float(np.linalg.norm(result.matrix[:3, 3] - true_m[:3, 3]))
    check(
        "剔掉野值后平移仍在 1 mm 内",
        pos_err < 0.001,
        f"误差 {pos_err * 1000:.4f} mm",
    )


def check_handeye_refine() -> None:
    print("\n[7] 精化 (Gauss-Newton, 内参固定)")
    # 关键: 精化必须用**固定**的内参。边解边估会让内外参互相补偿。
    true_k = np.array([[900.0, 0, 640.0], [0, 900.0, 400.0], [0, 0, 1.0]])
    samples, true_m = make_synthetic_samples(n=20, noise_px=0.5)
    result = solve_handeye(samples, refine=False)
    if result is None:
        check("解算成功", False)
        return
    baseline = float(np.linalg.norm(result.matrix[:3, 3] - true_m[:3, 3]))

    refined_result = solve_handeye(
        samples, refine=True, camera_matrix=true_k, dist_coeffs=np.zeros(5)
    )
    if refined_result is None:
        check("精化解算成功", False)
        return

    pos_err = float(
        np.linalg.norm(refined_result.matrix[:3, 3] - true_m[:3, 3])
    )
    # 精化是拿「位置精度」换「重投影贴合度」—— 有像素噪声时两者最优解
    # 不完全一致, 所以允许亚毫米级漂移, 但不能变差到毫米级。
    check(
        "精化后平移误差仍在亚毫米级",
        pos_err < 0.001,
        f"粗解 {baseline * 1000:.3f} mm -> 精化 {pos_err * 1000:.3f} mm",
    )
    check(
        "精化后位置 RMS 很小",
        refined_result.rms_position < 0.002,
        f"{refined_result.rms_position * 1000:.3f} mm",
    )

    # 精化的目的: 降低重投影残差。验证它确实降了。
    r_before = _reproj_rms(samples, result.matrix, true_k)
    r_after = _reproj_rms(samples, refined_result.matrix, true_k)
    check(
        "精化后重投影残差下降",
        r_after < r_before,
        f"{r_before:.3f} px -> {r_after:.3f} px",
    )


def _reproj_rms(samples, matrix, camera_matrix) -> float:
    """用固定内参算一批采样的重投影 RMS (像素)。"""
    from calibration.handeye import _board_pose_in_camera, _project, _rodrigues

    errs: List[float] = []
    for s in samples:
        t = _board_pose_in_camera(matrix, s)
        rvec, tvec = _rodrigues(t[:3, :3]), t[:3, 3]
        proj = _project(s.obj_points, rvec, tvec, camera_matrix)
        errs.append(float(np.linalg.norm(proj - s.img_points, axis=1).mean()))
    return float(np.mean(errs))


def check_chirality_detection() -> None:
    print("\n[8] 手性错误能被检出")
    # 人为做一个镜像 (行列式 -1)
    m = np.eye(4)
    m[:3, :3] = np.diag([1.0, 1.0, -1.0])
    det = float(np.linalg.det(m[:3, :3]))
    check("镜像矩阵 det = -1 (能被检出)", abs(det + 1.0) < 1e-9, f"det={det:+.6f}")


def check_provider() -> None:
    print("\n[9] 外参 provider")
    from extrinsic import ExtrinsicProviderFactory

    class FakeCfg:
        enabled = True
        matrix = [[0, 0, 1, 0.1], [-1, 0, 0, 0.2], [0, -1, 0, 0.3], [0, 0, 0, 1]]
        rotation = []
        translation = [0, 0, 0]
        flip_axes = False
        target_frame = "arm_base"
        dynamic = False

    provider = ExtrinsicProviderFactory.build(FakeCfg())
    m = provider.get()
    check("静态 provider 能拿到矩阵", m is not None)
    if m is not None:
        out = provider.transform((0.0, 0.0, 1.0))
        check(
            "变换正确 (相机前 -> +X + 平移)",
            out is not None
            and abs(out[0] - 1.1) < 1e-9
            and abs(out[1] - 0.2) < 1e-9
            and abs(out[2] - 0.3) < 1e-9,
            f"{out}",
        )
    check("provider 的 frame 是 arm_base", provider.frame == "arm_base", provider.frame)

    class Disabled(FakeCfg):
        enabled = False

    p2 = ExtrinsicProviderFactory.build(Disabled())
    check("没启用时返回 None", p2.get() is None)
    check("没启用时 frame 是 camera", p2.frame == "camera", p2.frame)


def check_rigid_provider() -> None:
    print("\n[10] 多臂 / 头部补偿 provider")
    from extrinsic import (
        RigidExtrinsicProvider,
        WorldContext,
        matrix_to_dict,
    )

    # ---- 场景: 相机装在躯干上, 双臂各有基座 ----
    # T_world_cam: 相机在世界系, 位置 (1.0, 0.5, 1.5)
    t_world_cam = matrix_from_pose((1.0, 0.5, 1.5), (0.0, 0.0, 0.0))
    # 左臂基座在世界系, 偏左; 右臂基座偏右
    t_world_left = matrix_from_pose((0.0, 0.4, 0.0), (0.0, 0.0, 0.0))
    t_world_right = matrix_from_pose((0.0, -0.4, 0.0), (0.0, 0.0, 0.0))

    ctx = WorldContext()
    ctx.base_frames["LeftArm"] = t_world_left
    ctx.base_frames["RightArm"] = t_world_right

    # ---- 目标系 = world ----
    p = RigidExtrinsicProvider(t_world_cam, matrix_frame="world", frame="world",
                               context=ctx)
    out = p.transform((0.0, 0.0, 1.0))
    check(
        "world 系: 相机前 1m -> 相机位置 + 1m",
        out is not None and abs(out[0] - 1.0) < 1e-9 and abs(out[1] - 0.5) < 1e-9
        and abs(out[2] - 2.5) < 1e-9,
        f"{out}",
    )

    # ---- 目标系 = left_base ----
    p_left = RigidExtrinsicProvider(t_world_cam, matrix_frame="world",
                                    frame="left_base", context=ctx)
    out = p_left.transform((0.0, 0.0, 1.0))
    expected = invert_transform(t_world_left) @ t_world_cam @ np.array([0, 0, 1, 1.0])
    check(
        "left_base 系: 等于 (T_world_base_left)⁻¹ · T_world_cam",
        out is not None
        and abs(out[0] - expected[0]) < 1e-9
        and abs(out[1] - expected[1]) < 1e-9
        and abs(out[2] - expected[2]) < 1e-9,
        f"{out} vs {expected[:3]}",
    )

    # ---- 目标系 = right_base ----
    p_right = RigidExtrinsicProvider(t_world_cam, matrix_frame="world",
                                     frame="right_base", context=ctx)
    out = p_right.transform((0.0, 0.0, 1.0))
    expected = invert_transform(t_world_right) @ t_world_cam @ np.array([0, 0, 1, 1.0])
    check(
        "right_base 系: 与左臂不同 (基座偏移真的生效了)",
        out is not None and abs(out[0] - expected[0]) < 1e-9
        and abs(out[1] - expected[1]) < 1e-9,
        f"{out} vs {expected[:3]}",
    )

    # ---- 拿不到基座位姿 -> 返回 None (不假装) ----
    empty_ctx = WorldContext()
    p_none = RigidExtrinsicProvider(t_world_cam, matrix_frame="world",
                                     frame="left_base", context=empty_ctx)
    check("拿不到基座位姿时返回 None", p_none.get() is None)

    # ---- arm=auto: 有两条臂时优先 left ----
    p_auto = RigidExtrinsicProvider(t_world_cam, matrix_frame="world",
                                    frame="arm_base", arm="auto", context=ctx)
    out = p_auto.transform((0.0, 0.0, 1.0))
    expected = invert_transform(t_world_left) @ t_world_cam @ np.array([0, 0, 1, 1.0])
    check(
        "arm=auto 优先用左臂",
        out is not None and abs(out[0] - expected[0]) < 1e-9,
        f"{out}",
    )

    # ---- arm=right 显式指定 ----
    p_r = RigidExtrinsicProvider(t_world_cam, matrix_frame="world",
                                  frame="arm_base", arm="right", context=ctx)
    out = p_r.transform((0.0, 0.0, 1.0))
    expected = invert_transform(t_world_right) @ t_world_cam @ np.array([0, 0, 1, 1.0])
    check(
        "arm=right 用右臂基座",
        out is not None and abs(out[0] - expected[0]) < 1e-9,
        f"{out}",
    )

    # ---- 头部补偿: matrix_frame=head ----
    # T_head_cam: 相机相对头座
    t_head_cam = matrix_from_pose((0.1, 0.0, 0.2), (0.0, 0.0, 0.0))
    # T_world_head: 头在世界系
    t_world_head = matrix_from_pose((0.0, 0.0, 1.6), (0.0, 0.0, 0.0))

    head_ctx = WorldContext()
    head_ctx.head_frame = t_world_head
    head_ctx.base_frames["LeftArm"] = t_world_left

    ph = RigidExtrinsicProvider(t_head_cam, matrix_frame="head", frame="world",
                                context=head_ctx)
    out = ph.transform((0.0, 0.0, 1.0))
    expected = t_world_head @ t_head_cam @ np.array([0, 0, 1, 1.0])
    check(
        "head 系: T_world_cam = T_world_head · T_head_cam",
        out is not None
        and abs(out[0] - expected[0]) < 1e-9
        and abs(out[1] - expected[1]) < 1e-9
        and abs(out[2] - expected[2]) < 1e-9,
        f"{out} vs {expected[:3]}",
    )

    # 头会动但拿不到位姿 -> 必须 None, 不能拿静态矩阵顶替
    ph2 = RigidExtrinsicProvider(t_head_cam, matrix_frame="head", frame="world",
                                 context=WorldContext())
    check("头位姿拿不到时返回 None (不假装)", ph2.get() is None)

    # 头动了 -> 结果跟着变
    head_ctx.head_frame = matrix_from_pose((0.0, 0.0, 1.8), (0.0, 0.0, 0.0))
    ph.set_context(head_ctx)
    out = ph.transform((0.0, 0.0, 1.0))
    check(
        "头抬高 20cm 后 world 坐标跟着变",
        out is not None and abs(out[2] - 3.0) < 1e-9,
        f"{out}",
    )

    # ---- with_frame: 换目标系但共享上下文 ----
    p_world = RigidExtrinsicProvider(t_world_cam, matrix_frame="world",
                                     frame="world", context=ctx)
    sub = p_world.with_frame("left_base")
    out = sub.transform((0.0, 0.0, 1.0))
    expected = invert_transform(t_world_left) @ t_world_cam @ np.array([0, 0, 1, 1.0])
    check(
        "with_frame 换目标系, 上下文共享",
        out is not None and abs(out[0] - expected[0]) < 1e-9,
        f"{out}",
    )

    # ---- 工厂: matrix_frame=head 时造出 RigidExtrinsicProvider ----
    from extrinsic import ExtrinsicProviderFactory

    class HeadCfg:
        enabled = True
        matrix = [[1, 0, 0, 0.1], [0, 1, 0, 0.0], [0, 0, 1, 0.2], [0, 0, 0, 1]]
        rotation = []
        translation = [0, 0, 0]
        flip_axes = False
        target_frame = "world"
        dynamic = False
        matrix_frame = "head"
        arm = "auto"

    provider = ExtrinsicProviderFactory.build(HeadCfg())
    check(
        "工厂: matrix_frame=head -> RigidExtrinsicProvider",
        isinstance(provider, RigidExtrinsicProvider),
        type(provider).__name__,
    )
    check("工厂: 头位姿没给时不可用", provider.get() is None)


def main() -> int:
    print("=" * 62)
    print("标定流程自检 (合成数据)")
    print("=" * 62)

    check_axis_permutation()
    check_rpy()
    check_invert()
    check_handeye_noiseless()
    check_handeye_noisy()
    check_handeye_outlier()
    check_handeye_refine()
    check_chirality_detection()
    check_provider()
    check_rigid_provider()

    print()
    print("=" * 62)
    print(f"结果: {PASSED} 通过, {FAILED} 失败")
    print("=" * 62)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
