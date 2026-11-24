#!/usr/bin/env python3
"""手眼标定 (eye-to-hand): 板挂工具, 用控制器正解当量尺。

用法::

    # 采 20 个位姿并解外参, 结果写到 configs/handeye.json
    # ⚠️ 必须加 --no-capture-output —— conda run 会切断 stdin,
    #    表现为「刚打印提示符就立刻退出, 一个样本都没采到」。
    conda run --no-capture-output -n yolo python tools/calib_handeye.py --capture

    # 换个臂 (双胞胎要分别标)
    conda run --no-capture-output -n yolo python tools/calib_handeye.py --capture --arm right

    # 从已采的样本重解 (不重新采)
    conda run -n yolo python tools/calib_handeye.py --samples /tmp/handeye_samples.json

**离线重解 (不动机械臂、不再拍照)**:

采样时会把**原始彩色图**和**位姿**一起存下来, 所以之后可以反复重算:

    # 用采样时存下的角点直接重解
    conda run -n yolo python tools/calib_handeye.py --samples /tmp/handeye_samples.json

    # 从保存的图片**重新检测角点**再解 (换了检测参数时用)
    conda run -n yolo python tools/calib_handeye.py --samples /tmp/handeye_samples.json --redetect

    # 换一个工具偏置重解 (比如发现 T_tool_board 填反了)
    conda run -n yolo python tools/calib_handeye.py --samples /tmp/handeye_samples.json \
        --tool-offset /tmp/tool_offset.json

    # 只用部分样本 (剔除坏帧)
    conda run -n yolo python tools/calib_handeye.py --samples /tmp/handeye_samples.json \
        --exclude 3,7,12

**工具偏置 (``--tool-offset``)**:

``T_tool_board`` = 板相对工具的固定偏置 (4x4)。常规解算**要求它已知** ——
板原点离工具原点 340 mm 的话, 填错会让所有帧解出的外参互相打架,
报「剔野值后只剩 0 个采样」。

偏置文件格式 (``{"matrix": [[4x4]]}``)。**文件不存在会直接报错**,
不会静默用单位阵 (以前会, 导致白采 30 个位姿)。

不确定偏置? **不加** ``--tool-offset`` 参数 —— 常规解算失败时脚本会自动
尝试**联合解算** (同时求 ``T_tool_board`` 和 ``T_world_cam``), 并把结果
存成 ``configs/tool_board.json``, 下次采集直接用。

    # 看看每一帧长什么样 (角点画在图上)
    conda run -n yolo python tools/calib_view.py --samples /tmp/handeye_samples.json

**前置条件**:
1. 内参已标定 (tools/calib_intrinsics.py) —— 不然精度上不去
2. ChArUco 板**固定在工具上** (用打印模板对齐, 或销钉定位)
3. 工具坐标系的**物理尖点**与板原点重合 (或用 --tool-offset 填偏置)
4. arm_service 在跑 (它提供控制器的世界系位姿)

**采样要点**:
- 20~30 个位姿, 在工作空间里**姿态多样化** (三个轴各 ±30° 以上)
- 不要都在一个平面附近 —— 旋转不够多样, 解不出的旋转轴
- 避开: 7 轴冗余导致的肘部翻转、奇异位形、关节软限位附近
- 板与相机的距离要在深度相机有效范围内 (Gemini 336L 约 0.2 m 起)
- 每个位姿要**停稳**再采 (脚本会等)

**重要**: 相机装在机器人**头部**时, 如果头部可以转动, 静态外参不成立。
先确认头部是固定的再往下做。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.board import BoardSpec  # noqa: E402
from calibration.capture import (  # noqa: E402
    ArmKinematicsClient,
    CameraCapture,
    StalePoseError,
    capture_sample,
    redetect_from_image,
)
from calibration.handeye import PoseSample, solve_handeye  # noqa: E402
from calibration.paths import config_path  # noqa: E402
from paths import conda_env_python  # noqa: E402
from calibration.session import (  # noqa: E402
    SessionData,
    load_session,
    save_session,
)


def load_spec() -> BoardSpec:
    path = config_path("calibration.yaml")
    if not path.exists():
        return BoardSpec()
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        section = data.get("vision_calibration", data)
        return BoardSpec.from_dict(section.get("board", {}))
    except Exception:
        return BoardSpec()


def load_intrinsics() -> Optional[Dict[str, Any]]:
    """读标定好的内参 (configs/intrinsics.json)。"""
    path = config_path("intrinsics.json")
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def load_tool_offset(path: Optional[str]) -> np.ndarray:
    """读板相对工具的偏置 (4x4)。

    如果板原点与工具物理尖点重合, 就是单位阵。

    **文件不存在时必须报错, 不能静默回退到单位阵**。

    为什么: 偏置填错 (或者文件路径打错) 时, 单位阵会让「板原点 = 工具原点」
    这个假设悄悄生效 —— 解算会得到一个**看起来有结果但完全错误**的解,
    或者干脆报「剔野值后只剩 0 个」让人一头雾水。

    上次就踩了这个坑: ``--tool-offset /tmp/tool_offset.json`` 的文件根本
    不存在, 脚本只打了个警告就用了单位阵, 板实际装在工具外 285 mm 处,
    结果整批数据不自洽, 白采了 15 帧。

    所以这里直接抛异常 —— 早失败比晚困惑好。
    """
    if not path:
        return np.eye(4, dtype=np.float64)
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"工具偏置文件不存在: {p}\n"
            f"  这不是可以忽略的小问题 —— 缺了它就会默认「板原点 = 工具原点」,\n"
            f"  解出来的结果会完全错。\n"
            f"  要么创建这个文件 (4x4 矩阵, 格式见 docs/calibration.md),\n"
            f"  要么不加 --tool-offset 参数 (仅当板原点确实与工具尖点重合时)。"
        )
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        m = np.array(data["matrix"], dtype=np.float64)
        if m.shape != (4, 4):
            raise ValueError("不是 4x4")
        return m
    except Exception as exc:
        raise ValueError(
            f"读 tool offset 失败 ({exc}): {p}\n"
            f"  格式应该是 {{\"matrix\": [[...4x4...]]}}"
        ) from exc


def _parse_indices(text: str) -> List[int]:
    """解析 "1,3,5" 或 "1-4" 形式的索引列表。"""
    out: List[int] = []
    for part in str(text or "").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                out.extend(range(int(a), int(b) + 1))
            except ValueError:
                continue
        else:
            try:
                out.append(int(part))
            except ValueError:
                continue
    return out


# ------------------------------------------------------------ 采样


def _check_stdin_interactive() -> bool:
    """检查 stdin 能不能交互。

    **这是 ``conda run`` 的坑**: ``conda run`` 默认捕获输出, 会让子进程的
    stdin 直接变成 EOF —— 于是脚本第一句 ``input()`` 就抛 ``EOFError``,
    表现为「刚打印提示符就立刻退出, 一个样本都没采到」。

    解决办法: 用 ``conda run --no-capture-output`` (或者直接用环境里的
    python, 不套 conda run)。

    这里提前检查并给出明确提示, 别让人以为是相机或机械臂的问题。
    """
    from calibration.interactive import require_interactive_stdin

    return require_interactive_stdin("tools/calib_handeye.py --capture --arm right")


def capture_samples(
    count: int,
    arm: str,
    arm_url: str,
    tool_offset: np.ndarray,
    dist_coeffs: Optional[np.ndarray],
    spec: Optional[BoardSpec] = None,
) -> Tuple[List[PoseSample], List[Optional[np.ndarray]], List[Dict[str, Any]]]:
    """交互式采样。

    返回 ``(samples, images, rejected)`` —— ``images`` 与 ``samples``
    一一对应 (BGR 原始图), ``rejected`` 记着被跳过的尝试 (角点不够等)。
    """
    from camera import OrbbecCamera
    from config import load_config

    spec = spec or load_spec()
    cfg = load_config()
    cam = OrbbecCamera(cfg.camera)
    print("正在打开相机...", file=sys.stderr)
    try:
        cam.open()
    except Exception as exc:
        print(f"相机打不开: {exc}", file=sys.stderr)
        return [], [], []

    capture = CameraCapture(cam)
    client = ArmKinematicsClient(arm_url)

    # 用标定好的内参 (如果有)
    camera_matrix = capture.camera_matrix()
    intr = load_intrinsics()
    if intr:
        try:
            camera_matrix = np.array(intr["camera_matrix"], dtype=np.float64)
            if dist_coeffs is None:
                dist_coeffs = np.array(intr["dist_coeffs"], dtype=np.float64)
            print(
                f"用标定内参: fx={camera_matrix[0, 0]:.2f} "
                f"fy={camera_matrix[1, 1]:.2f}",
                file=sys.stderr,
            )
        except Exception as exc:
            print(f"读标定内参失败 ({exc}), 用出厂内参", file=sys.stderr)

    # 先确认臂名
    snap = client.snapshot(refresh=True)
    if not snap:
        print("连不上 arm_service, 退出", file=sys.stderr)
        cam.close()
        return [], [], []

    # **数据陈旧就别开始摆位姿了** —— 摆 30 个姿态很费时间,
    # 拿到的却是冻结的位姿, 等于全白费。
    if client.is_stale(snap):
        notes = "; ".join(snap.get("notes") or [])
        bar = "=" * 62
        print(
            f"""
{bar}
  **控制器数据陈旧 —— 不能开始标定**
{bar}

  {notes or 'arm_service 报告数据是旧的'}

  常见原因:
    - arm_service 与控制器断连了 (看 logs/arm.log)
    - 控制器是仿真模式, cartesian 不随关节变化

  先确认 /v1/kinematics 返回的 stale=false:
    curl -s 'http://127.0.0.1:8802/v1/kinematics?refresh=true' | python3 -m json.tool
{bar}
""",
            file=sys.stderr,
        )
        cam.close()
        return [], [], []

    chosen = client.pick_arm(snap, arm)
    if not chosen:
        print("arm_service 里没有机器人", file=sys.stderr)
        cam.close()
        return [], [], []
    print(f"使用臂: {chosen}", file=sys.stderr)

    # ---- 位姿活性预检 ----
    #
    # **这一步能在 2 秒内发现「位姿是冻的」**, 而不用摆 30 个姿态才发现。
    #
    # 怎么测: 连着取两次位姿, 中间等一会儿。如果两次**完全一样**, 那
    # TCP 读数根本没在跟着动 —— 可能是控制器断了 (服务在返回缓存),
    # 或者控制器是仿真模式 (cartesian 是固定预设值)。
    print("检查位姿是否跟随机械臂变化...", file=sys.stderr)
    live = _check_pose_liveness(client, chosen)
    if live is not None:
        bar = "=" * 62
        print(
            f"""
{bar}
  **机械臂位姿不跟随变化 —— 不能标定**
{bar}

  {live}

  这说明控制器给的 TCP 位姿是**固定值**。两种可能:

    1. 控制器已断开, arm_service 在返回缓存的旧快照
       -> 看 logs/arm.log 有没有「读取失败 / 已断开」
       -> 确认 configs/arm.yaml 的 controller_ws 地址对

    2. 控制器是**仿真模式**, cartesian 不随关节角变化
       -> 手眼标定必须用**真机**, 仿真下根本跑不通

  确认方式:
    curl -s 'http://127.0.0.1:8802/v1/kinematics?refresh=true' | python3 -m json.tool
  手动动一下手臂, 看 cartesian 的 x/y/z 变不变。
{bar}
""",
            file=sys.stderr,
        )
        cam.close()
        return [], [], []

    samples: List[PoseSample] = []
    images: List[Optional[np.ndarray]] = []
    rejected: List[Dict[str, Any]] = []
    #: 采集中途发现位姿不可信 -> 置 True, 采完直接作废
    stale_error: Optional[Exception] = None
    try:
        for i in range(count):
            print(
                f"\n[{i + 1}/{count}] 把机械臂摆到新位姿 (姿态要多样), "
                f"按回车采样, 输入 q 退出",
                file=sys.stderr,
            )
            try:
                key = input("> ")
            except EOFError:
                # stdin 没了 —— 大概率是 `conda run` 切断的。
                # 别静默退出 (看起来像「相机/机械臂有问题」), 要说清楚。
                env_python = conda_env_python("yolo") or "<conda 环境>/bin/python"
                print(
                    "\n  **stdin 提前结束 (EOF)** —— 采样中断。\n"
                    "  如果你还没采够, 这是因为用了 `conda run` (它会切断 stdin)。\n"
                    "  改用: conda run --no-capture-output -n yolo python ...\n"
                    f"  或:   {env_python} ...\n",
                    file=sys.stderr,
                )
                break
            if key.strip().lower() in ("q", "quit", "exit"):
                break

            print("  等机械臂停稳...", file=sys.stderr)
            try:
                tool_pose = _wait_stable(client, chosen)
            except StalePoseError as exc:
                # 位姿读数断了 —— 立刻中止, 别再收废样本
                bar = "=" * 62
                print(
                    f"""
{bar}
  **采样中止: 机械臂位姿读数不可信**
{bar}

  {exc}

  已采到的样本**不会**用于解算 —— 位姿是假的。
  修好连接后重新采集。
{bar}
""",
                    file=sys.stderr,
                )
                stale_error = exc
                break
            if tool_pose is None:
                print("  没等到停稳, 跳过", file=sys.stderr)
                rejected.append({"attempt": i, "reason": "机械臂没停稳"})
                continue

            sample = capture_sample(
                capture,
                spec,
                tool_pose,
                camera_matrix=camera_matrix,
                dist_coeffs=dist_coeffs,
                label=chosen,
            )
            if sample is None:
                rejected.append({"attempt": i, "reason": "角点检不全 / solvePnP 失败"})
                continue
            sample.t_tool_board = tool_offset
            samples.append(sample)
            images.append(getattr(sample, "image", None))

            # ---- **实时体检**: 每采一帧就查一次装夹/坐标/尺度 ----
            #
            # 这些判据**不需要外参**, 所以采集现场就能用。
            # 目的是: 摆位姿的过程中就把问题暴露出来, 而不是采完 20 个
            # 姿态才发现数据废了 (半小时白费)。
            _print_live_diagnostic(samples)

    finally:
        cam.close()

    if stale_error is not None:
        # 位姿不可信 -> 已采的样本全部作废, 明确告诉调用方
        return [], [], rejected

    return samples, images, rejected


def _print_live_diagnostic(samples: List[PoseSample]) -> None:
    """采集中途的实时体检 —— 有问题立刻提示。

    检查三件事 (都不需要外参):

    1. **旋转激励度** —— 绕各轴转得够不够。纯平移的采集对旋转没约束,
       外参会「解得出来但是错的」。
    2. **相对转动** —— 板相对工具有没有在滑动/摆动。
    3. **位移尺度比** —— 板位移/工具位移, 应该等于 1。
    """
    if len(samples) < 3:
        return

    from calibration.diagnostics import report

    tool_poses = [s.t_world_tool for s in samples]
    board_poses = [s.t_cam_board for s in samples]
    problems = report(tool_poses, board_poses)
    if not problems:
        return

    # 只在问题「变严重」时提示, 免得刷屏
    bar = "-" * 62
    print(f"\n{bar}", file=sys.stderr)
    print("  **采集体检发现问题** (摆位姿的过程中就改, 别等采完)", file=sys.stderr)
    print(f"{bar}", file=sys.stderr)
    for p in problems:
        print(f"  ! {p}", file=sys.stderr)
    print(f"{bar}\n", file=sys.stderr)


def _check_pose_liveness(client: ArmKinematicsClient, arm: str) -> Optional[str]:
    """检查 TCP 位姿是不是「活的」。

    连取两次位姿 (中间等 0.6s)。如果两次**完全一样** (位置差 < 0.1 mm
    且姿态差 < 1e-6), 说明读数没跟着动 —— 返回一段说明文字;
    正常则返回 None。

    **为什么要单独测这个**: 采样时如果位姿是冻的, 25 帧的
    ``T_world_tool`` 会完全相同, 解算时被当成野值剔到只剩 1 个,
    排查起来极其痛苦 (上次就踩了这个坑)。这里**在开始摆位姿之前**
    就能发现, 节省 10 分钟。

    **注意**: 如果用户还没开始动臂, 两次读数当然一样 —— 所以这个检查
    是在**用户已经摆好第一个位姿**之后才有意义。这里做的是「弱检查」:
    只判断「控制器通不通、数据新不新」, 真正的「有没有跟着动」由
    采样时的 :meth:`ArmKinematicsClient.require_fresh` 兜底。
    """
    import time as _time

    try:
        snap1 = client.snapshot(refresh=True)
        if not snap1:
            return "拿不到运动学快照 (arm_service 没响应)"
        if client.is_stale(snap1):
            notes = "; ".join(snap1.get("notes") or [])
            return f"快照是陈旧的{': ' + notes if notes else ''}"

        pose1 = client.tool_pose(snap1, arm)
        if pose1 is None:
            return f"拿不到 {arm} 的 TCP 位姿 (没有 cartesian 字段)"

        _time.sleep(0.6)

        snap2 = client.snapshot(refresh=True)
        if not snap2:
            return "第二次取位姿失败"
        if client.is_stale(snap2):
            return "第二次取位姿时快照已经陈旧 (控制器可能刚断)"

        pose2 = client.tool_pose(snap2, arm)
        if pose2 is None:
            return f"第二次拿不到 {arm} 的 TCP 位姿"

        moved = float(np.linalg.norm(pose1[:3, 3] - pose2[:3, 3]))
        rotated = float(np.abs(pose1[:3, :3] - pose2[:3, :3]).max())
        age = int(snap2.get("age_ms") or 0)

        if moved < 1e-4 and rotated < 1e-6:
            return (
                f"连续两次取到的 TCP 位姿完全一样 (位置差 {moved * 1e6:.1f} µm, "
                f"快照年龄 {age} ms) —— 读数是冻结的"
            )
        return None
    except Exception as exc:
        return f"检查位姿活性时出错: {type(exc).__name__}: {exc}"


def _wait_stable(client: ArmKinematicsClient, arm: str):
    from calibration.capture import tool_pose_stable

    return tool_pose_stable(client, arm, tolerance_m=0.002, max_wait_s=15.0)


# ------------------------------------------------------------ 离线重解


def _replace_offset(s: PoseSample, tb: np.ndarray) -> PoseSample:
    """复制一个样本, 换掉工具偏置。"""
    return PoseSample(
        t_cam_board=s.t_cam_board,
        t_world_tool=s.t_world_tool,
        t_tool_board=tb,
        obj_points=s.obj_points,
        img_points=s.img_points,
        corner_count=s.corner_count,
        label=s.label,
        image_path=s.image_path,
        board_distance_m=s.board_distance_m,
    )


def resolve_offline(
    session: SessionData,
    redetect: bool = False,
    tool_offset: Optional[np.ndarray] = None,
    exclude: Optional[Sequence[int]] = None,
    min_corners: int = 8,
) -> List[PoseSample]:
    """从 session 重建样本列表 (不动机械臂、不再拍照)。

    - ``redetect=True``: 从保存的图片**重新检测角点**, 重新解 ``solvePnP``
    - ``tool_offset``: 覆盖采样时用的工具偏置 (发现填反了就改这个)
    - ``exclude``: 要剔除的样本索引
    """
    exclude_set = set(int(i) for i in (exclude or []))

    # 板参数: session 里记的优先, 否则用当前配置
    spec = session.board or load_spec()

    # 内参: session 里记的优先, 否则读 configs/intrinsics.json
    camera_matrix = session.camera_matrix
    dist_coeffs = session.dist_coeffs
    if camera_matrix is None:
        intr = load_intrinsics()
        if intr:
            try:
                camera_matrix = np.array(intr["camera_matrix"], dtype=np.float64)
                dist_coeffs = np.array(intr["dist_coeffs"], dtype=np.float64)
            except Exception:
                pass

    out: List[PoseSample] = []
    for i, s in enumerate(session.samples):
        if i in exclude_set:
            print(f"  剔除样本 {i}", file=sys.stderr)
            continue

        tb = tool_offset if tool_offset is not None else s.t_tool_board

        if redetect:
            if not s.image_path:
                print(f"  样本 {i} 没有图片, 跳过重检测", file=sys.stderr)
                out.append(_replace_offset(s, tb))
                continue
            path = session.resolve_image(s.image_path)
            res = redetect_from_image(
                path, spec, camera_matrix, dist_coeffs, min_corners=min_corners
            )
            if res is None:
                print(f"  样本 {i} 重检测失败, 剔除", file=sys.stderr)
                continue
            img_pts, obj_pts, t_cam = res
            out.append(
                PoseSample(
                    t_cam_board=t_cam,
                    t_world_tool=s.t_world_tool,
                    t_tool_board=tb,
                    obj_points=obj_pts,
                    img_points=img_pts,
                    corner_count=len(img_pts),
                    label=s.label,
                    image_path=s.image_path,
                    board_distance_m=float(abs(t_cam[2, 3])),
                )
            )
        else:
            out.append(_replace_offset(s, tb))

    return out


# ------------------------------------------------------------ 报告


def report(result: Any) -> None:
    """打印标定质量报告。"""
    print()
    print("=" * 62)
    print("手眼标定结果")
    print("=" * 62)
    print(f"参与解算的采样: {result.sample_count}")
    print(
        f"位置残差: 平均 {result.mean_position_error * 1000:.1f} mm, "
        f"最大 {result.max_position_error * 1000:.1f} mm"
    )
    print(
        f"位置 RMS: {result.rms_position * 1000:.2f} mm "
        f"(精化前 {result.rms_position_before_refine * 1000:.2f} mm)"
    )
    print(f"姿态残差: 平均 {math.degrees(result.mean_rotation_error):.3f}°")
    if result.rejected:
        print(f"剔除的野值采样: {result.rejected}")
    print()
    print("外参矩阵 (相机系 -> 目标系):")
    for row in result.matrix:
        print("   [" + ", ".join(f"{v: .6f}" for v in row) + "]")

    print()
    print("验收标准 (1.5 m 工作距离):")
    pos_ok = result.rms_position < 0.010
    rot_ok = result.mean_rotation_error < math.radians(1.0)
    print(
        f"  [{'PASS' if pos_ok else 'FAIL'}] 位置 RMS < 10 mm "
        f"(实测 {result.rms_position * 1000:.2f} mm)"
    )
    print(
        f"  [{'PASS' if rot_ok else 'FAIL'}] 姿态残差 < 1.0° "
        f"(实测 {math.degrees(result.mean_rotation_error):.3f}°)"
    )
    print()
    print("下一步:")
    print("  1. 跑 tools/calib_verify.py 做**左右手性检查** (最常见的翻车点)")
    print("  2. 把矩阵写进 configs/vision.yaml 的 extrinsic.matrix")
    print("  3. 把 extrinsic.enabled 设为 true, flip_axes 设为 false")
    print("=" * 62)


def _try_joint_solve(
    samples: List[PoseSample],
    camera_matrix: np.ndarray,
    dist_coeffs: Optional[np.ndarray],
):
    """联合解算 + 留一法稳定性自检。

    返回 ``(T_world_cam, T_tool_board, rms_px, loo_stable)`` 或 None。
    """
    from calibration.handeye import solve_joint

    joint = solve_joint(
        samples, camera_matrix, dist_coeffs,
        initial_tool_board=None,
    )
    if joint is None:
        return None

    cam, tb, rms = joint
    bar = "=" * 62
    print(
        f"""
{bar}
  联合解算结果
{bar}

  重投影残差 RMS: {rms:.3f} px   (正常 < 2 px)

  T_world_cam  (相机在世界系):
    平移 = {np.round(cam[:3, 3], 4)}

  T_tool_board (板相对工具):
    平移 = {np.round(tb[:3, 3], 4)}
    (板原点离工具原点 {np.linalg.norm(tb[:3, 3]) * 1000:.1f} mm)
{bar}
""",
        file=sys.stderr,
    )

    # ---- 留一法自检: 每次留 3 帧, 看解稳不稳定 ----
    #
    # **两个量一起解有 6 个自由度是耦合的** —— 采样姿态不够多样时,
    # 可能解得「偏」但残差看起来还行。所以必须验证稳定性。
    print("留一法稳定性自检 (每次留 3 帧)...", file=sys.stderr)
    n = len(samples)
    group = max(3, n // 5)
    xs, cs, rms_list = [], [], []
    for start in range(0, n, group):
        hold = list(range(start, min(start + group, n)))
        train = [s for i, s in enumerate(samples) if i not in hold]
        if len(train) < 3:
            continue
        sub = solve_joint(train, camera_matrix, dist_coeffs,
                          initial_tool_board=None)
        if sub is None:
            continue
        xs.append(sub[1][:3, 3])
        cs.append(sub[0][:3, 3])
        rms_list.append(sub[2])

    stable = False
    if len(xs) >= 2:
        xs_arr = np.array(xs)
        cs_arr = np.array(cs)
        x_std = float(np.max(xs_arr.std(axis=0))) * 1000
        c_std = float(np.max(cs_arr.std(axis=0))) * 1000
        stable = x_std < 30.0 and c_std < 50.0
        print(
            f"  {len(xs)} 组留一解: T_tool_board 平移标准差 最大 {x_std:.1f} mm, "
            f"T_world_cam 平移标准差 最大 {c_std:.1f} mm",
            file=sys.stderr,
        )
        if not stable:
            print(
                "  **不稳定** —— 说明这批数据里有坏帧, 或者采样姿态不够多样。\n"
                "  建议: 用 tools/calib_view.py 看图挑掉坏帧, 然后用 --exclude 重解。",
                file=sys.stderr,
            )
    else:
        print("  组数太少, 没法做留一法自检", file=sys.stderr)

    return cam, tb, rms, stable


def _write_joint_result(joint, args, sample_count: int) -> int:
    """把联合解算结果写盘 (外参 + 工具偏置)。"""
    cam, tb, rms, stable = joint

    out = Path(args.out) if args.out else config_path("handeye.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "matrix": [[round(float(v), 8) for v in row] for row in cam],
        "sample_count": sample_count,
        "rms_reprojection_px": round(float(rms), 4),
        "solver": "joint (T_tool_board + T_world_cam 同时解)",
        "tool_board_matrix": [[round(float(v), 8) for v in row] for row in tb],
        "stable": bool(stable),
    }
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"\n结果已保存到 {out}", file=sys.stderr)

    # 工具偏置也存一份 —— 下次采集可以直接用
    off_path = out.parent / "tool_board.json"
    with open(off_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"matrix": [[round(float(v), 8) for v in row] for row in tb]},
            fh, ensure_ascii=False, indent=2,
        )
    print(f"工具偏置已保存到 {off_path}", file=sys.stderr)
    print(
        f"\n下次采集建议直接用:\n"
        f"  --tool-offset {off_path}",
        file=sys.stderr,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="手眼标定")
    parser.add_argument("--capture", action="store_true", help="交互式采样并解算")
    parser.add_argument("--samples", type=str, default="", help="从已采样本解算")
    parser.add_argument("--count", type=int, default=20, help="采样数 (默认 20)")
    parser.add_argument(
        "--arm", default="",
        help="用哪条臂: left / right / 控制器里的机器人名。留空 = 自动选",
    )
    parser.add_argument(
        "--arm-url", default="http://127.0.0.1:8802", help="arm_service 地址",
    )
    parser.add_argument(
        "--tool-offset", type=str, default="",
        help="板相对工具的 4x4 偏置 JSON。文件不存在会直接报错 "
             "(不再静默用单位阵) —— 详见 docs/calibration.md 的「坑 12」",
    )
    parser.add_argument(
        "--samples-out", default="/tmp/handeye_samples.json", help="采样保存路径",
    )
    parser.add_argument(
        "--out", default="", help="标定结果 JSON (默认 configs/handeye.json)",
    )
    parser.add_argument(
        "--no-images", action="store_true",
        help="不保存原始彩色图 (只存角点, 体积小但不能重新检测)",
    )
    parser.add_argument(
        "--redetect", action="store_true",
        help="离线重解时从保存的图片重新检测角点",
    )
    parser.add_argument(
        "--exclude", type=str, default="",
        help="离线重解时剔除的样本索引, 如 3,7,12",
    )
    parser.add_argument(
        "--min-corners", type=int, default=8,
        help="角点数的下限 (默认 8)",
    )
    args = parser.parse_args()

    session: Optional[SessionData] = None
    samples: List[PoseSample] = []
    images: List[Optional[np.ndarray]] = []

    if args.capture:
        # **先检查 stdin 能不能交互** —— 不然会白开一次相机,
        # 然后因为 input() 立刻 EOF 而「一个样本都没采到」。
        if not _check_stdin_interactive():
            return 2

        try:
            tool_offset = load_tool_offset(args.tool_offset or None)
        except (FileNotFoundError, ValueError) as exc:
            print(f"\n错误: {exc}", file=sys.stderr)
            return 2
        spec = load_spec()
        samples, images, rejected = capture_samples(
            args.count,
            args.arm,
            args.arm_url,
            tool_offset,
            None,
            spec=spec,
        )
        if not samples:
            print("没采到任何样本", file=sys.stderr)
            return 1

        intr = load_intrinsics()
        cam_m = dist_c = None
        if intr:
            try:
                cam_m = np.array(intr["camera_matrix"], dtype=np.float64)
                dist_c = np.array(intr["dist_coeffs"], dtype=np.float64)
            except Exception:
                pass

        session = SessionData(
            samples=samples,
            board=spec,
            camera_matrix=cam_m,
            dist_coeffs=dist_c,
            tool_offset=tool_offset,
            arm=samples[0].label if samples else "",
            rejected=rejected,
        )
        out_path = Path(args.samples_out)
        save_session(
            session,
            out_path,
            images=None if args.no_images else images,
        )
        n_img = sum(1 for s in session.samples if s.image_path)
        print(
            f"采样已存到 {out_path} ({len(session.samples)} 个样本, "
            f"{n_img} 张图, {len(rejected)} 次跳过)"
        )
        samples = session.samples

    elif args.samples:
        p = Path(args.samples)
        if not p.exists():
            print(f"找不到 {p}", file=sys.stderr)
            return 1
        session = load_session(p)
        print(f"从 {p} 读到 {len(session.samples)} 个采样")
        if session.board:
            print(f"  板参数: square={session.board.square_length}")
        if session.has_images():
            print("  含原始图 —— 可以用 --redetect 重新检测角点")
        else:
            print("  没有原始图 —— 只能用已存下的角点")

        # 工具偏置覆盖: 命令行给了就优先
        try:
            tool_offset_override = (
                load_tool_offset(args.tool_offset) if args.tool_offset else None
            )
        except (FileNotFoundError, ValueError) as exc:
            # 偏置文件有问题 -> 明确报错, 别让 traceback 淹没信息
            print(f"\n错误: {exc}", file=sys.stderr)
            return 2
        samples = resolve_offline(
            session,
            redetect=args.redetect,
            tool_offset=tool_offset_override,
            exclude=_parse_indices(args.exclude),
            min_corners=args.min_corners,
        )
        if args.redetect:
            print(f"重检测后剩 {len(samples)} 个可用样本")
    else:
        print("要么用 --capture 采样, 要么用 --samples 读已有采样", file=sys.stderr)
        return 1

    if len(samples) < 3:
        print(f"只有 {len(samples)} 个采样, 至少要 3 个", file=sys.stderr)
        return 1

    print(f"\n解算外参 ({len(samples)} 个采样)...")

    # 精化要用**固定**的内参 —— 先单独标内参, 再拿它解外参。
    # 边解边估会让内外参互相补偿, 外参就不准了。
    camera_matrix = None
    dist_coeffs = None
    if session is not None and session.camera_matrix is not None:
        camera_matrix = session.camera_matrix
        dist_coeffs = session.dist_coeffs
        print(
            f"用 session 里记的内参做精化: fx={camera_matrix[0, 0]:.2f} "
            f"fy={camera_matrix[1, 1]:.2f}",
            file=sys.stderr,
        )
    else:
        intr = load_intrinsics()
        if intr:
            try:
                camera_matrix = np.array(intr["camera_matrix"], dtype=np.float64)
                dist_coeffs = np.array(intr["dist_coeffs"], dtype=np.float64)
                print(
                    f"用标定内参做精化: fx={camera_matrix[0, 0]:.2f} "
                    f"fy={camera_matrix[1, 1]:.2f}",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(f"读标定内参失败 ({exc}), 只做稳健平均", file=sys.stderr)

    # 没给 --tool-offset = 板位置未知 -> 直接联合求解, 不走「板在工具原点」的常规解算
    force_joint = not args.tool_offset and camera_matrix is not None
    if force_joint:
        print("未指定 --tool-offset: 板位置视为未知, 直接联合解算", file=sys.stderr)
        result = None
    else:
        result = solve_handeye(
            samples,
            refine=camera_matrix is not None,
            camera_matrix=camera_matrix,
            dist_coeffs=dist_coeffs,
        )

    if result is None:
        # ---- 常规解算失败 -> 尝试联合解算 ----
        #
        # 常规解算失败最常见的原因: **工具偏置没填对**。
        # 「板原点 = 工具原点」这个假设不成立时, 各帧解出的 T_world_cam
        # 会互相打架, 全被当成野值剔掉 (「剔野值后只剩 0 个」)。
        #
        # 联合解算把 T_tool_board 和 T_world_cam 一起解出来, 数据自己
        # 会告诉我们板到底装在哪。
        if camera_matrix is None:
            print(
                "解算失败 —— 检查采样数量和多样性。\n"
                "(没有内参, 无法做联合解算)",
                file=sys.stderr,
            )
            return 1

        print(
            "\n"
            "联合解算 (同时求 T_tool_board 和 T_world_cam)\n",
            file=sys.stderr,
        )
        joint = _try_joint_solve(samples, camera_matrix, dist_coeffs)
        if joint is None:
            print(
                "解算失败 —— 检查采样数量和多样性\n"
                "(常规解算和联合解算都失败; 多半是采样姿态不够多样, "
                "或者板/相机的对应关系有问题)",
                file=sys.stderr,
            )
            return 1
        return _write_joint_result(joint, args, len(samples))

    report(result)

    out = Path(args.out) if args.out else config_path("handeye.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, ensure_ascii=False, indent=2)
    print(f"\n结果已保存到 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
