#!/usr/bin/env python3
"""标定「会话」流程的自检 —— 存图 / 存取 / 离线重解 的闭环验证。

**为什么要有这个**

新加的「存图 + 离线重解」链路有好几个容易出错的接缝:

- 图片存下来了, 但 JSON 里没记路径 -> 离线重解找不到图
- JSON 记了路径, 但相对路径算错了 -> 换个目录就打不开
- 存的是**绝对路径** -> 换台机器就废了
- 离线重解算出来的结果, 跟采样时算的不一样 -> 说明重解逻辑有 bug
- 旧版采样格式 (裸数组) 读不了 -> 老的 samples.json 全废

这里用**合成数据**把这几条都验一遍 —— 不需要相机、不需要机械臂,
几秒钟就能跑完。

用法::

    conda run -n yolo python tools/check_session.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.board import (  # noqa: E402
    BoardSpec,
    board_object_points,
    generate_board_image,
)
from calibration.handeye import PoseSample, solve_handeye  # noqa: E402
from calibration.session import (  # noqa: E402
    SessionData,
    load_session,
    save_session,
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


def make_synthetic_session(
    n: int = 12,
    tool_offset: Optional[np.ndarray] = None,
    recorded_offset: Optional[np.ndarray] = None,
    seed: int = 42,
) -> Tuple[SessionData, List[np.ndarray], np.ndarray]:
    """造一个合成 session: 板图 + 相机位姿 + 工具位姿。

    **物理模型** (很重要, 别搞反):

    - 控制器给的 ``t_world_tool`` 是**独立**的 —— 它取决于机械臂摆到哪,
      与板怎么装**无关**。
    - 板的世界位姿 = ``t_world_tool · t_tool_board_真实`` (板刚性固定在工具上)
    - 板在相机系的位姿 = ``inv(T_world_cam) · t_world_board``

    ``tool_offset`` 是**物理上真实的**板-工具偏置;
    ``recorded_offset`` 是**记录进样本的**偏置 (模拟用户填错)。

    两者不同时 —— 解算用 ``recorded_offset`` 就对不上, 用 ``tool_offset``
    才能解出真值。这正是「``T_tool_board`` 填错了」的模拟。

    返回 ``(session, images, true_T_world_cam)``。
    """
    import cv2

    rng = np.random.default_rng(seed)
    spec = BoardSpec()
    board_pts = board_object_points(spec)
    board_img = generate_board_image(spec, 4000)

    K = np.array([[900.0, 0, board_img.shape[1] / 2],
                  [0, 900.0, board_img.shape[0] / 2],
                  [0, 0, 1.0]])

    # 真值: 相机在机器人躯干上, 朝前略向下
    from extrinsic import rpy_to_matrix

    true_T = np.eye(4)
    true_T[:3, :3] = rpy_to_matrix([0.15, 0.25, 0.0])
    true_T[:3, 3] = [0.05, 0.0, 1.45]

    # 物理上真实的板-工具偏置
    tb_phys = np.eye(4) if tool_offset is None else tool_offset
    # 记录进样本的偏置 (默认与物理一致)
    tb_rec = tb_phys if recorded_offset is None else recorded_offset

    samples: List[PoseSample] = []
    images: List[np.ndarray] = []

    for i in range(n):
        # 控制器给的工具位姿 (独立于板怎么装)
        t_world_tool = np.eye(4)
        t_world_tool[:3, :3] = rpy_to_matrix(
            [rng.uniform(-0.6, 0.6), rng.uniform(-0.6, 0.6), rng.uniform(-3.0, 3.0)]
        )
        t_world_tool[:3, 3] = [
            rng.uniform(-0.3, 0.3), rng.uniform(-0.25, 0.25), 0.4 + 0.8 * rng.random()
        ]

        # 板的世界位姿 = 工具位姿 · **物理真实**的偏置
        t_world_board = t_world_tool @ tb_phys

        # 板在相机系的位姿 (相机看到的就是这个)
        t_cam_board = np.linalg.inv(true_T) @ t_world_board

        # **板必须在相机前方** (Z > 0) —— 否则投影会翻到图像后面,
        # solvePnP / 精化全都失去意义。真实场景里相机在机器人头部,
        # 板挂在末端, 必然在相机前方。
        #
        # 做法: 先随机生成板在**相机系**里的位姿 (保证 Z > 0),
        # 再反推工具位姿 —— 这样「控制器给的位姿」和「相机看到的」
        # 天然自洽, 且几何合法。
        dist = 0.4 + 0.8 * rng.random()
        ang = np.array([
            rng.uniform(-0.6, 0.6),
            rng.uniform(-0.6, 0.6),
            rng.uniform(-3.0, 3.0),
        ])
        rot, _ = cv2.Rodrigues(ang)
        t_cam_board = np.eye(4)
        t_cam_board[:3, :3] = rot
        t_cam_board[:3, 3] = [
            rng.uniform(-0.3, 0.3), rng.uniform(-0.25, 0.25), dist
        ]

        # 反推: 板的世界位姿已知 -> 工具位姿 = 板的世界位姿 · 偏置的逆
        t_world_board = true_T @ t_cam_board
        t_world_tool = t_world_board @ np.linalg.inv(tb_phys)

        # 角点投影 (就是「检测」出来的像素)
        proj, _ = cv2.projectPoints(
            board_pts, t_cam_board[:3, :3], t_cam_board[:3, 3], K, None
        )
        img_pts = proj.reshape(-1, 2)

        # 画一张「图」: 把板图按位姿贴上去
        image = _render_board(board_img, t_cam_board, K)

        samples.append(
            PoseSample(
                t_cam_board=t_cam_board,
                t_world_tool=t_world_tool,
                t_tool_board=tb_rec.copy(),
                obj_points=board_pts.copy(),
                img_points=img_pts.astype(np.float64),
                corner_count=len(board_pts),
                label="SyntheticArm",
                image_path="",
                board_distance_m=float(abs(t_cam_board[2, 3])),
            )
        )
        images.append(image)

    session = SessionData(
        samples=samples,
        board=spec,
        camera_matrix=K,
        dist_coeffs=None,
        tool_offset=tb_rec,
        arm="SyntheticArm",
    )
    return session, images, true_T


def _render_board(board_img: np.ndarray, t_cam_board: np.ndarray, K: np.ndarray):
    """把板图按给定位姿渲染到一张画布上。

    真实流程里角点是 OpenCV 检出来的, 这里按**已知对应关系**把板图
    透视变换过去 —— 保证「重检测」能拿到一样的结果。

    注意: ``board_img`` 是单通道 (灰度), 而画布是三通道, 所以要转。
    """
    import cv2

    if board_img.ndim == 2:
        board_img = cv2.cvtColor(board_img, cv2.COLOR_GRAY2BGR)

    h, w = board_img.shape[:2]
    ppm = 4000.0
    canvas = np.full((h, w, 3), 40, dtype=np.uint8)

    # 打印图的四角 -> 板系坐标 (像素 / ppm)
    corners_obj = np.array(
        [[0.0, 0.0, 0.0],
         [w / ppm, 0.0, 0.0],
         [w / ppm, h / ppm, 0.0],
         [0.0, h / ppm, 0.0]],
        dtype=np.float64,
    )
    pts, _ = cv2.projectPoints(
        corners_obj, t_cam_board[:3, :3], t_cam_board[:3, 3], K, None
    )
    pts = pts.reshape(-1, 2).astype(np.float32)

    src = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
    try:
        M = cv2.getPerspectiveTransform(src, pts)
        warped = cv2.warpPerspective(
            board_img, M, (w, h), borderValue=(40, 40, 40)
        )
        mask = cv2.warpPerspective(
            np.full((h, w), 255, np.uint8), M, (w, h), borderValue=0
        )
        m = mask > 0
        canvas[m] = warped[m]
    except Exception as exc:
        print(f"渲染失败: {exc}", file=sys.stderr)
    return canvas


# ------------------------------------------------------------ 检查项


def check_session_roundtrip() -> None:
    print("\n[1] session 存取往返")
    tmp = Path(tempfile.mkdtemp(prefix="calib_session_"))
    try:
        session, images, true_T = make_synthetic_session()
        path = tmp / "samples.json"
        save_session(session, path, images=images)

        check("session.json 已生成", path.exists())
        img_dir = tmp / "images"
        check("images/ 目录已生成", img_dir.is_dir())
        n_png = len(list(img_dir.glob("*.png")))
        check("图片数量与采样数一致", n_png == len(session.samples),
              f"{n_png} vs {len(session.samples)}")

        loaded = load_session(path)
        check("采样数一致", len(loaded.samples) == len(session.samples),
              f"{len(loaded.samples)}")
        check("板参数已存下", loaded.board is not None
              and abs(loaded.board.square_length - 0.040) < 1e-9)
        check("内参已存下", loaded.camera_matrix is not None
              and loaded.camera_matrix.shape == (3, 3))
        check("工具偏置已存下", loaded.tool_offset is not None
              and loaded.tool_offset.shape == (4, 4))
        check("图片路径已记下", all(s.image_path for s in loaded.samples))
        check("图片路径是相对路径", all(
            not Path(s.image_path).is_absolute() for s in loaded.samples
        ))
        check("has_images() 为真", loaded.has_images())
        check("resolve_image 能定位到文件", all(
            loaded.resolve_image(s.image_path).exists() for s in loaded.samples
        ))

        # 矩阵一致
        m0 = session.samples[0].t_cam_board
        m1 = loaded.samples[0].t_cam_board
        check("矩阵往返无损", float(np.abs(m0 - m1).max()) < 1e-12)

        # **几何合法性**: 板必须在相机前方 (Z > 0)。
        # 板在相机后面时投影会翻到图像外, solvePnP 和精化全都失去意义 ——
        # 合成数据必须保证这一点, 否则自检本身就在测一个假场景。
        zs = [float(s.t_cam_board[2, 3]) for s in loaded.samples]
        check("板都在相机前方 (Z > 0)", all(z > 0 for z in zs),
              f"最小 Z = {min(zs):+.4f} m")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_offline_resolve() -> None:
    print("\n[2] 离线重解 = 采样时的解")
    from calib_handeye import resolve_offline

    tmp = Path(tempfile.mkdtemp(prefix="calib_offline_"))
    try:
        session, images, true_T = make_synthetic_session()
        path = tmp / "samples.json"
        save_session(session, path, images=images)

        loaded = load_session(path)
        r0 = solve_handeye(loaded.samples)
        r1 = solve_handeye(resolve_offline(loaded))

        check("两次解算都有结果", r0 is not None and r1 is not None)
        if r0 and r1:
            dp = float(np.linalg.norm(r0.matrix[:3, 3] - r1.matrix[:3, 3]))
            check("位置完全一致", dp < 1e-12, f"差 {dp * 1000:.6f} mm")
            dr = float(np.abs(r0.matrix[:3, :3] - r1.matrix[:3, :3]).max())
            check("旋转完全一致", dr < 1e-12, f"差 {dr:.2e}")

            # 与真值比
            dp_true = float(np.linalg.norm(r0.matrix[:3, 3] - true_T[:3, 3]))
            check("解出的外参接近真值", dp_true < 1e-6,
                  f"位置差 {dp_true * 1000:.4f} mm")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_refine_consistency() -> None:
    print("\n[2b] 精化不能把结果弄坏")
    from calibration.handeye import _refine

    tmp = Path(tempfile.mkdtemp(prefix="calib_refine_"))
    try:
        session, images, true_T = make_synthetic_session()
        path = tmp / "samples.json"
        save_session(session, path, images=images)
        loaded = load_session(path)

        coarse = solve_handeye(loaded.samples, refine=False)
        check("粗解成功", coarse is not None)
        if coarse is None:
            return

        dp_coarse = float(np.linalg.norm(coarse.matrix[:3, 3] - true_T[:3, 3]))
        check("粗解接近真值", dp_coarse < 1e-3, f"位置差 {dp_coarse * 1000:.4f} mm")

        refined = _refine(
            loaded.samples, coarse.matrix, loaded.camera_matrix, loaded.dist_coeffs
        )
        dp_ref = float(np.linalg.norm(refined[:3, 3] - true_T[:3, 3]))
        check("精化后仍然接近真值", dp_ref < 1e-3, f"位置差 {dp_ref * 1000:.4f} mm")

        # 精化只能让重投影残差下降, 不能让外参跑偏
        dr = float(np.linalg.norm(refined[:3, 3] - coarse.matrix[:3, 3]))
        check("精化没有跑偏", dr < 1e-3, f"偏离粗解 {dr * 1000:.4f} mm")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_tool_offset_override() -> None:
    print("\n[3] 离线重解可以覆盖工具偏置 (T_tool_board 填错时的救星)")
    from calib_handeye import resolve_offline

    tmp = Path(tempfile.mkdtemp(prefix="calib_offset_"))
    try:
        # **物理上板是垂直固定的** (真实偏置不是单位阵)
        tb_true = np.eye(4)
        tb_true[:3, :3] = np.array(
            [[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        )
        tb_true[:3, 3] = [0.05, 0.0, 0.12]

        # **采样时填错了** —— 记录进样本的是单位阵
        session, images, true_T = make_synthetic_session(
            n=14, tool_offset=tb_true, recorded_offset=np.eye(4)
        )
        path = tmp / "samples.json"
        save_session(session, path, images=images)
        loaded = load_session(path)

        # 用采样时 (错的) 偏置 -> 解不出来
        r_bad = solve_handeye(resolve_offline(loaded))
        check("错的偏置 -> 解算失败 (预期)", r_bad is None,
              "却解出来了" if r_bad is not None else "")

        # 离线换成正确的偏置 -> 解出真值
        r_fix = solve_handeye(resolve_offline(loaded, tool_offset=tb_true))
        check("换成正确偏置后能解算", r_fix is not None)
        if r_fix:
            dp = float(np.linalg.norm(r_fix.matrix[:3, 3] - true_T[:3, 3]))
            check("解出的是真值外参", dp < 1e-6, f"位置差 {dp * 1000:.4f} mm")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_exclude() -> None:
    print("\n[4] 离线重解可以剔除样本")
    from calib_handeye import resolve_offline

    tmp = Path(tempfile.mkdtemp(prefix="calib_excl_"))
    try:
        session, images, _ = make_synthetic_session()
        path = tmp / "samples.json"
        save_session(session, path, images=images)
        loaded = load_session(path)

        out = resolve_offline(loaded, exclude=[0, 2, 5])
        check("剔掉了 3 个", len(out) == len(loaded.samples) - 3,
              f"{len(out)} vs {len(loaded.samples)}")
        # 被剔掉的那些不该出现在结果里
        check("索引对得上", all(
            s is not loaded.samples[i] for i, s in enumerate(out)
            if i < 2
        ))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_legacy_format() -> None:
    print("\n[5] 兼容旧版采样格式 (裸数组)")
    tmp = Path(tempfile.mkdtemp(prefix="calib_legacy_"))
    try:
        session, images, _ = make_synthetic_session()
        # 构造旧格式: 直接是数组, 没有 meta, 没有 image
        legacy = [
            {
                "t_cam_board": s.t_cam_board.tolist(),
                "t_world_tool": s.t_world_tool.tolist(),
                "t_tool_board": s.t_tool_board.tolist(),
                "obj_points": s.obj_points.tolist(),
                "img_points": s.img_points.tolist(),
                "corner_count": s.corner_count,
                "label": s.label,
            }
            for s in session.samples
        ]
        path = tmp / "legacy.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(legacy, fh)

        loaded = load_session(path)
        check("旧格式能读出采样", len(loaded.samples) == len(legacy),
              f"{len(loaded.samples)}")
        check("旧格式没有板参数", loaded.board is None)
        check("旧格式没有图片", not loaded.has_images())

        r = solve_handeye(loaded.samples)
        check("旧格式也能解算", r is not None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_redetect() -> None:
    print("\n[6] 从保存的图重新检测角点")
    from calibration.capture import redetect_from_image

    tmp = Path(tempfile.mkdtemp(prefix="calib_redet_"))
    try:
        session, images, true_T = make_synthetic_session(n=6)
        path = tmp / "samples.json"
        save_session(session, path, images=images)
        loaded = load_session(path)

        spec = loaded.board
        n_ok = 0
        for i, s in enumerate(loaded.samples):
            p = loaded.resolve_image(s.image_path)
            res = redetect_from_image(p, spec, loaded.camera_matrix, None, min_corners=1)
            if res is not None:
                n_ok += 1
        check("重检测能跑通", n_ok > 0, f"{n_ok}/{len(loaded.samples)} 帧有结果")
        check("重检测至少覆盖一半", n_ok >= len(loaded.samples) // 2,
              f"{n_ok}/{len(loaded.samples)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_view_tool() -> None:
    print("\n[7] 可视化工具能出图")
    tmp = Path(tempfile.mkdtemp(prefix="calib_view_"))
    try:
        session, images, _ = make_synthetic_session(n=4)
        path = tmp / "samples.json"
        save_session(session, path, images=images)

        import subprocess

        out_dir = tmp / "views"
        script = Path(__file__).resolve().parent / "calib_view.py"
        proc = subprocess.run(
            [sys.executable, str(script), "--samples", str(path),
             "--out-dir", str(out_dir), "--montage"],
            capture_output=True, text=True, timeout=120,
        )
        check("calib_view.py 正常退出", proc.returncode == 0,
              proc.stderr.strip()[-200:] if proc.returncode else "")
        n_png = len(list(out_dir.glob("*.png"))) if out_dir.is_dir() else 0
        check("每帧一张叠加图", n_png >= 4, f"{n_png} 张")
        check("montage.png 已生成", (out_dir / "montage.png").exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_stdin_detection() -> None:
    print("\n[8] stdin 交互检测 (conda run 会切断 stdin)")
    import io
    import subprocess

    from calibration.interactive import stdin_is_interactive

    # 有数据 -> 视为可交互
    old = sys.stdin
    try:
        # 要用真的 BufferedReader 套 TextIOWrapper —— 因为检测逻辑走的是
        # ``sys.stdin.buffer.peek()``, 得有真实的 buffer 层
        def _make_stdin(payload: bytes):
            return io.TextIOWrapper(io.BufferedReader(io.BytesIO(payload)))

        sys.stdin = _make_stdin(b"\n")
        check("管道里有数据 -> 可交互", stdin_is_interactive())

        # 空 -> EOF -> 不可交互
        sys.stdin = _make_stdin(b"")
        check("EOF -> 判为不可交互", not stdin_is_interactive())

        sys.stdin = None
        check("stdin 为 None -> 判为不可交互", not stdin_is_interactive())
    finally:
        sys.stdin = old

    # 真跑一遍 conda run, 确认它确实切断 stdin (这是本 bug 的根因)
    code = (
        "import sys; sys.path.insert(0, %r); "
        "from calibration.interactive import stdin_is_interactive; "
        "print('INTERACTIVE' if stdin_is_interactive() else 'EOF')"
    ) % str(Path(__file__).resolve().parents[1])

    # conda 的位置不写死 —— 换机器/换用户名时 shutil.which 找不到,
    # 再退回常见安装位置。
    conda = shutil.which("conda") or ""
    if not conda:
        home = Path.home()
        for candidate in (
            home / "miniconda3" / "bin" / "conda",
            home / "anaconda3" / "bin" / "conda",
            home / "miniforge3" / "bin" / "conda",
        ):
            if candidate.exists():
                conda = str(candidate)
                break
    if not conda or not Path(conda).exists():
        check("找得到 conda", False, conda or "(没找到)")
        return

    try:
        # 明确喂一个 "\n" 进去 —— 这样「能不能读到」就只取决于
        # conda run 有没有切断 stdin, 不受父进程 stdin 状态影响。
        r1 = subprocess.run(
            [conda, "run", "-n", "yolo", "python", "-c", code],
            input=b"\n", capture_output=True, timeout=300,
        )
        out1 = (r1.stdout + r1.stderr).decode("utf-8", "replace")
        check("conda run 下 stdin 是 EOF (喂了数据也读不到)",
              "EOF" in out1 and "INTERACTIVE" not in out1,
              out1.strip()[-120:])

        r2 = subprocess.run(
            [conda, "run", "--no-capture-output", "-n", "yolo",
             "python", "-c", code],
            input=b"\n", capture_output=True, timeout=300,
        )
        out2 = (r2.stdout + r2.stderr).decode("utf-8", "replace")
        check("--no-capture-output 下 stdin 能读到 (喂了数据就读得到)",
              "INTERACTIVE" in out2, out2.strip()[-120:])
    except Exception as exc:
        check("能跑 conda run 做对照实验", False, f"{type(exc).__name__}: {exc}")


def check_stale_pose_detection() -> None:
    """**位姿陈旧/冻结的检测** —— 上次标定翻车的根因。

    **背景**: 控制器断连后, arm_service 仍然返回 200 + 断连前的快照。
    25 个采样的 ``T_world_tool`` 完全相同, 解算时被当成野值剔到只剩 1 个,
    排查花了很久。

    这里验证三层防线:
    1. ``stale`` 标记能被识别
    2. 控制器断开 (没有 stale 字段的老版本) 也能识别
    3. **位姿冻结** (快照很新但内容不动) 也能识别
    """
    print("\n[9] 位姿陈旧 / 冻结的检测")
    from calibration.capture import ArmKinematicsClient, StalePoseError

    # ---- 1) stale 标记 ----
    snap_stale = {
        "ok": False,
        "arms": {"RightArm": {"cartesian": {"x": 0.5, "y": 0.0, "z": 0.3,
                                             "a": 0.1, "b": 0.2, "c": 0.3}}},
        "controller_connected": False,
        "stale": True,
        "notes": ["控制器已断开"],
    }
    check("stale=True 能被识别", ArmKinematicsClient.is_stale(snap_stale))

    # ---- 2) 老版本没有 stale 字段, 靠 controller_connected ----
    snap_legacy = dict(snap_stale)
    snap_legacy.pop("stale")
    check("没有 stale 字段时靠 controller_connected 判断",
          ArmKinematicsClient.is_stale(snap_legacy))

    snap_ok = {
        "ok": True,
        "arms": {"RightArm": {"cartesian": {"x": 0.5, "y": 0.0, "z": 0.3,
                                             "a": 0.1, "b": 0.2, "c": 0.3}}},
        "controller_connected": True,
        "stale": False,
    }
    check("正常快照不被误判", not ArmKinematicsClient.is_stale(snap_ok))

    # ---- 3) require_fresh 遇到陈旧快照要抛异常 ----
    client = ArmKinematicsClient()
    try:
        client.require_fresh(snap_stale, "RightArm")
        check("陈旧快照 -> require_fresh 抛异常", False, "竟然没抛")
    except StalePoseError:
        check("陈旧快照 -> require_fresh 抛异常", True)

    # ---- 4) **位姿冻结** (快照是新的, 但内容纹丝不动) ----
    frozen = {
        "ok": True,
        "arms": {"RightArm": {"cartesian": {"x": 0.5, "y": 0.0, "z": 0.3,
                                             "a": 0.1, "b": 0.2, "c": 0.3}}},
        "controller_connected": True,
        "stale": False,
    }
    client2 = ArmKinematicsClient()
    # 第一次取 -> 成功 (建立基线)
    try:
        client2.require_fresh(frozen, "RightArm")
        check("第一次取位姿正常", True)
    except StalePoseError as exc:
        check("第一次取位姿正常", False, str(exc))

    # 第二次取 -> 完全一样 -> 应该报「冻结」
    import time as _time

    client2._last_pose_time = _time.monotonic() - 1.0  # 假装过了 1 秒
    try:
        client2.require_fresh(frozen, "RightArm")
        check("位姿冻结 -> require_fresh 抛异常", False, "竟然没抛")
    except StalePoseError as exc:
        check("位姿冻结 -> require_fresh 抛异常", True, str(exc)[:60])

    # ---- 5) 位姿变了 -> 不该报冻结 ----
    client3 = ArmKinematicsClient()
    client3.require_fresh(frozen, "RightArm")
    moved = {
        "ok": True,
        "arms": {"RightArm": {"cartesian": {"x": 0.6, "y": 0.0, "z": 0.3,
                                             "a": 0.1, "b": 0.2, "c": 0.3}}},
        "controller_connected": True,
        "stale": False,
    }
    client3._last_pose_time = _time.monotonic() - 1.0
    try:
        client3.require_fresh(moved, "RightArm")
        check("位姿变了 -> 不误报冻结", True)
    except StalePoseError as exc:
        check("位姿变了 -> 不误报冻结", False, str(exc))

    # ---- 6) **离线重解也不该用陈旧数据** ----
    # 造一批「位姿全同」的样本, 确认解算会失败 (而不是给出一个假结果)
    session, images, true_T = make_synthetic_session(n=8)
    for s in session.samples:
        s.t_world_tool = session.samples[0].t_world_tool.copy()  # 全部冻结
    r = solve_handeye(session.samples)
    check("位姿冻结的样本 -> 解算失败 (而不是给出假结果)", r is None,
          "却解出来了" if r is not None else "")


def main() -> int:
    print("=" * 62)
    print("标定 session 流程自检 (合成数据)")
    print("=" * 62)

    check_session_roundtrip()
    check_offline_resolve()
    check_refine_consistency()
    check_tool_offset_override()
    check_exclude()
    check_legacy_format()
    check_redetect()
    check_view_tool()
    check_stdin_detection()
    check_stale_pose_detection()

    print()
    print("=" * 62)
    print(f"结果: {PASSED} 通过, {FAILED} 失败")
    print("=" * 62)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
