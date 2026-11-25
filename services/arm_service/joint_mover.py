"""关节流移动: 客户端自己做逆解, 控制器只吃关节角。

**为什么不用控制器的逆解接口** (``cartesianIkPath: analytic``):

控制器的末端逆解有自己的参考坐标系约定 (API 说明 28965):

- ``HM5-54-*`` / ``DA5-54-*``: 位姿在**躯干系**, 末端是该机型数值模型的
  第 7 关节坐标系 —— 与 ``robot/status.cartesian`` (基座系 + 工具偏置)
  **不是同一个系**;
- ``NDA-06-*``: 位姿在**该臂基座系**, 末端是法兰。

搞错的话, ``flange = target_base · tool⁻¹`` 算出来的「工具偏置」是垃圾
(现场实测 1.01m / 120°), 目标位姿整个算错, 逆解必然失败。

**本模块的做法** (与 ``Telep`` 遥操方案一致):

1. 入控用 ``cartesianIkPath: "none"`` —— 控制器**全程不做末端逆解**,
   只接受关节包 (``controlSpace: 0``);
2. 用 pinocchio (URDF) 在本地做 DLS 逆解, 得到目标关节角;
3. 通过关节流下发关节角, 自己按关节空间插值。

坐标系口径: 一切 IK/FK 都在**该臂基座系**里做, 世界系 <-> 基座系靠
控制器的 ``baseFrame`` 换算。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from robot.controller import Phase
from robot.geometry import Pose, distance, euler_to_quat, quat_angle
from robot.streamer import JointStreamer

logger = logging.getLogger("arm.jointmover")

#: 到位判据: TCP 与目标的距离 (米)
ARRIVE_TOL_M = 0.02
#: 运动时长合法范围 (秒)
MIN_DURATION = 0.5
MAX_DURATION = 30.0
#: 入控响应等待上限 (秒)
START_TIMEOUT = 8.0
#: 轨迹发完后再多保持多久 (秒), 让机器人收敛到终点
SETTLE_S = 0.4
#: 单帧关节增量上限 (deg) —— 防跳解支
MAX_STEP_DEG = 3.0
#: 逆解容差
IK_TOL_POS_M = 1e-4
IK_TOL_ROT_RAD = 1e-3


class JointSpaceMover:
    """关节流移动执行器。依附于 ArmService。

    与 :class:`CartesianMover` 的区别: **不用控制器的末端逆解**,
    自己在本地解出关节角再下发。
    """

    def __init__(self, service: Any) -> None:
        self._svc = service
        self._lock = threading.Lock()
        self._abort = threading.Event()
        self._speak_enabled = True
        self._start_event = threading.Event()
        self._start_result: Dict[str, Any] = {}

    # ------------------------------------------------------------ 外部入口

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def abort(self) -> None:
        self._abort.set()

    def on_start_control(self, db: Any, response: Dict[str, Any]) -> None:
        """ArmService 收到 realtime/startControl 应答时转发过来。"""
        err = response.get("err")
        result: Dict[str, Any] = {
            "ok": not err,
            "db": db if isinstance(db, dict) else {},
        }
        if err:
            details = []
            if isinstance(db, dict):
                for item in db.get("results") or []:
                    if isinstance(item, dict) and item.get("err"):
                        e = item["err"]
                        details.append(f"{e.get('code', '?')}:{e.get('message', '')}")
            result["code"] = (err or {}).get("code", "?")
            result["message"] = " | ".join(
                [str((err or {}).get("message", ""))] + details
            )
        self._start_result = result
        self._start_event.set()

    # ------------------------------------------------------------ 主流程

    def move_to(
        self,
        arm: str,
        x: float,
        y: float,
        z: float,
        a: Optional[float] = None,
        b: Optional[float] = None,
        c: Optional[float] = None,
        duration: Optional[float] = None,
        speak: bool = True,
    ) -> Dict[str, Any]:
        """把某条臂的 TCP 移到世界系下的目标位姿。**阻塞到走完**。

        a/b/c 不给就用配置里示教的抓取姿态; 两者都没给就**保持当前
        TCP 姿态**, 只挪位置。

        **控制指令总闸** 关着时: 只算不动 (``ok=false, code="disabled"``),
        但仍会返回算好的目标位姿。
        """
        if not self._svc.control_enabled:
            logger.warning("[关节流移动] 控制指令总闸已关闭: 只算位置, 不下发指令")
            return self._reject(
                "disabled",
                "机械臂控制已关闭，只计算了位置，机械臂没有动",
                stage="gate", speak=False,
                extra={
                    "target_world": {"x": float(x), "y": float(y), "z": float(z)},
                    "planned_only": True,
                },
            )

        grasp = self._svc.arm_cfg.grasp
        dur = float(duration) if duration else float(grasp.duration)
        if not (MIN_DURATION <= dur <= MAX_DURATION):
            return self._reject("bad_param", f"运动时长 {dur} 秒不合法", speak=False)
        if not all(math.isfinite(float(v)) for v in (x, y, z)):
            return self._reject("bad_param", "目标坐标不是有限数", speak=False)

        if not self._lock.acquire(blocking=False):
            return self._reject("busy", "机械臂正在执行上一个移动", speak=True)
        try:
            self._abort.clear()
            self._speak_enabled = bool(speak)
            return self._run(arm, float(x), float(y), float(z), a, b, c, dur)
        finally:
            self._lock.release()

    # ------------------------------------------------------------ 执行

    def move_joints(
        self,
        arm: str,
        joints: Sequence[float],
        duration: Optional[float] = None,
        speak: bool = True,
    ) -> Dict[str, Any]:
        """把某条臂**直接**移到给定的关节角。**阻塞到走完**。

        与 :meth:`move_to` 的区别: 这里**不走逆解** —— 目标关节角是
        调用方直接给的 (``arm`` + ``pose`` 片段)。

        **控制指令总闸** 关着时: 只算不动 (``ok=false, code="disabled"``),
        但仍会返回算好的目标关节角。
        """
        if not self._svc.control_enabled:
            logger.warning("[关节流移动] 控制指令总闸已关闭: 只算位置, 不下发指令")
            return self._reject(
                "disabled",
                "机械臂控制已关闭，只计算了位置，机械臂没有动",
                stage="gate", speak=False,
                extra={
                    "target_joints": [float(v) for v in joints],
                    "planned_only": True,
                },
            )

        values = [float(v) for v in (joints or [])]
        if len(values) != 7:
            return self._reject(
                "bad_param", f"关节角必须正好 7 个 (给了 {len(values)} 个)",
                speak=False,
            )
        if not all(math.isfinite(v) for v in values):
            return self._reject("bad_param", "关节角不是有限数", speak=False)

        grasp = self._svc.arm_cfg.grasp
        dur = float(duration) if duration else float(grasp.duration)
        if not (MIN_DURATION <= dur <= MAX_DURATION):
            return self._reject("bad_param", f"运动时长 {dur} 秒不合法", speak=False)

        if not self._lock.acquire(blocking=False):
            return self._reject("busy", "机械臂正在执行上一个移动", speak=True)
        try:
            self._abort.clear()
            self._speak_enabled = bool(speak)
            return self._run_joints(arm, values, dur)
        finally:
            self._lock.release()

    def _run_joints(
        self,
        arm: str,
        q_target: List[float],
        duration: float,
    ) -> Dict[str, Any]:
        """按给定的关节角走一遍 (不做逆解)。"""
        svc = self._svc
        ctl = svc._controller
        conn = svc._conn

        t = svc._play_thread
        if t is not None and t.is_alive():
            return self._reject("busy", "机械臂正在播放动作，请先停止", speak=True)

        # ---- 1. 安全门阀 ----
        report = svc.check_safety()
        if not report.ok:
            logger.warning("[关节流移动] 门阀拒绝: %s", report.reason)
            if self._speak_enabled:
                svc._speak_reject(report)
            return self._reject(
                report.code or "safety", report.reason, stage="safety", speak=False
            )

        # ---- 2. 选臂 ----
        name, side = self._resolve_arm(arm)
        if not name:
            return self._reject("no_arm", f"找不到机械臂 {arm!r}", speak=True)

        # ---- 3. 运动学快照 (要实测角当起点) ----
        snap = svc.refresh_kinematics()
        if snap.get("stale") or not snap.get("ok"):
            return self._reject(
                "stale", "拿不到最新的机械臂位姿，不能移动", stage="plan", speak=True
            )
        data = (snap.get("arms") or {}).get(name) or {}
        joints_now = data.get("joints")
        if not joints_now or len(joints_now) < 7:
            return self._reject(
                "no_joints", f"{name} 没有实测关节角", stage="plan", speak=True
            )

        q_start = [float(v) for v in joints_now[:7]]
        info: Dict[str, Any] = {
            "arm": name,
            "side": side,
            "duration": duration,
            "joints_before": [round(v, 5) for v in q_start],
            "joints_target": [round(v, 5) for v in q_target],
            #: 目标关节角是直接给的, 没有逆解环节
            "ik_mode": "explicit_joints",
        }
        logger.info(
            "[关节流移动] %s 直接移到关节角 %s (时长 %.1fs)",
            name, [round(v, 4) for v in q_target], duration,
        )

        # ---- 4. 确保使能 ----
        if name in ctl.disabled_arms():
            ctl.enable_arms()
            deadline = time.time() + 3.0
            while time.time() < deadline and name in ctl.disabled_arms():
                time.sleep(0.05)
            if name in ctl.disabled_arms():
                return self._reject(
                    "enable_failed", "机械臂上使能失败", stage="plan",
                    speak=True, extra=info,
                )

        # ---- 5. 入控 (关节流, 不带末端逆解) ----
        conn.get_realtime_status()
        time.sleep(0.3)

        entered = False
        streamer: Optional[JointStreamer] = None
        try:
            ctl.clear_failure()
            ctl._set_phase(Phase.ENTERING, "正在进入关节控制")
            self._start_event.clear()
            self._start_result = {}

            streamer = JointStreamer(
                port=ctl.streamer_port,
                frequency=ctl.streamer_frequency,
            )
            streamer.set_host(conn.host or "127.0.0.1")
            streamer.set_target(name, q_start)
            streamer.start()

            cfg = ctl.joint_control_config()
            cmd_id = conn.start_control(cfg, [name])
            if cmd_id < 0:
                return self._reject(
                    "send_failed", "发送入控命令失败", stage="enter",
                    speak=True, extra=info,
                )
            entered = True

            if not self._start_event.wait(START_TIMEOUT):
                return self._reject(
                    "start_timeout", "机械臂入控超时", stage="enter",
                    speak=True, extra=info,
                )
            res = self._start_result
            if not res.get("ok"):
                from mover import translate_control_error
                text = translate_control_error(
                    res.get("code"), res.get("message", "")
                )
                logger.warning("[关节流移动] 入控失败 (%s): %s",
                               res.get("code"), res.get("message"))
                info["controller_error"] = {
                    "code": res.get("code"), "message": res.get("message"),
                }
                return self._reject(
                    "enter_failed", text, stage="enter", speak=True, extra=info,
                )

            # ---- 6. 关节空间插值下发 ----
            ctl._set_phase(Phase.PLAYING, "正在移动到目标关节角")
            abort_msg = self._stream_joint_trajectory(
                streamer, name, q_start, q_target, duration, info
            )
            if abort_msg:
                return self._reject(
                    info.get("abort_code", "aborted"), abort_msg,
                    stage="move", speak=True, extra=info,
                )
        finally:
            if streamer is not None:
                try:
                    streamer.stop()
                except Exception as exc:  # pragma: no cover
                    logger.warning("[关节流移动] 停关节流失败: %s", exc)
            if entered:
                conn.stop_control([name])
                logger.info("[关节流移动] 已发 realtime/stopControl")
            ctl._set_phase(Phase.IDLE)

        # ---- 7. 读回实测角核对 ----
        time.sleep(SETTLE_S)
        after = svc.refresh_kinematics()
        actual = ((after.get("arms") or {}).get(name) or {}).get("joints")
        if actual and len(actual) >= 7:
            err = max(abs(a - b) for a, b in zip(actual[:7], q_target))
            info["joints_after"] = [round(float(v), 5) for v in actual[:7]]
            info["joint_error_deg"] = round(math.degrees(err), 3)
            if math.degrees(err) > 2.0:
                message = f"没有完全到位，关节角还差 {math.degrees(err):.1f} 度"
                code = "not_arrived"
                ok = False
            else:
                message = f"已到达目标关节角，误差 {math.degrees(err):.1f} 度"
                code = "ok"
                ok = True
        else:
            message = "已下发目标关节角，但读不回实测角"
            code = "no_feedback"
            ok = False

        logger.info("[关节流移动] %s: %s", "成功" if ok else "失败", message)
        if not ok and self._speak_enabled:
            svc._speak_text(message)
        return {"ok": ok, "code": code, "stage": "done", "message": message,
                **info}

    def _run(
        self,
        arm: str,
        x: float,
        y: float,
        z: float,
        a: Optional[float],
        b: Optional[float],
        c: Optional[float],
        duration: float,
    ) -> Dict[str, Any]:
        svc = self._svc
        grasp = svc.arm_cfg.grasp
        ctl = svc._controller
        conn = svc._conn

        t = svc._play_thread
        if t is not None and t.is_alive():
            return self._reject("busy", "机械臂正在播放动作，请先停止", speak=True)

        # ---- 1. 安全门阀 ----
        report = svc.check_safety()
        if not report.ok:
            logger.warning("[关节流移动] 门阀拒绝: %s", report.reason)
            if self._speak_enabled:
                svc._speak_reject(report)
            return self._reject(
                report.code or "safety", report.reason, stage="safety", speak=False
            )

        # ---- 2. 选臂 ----
        name, side = self._resolve_arm(arm)
        if not name:
            return self._reject("no_arm", f"找不到机械臂 {arm!r}", speak=True)

        # ---- 3. 运动学快照 ----
        snap = svc.refresh_kinematics()
        if snap.get("stale") or not snap.get("ok"):
            return self._reject(
                "stale", "拿不到最新的机械臂位姿，不能移动", stage="plan", speak=True
            )
        data = (snap.get("arms") or {}).get(name) or {}
        if "base_frame" not in data or "cartesian" not in data:
            return self._reject(
                "no_pose", f"{name} 还没有收到基座或末端位姿", stage="plan", speak=True
            )
        tcp_world = data["cartesian"]
        joints_now = data.get("joints")
        if not joints_now or len(joints_now) < 7:
            return self._reject(
                "no_joints", f"{name} 没有实测关节角", stage="plan", speak=True
            )

        # ---- 4. 目标姿态 ----
        taught = grasp.orientation.get(side) or {}
        if a is not None or b is not None or c is not None:
            oa = float(taught.get("a", 0.0) if a is None else a)
            ob = float(taught.get("b", 0.0) if b is None else b)
            oc = float(taught.get("c", 0.0) if c is None else c)
            keep_orientation = False
        elif taught:
            oa = float(taught["a"])
            ob = float(taught["b"])
            oc = float(taught["c"])
            keep_orientation = False
        else:
            oa = float(tcp_world["a"])
            ob = float(tcp_world["b"])
            oc = float(tcp_world["c"])
            keep_orientation = True
            logger.info(
                "[关节流移动] %s 没示教抓取姿态 —— 保持当前 TCP 姿态 "
                "(a=%.4f, b=%.4f, c=%.4f), 只挪位置",
                name, oa, ob, oc,
            )

        target_world: Pose = ((x, y, z), euler_to_quat(oa, ob, oc))
        reach = math.sqrt(
            sum(v * v for v in svc._target_in_base(name, (x, y, z)))
        )
        travel = distance(
            (tcp_world["x"], tcp_world["y"], tcp_world["z"]), (x, y, z)
        )
        info: Dict[str, Any] = {
            "arm": name,
            "side": side,
            "duration": duration,
            "target_world": {"x": x, "y": y, "z": z, "a": oa, "b": ob, "c": oc},
            "keep_orientation": keep_orientation,
            "reach_m": round(reach, 4),
            "max_reach_m": grasp.max_reach,
            "travel_m": round(travel, 4),
            "tcp_before": dict(tcp_world),
            #: 本次走的是本地逆解 + 关节流, 不经过控制器末端逆解
            "ik_mode": "local_pinocchio",
        }
        logger.info(
            "[关节流移动] %s 目标(世界系)=(%.4f, %.4f, %.4f) abc=(%.3f, %.3f, %.3f) "
            "距基座 %.3fm (上限 %.2fm) 行程 %.3fm 时长 %.1fs",
            name, x, y, z, oa, ob, oc, reach, grasp.max_reach, travel, duration,
        )
        if reach > grasp.max_reach:
            return self._reject(
                "out_of_reach",
                f"目标离机械臂基座 {reach:.2f} 米，超过可达范围 {grasp.max_reach:.2f} 米",
                stage="plan", speak=True, extra=info,
            )

        # ---- 5. 本地逆解 ----
        kin = svc.kinematics_for(name)
        if kin is None:
            return self._reject(
                "no_kinematics",
                "本地运动学不可用（缺少 pinocchio 或 URDF），不能做笛卡尔移动",
                stage="plan", speak=True, extra=info,
            )

        # 基座位姿要用**控制器实测的 baseFrame** —— URDF 自带装法与实测
        # 不是同一个数, 不同步 IK 目标整体偏。
        svc.sync_kinematics_base(name, data.get("base_frame") or {})

        ik = kin.ik_world_rpy_with_fallback(
            (x, y, z), (oa, ob, oc), joints_now,
            max_iters=120,
            tol_pos=IK_TOL_POS_M,
            tol_rot=IK_TOL_ROT_RAD,
            posture_gain=0.0,
        )
        info["ik"] = {
            "ok": ik.ok,
            "err_pos_mm": round(ik.err_pos * 1000, 3),
            "err_rot_deg": round(math.degrees(ik.err_rot), 3),
            "iters": ik.iters,
            "at_limit": ik.at_limit,
        }
        logger.info("[关节流移动] %s 本地逆解: %s", name, ik.summary())
        if not ik.ok:
            # 逆解失败时别只丢一句「够不着」—— 顺手扫一遍哪些姿态**是**
            # 够得着的, 直接告诉用户该改成什么。不然用户只能一次次试。
            advice = self._suggest_poses(kin, (x, y, z), (oa, ob, oc),
                                         joints_now)
            if advice:
                msg = (
                    f"目标点够不着（位置差 {ik.err_pos * 1000:.1f} 毫米，"
                    f"姿态差 {math.degrees(ik.err_rot):.1f} 度）。"
                    f"这个位置能到的姿态是: {advice} —— "
                    f"把 configs/arm.yaml 里的 grasp.{side} 改成其中之一再试。"
                )
            else:
                msg = (
                    f"目标点够不着（位置差 {ik.err_pos * 1000:.1f} 毫米，"
                    f"姿态差 {math.degrees(ik.err_rot):.1f} 度），"
                    f"而且这个位置附近找不到任何全姿态可解的解。"
                )
            return self._reject("ik_failed", msg, stage="plan", speak=True,
                                extra=info)

        # 姿态可能没完全对上 (奇异位形附近, 冗余自由度用掉了)。
        # 抓取只在乎位置, 姿态差一点照样能抓 —— 但要说清楚, 别默默吞掉。
        rot_dev = math.degrees(ik.err_rot)
        if rot_dev > 5.0:
            logger.warning(
                "[关节流移动] %s 位置已对准 (%0.1fmm), 但姿态差 %.1f 度 "
                "—— 当前位姿接近奇异 (手臂伸直), 姿态自由度丢失。"
                "抓取只按位置走, 姿态请用视觉结果复核。",
                name, ik.err_pos * 1000, rot_dev,
            )
            info["ik"]["rot_deviation_deg"] = round(rot_dev, 2)
            info["ik"]["position_only"] = True

        q_target = ik.q
        q_start = [float(v) for v in joints_now[:7]]
        info["joints_before"] = [round(v, 5) for v in q_start]
        info["joints_target"] = [round(v, 5) for v in q_target]

        # ---- 6. 确保使能 ----
        if name in ctl.disabled_arms():
            ctl.enable_arms()
            deadline = time.time() + 3.0
            while time.time() < deadline and name in ctl.disabled_arms():
                time.sleep(0.05)
            if name in ctl.disabled_arms():
                return self._reject(
                    "enable_failed", "机械臂上使能失败", stage="plan", speak=True, extra=info
                )

        # ---- 7. 入控 (关节流, 不带末端逆解) ----
        conn.get_realtime_status()
        time.sleep(0.3)

        entered = False
        streamer: Optional[JointStreamer] = None
        try:
            ctl.clear_failure()
            ctl._set_phase(Phase.ENTERING, "正在进入关节控制")
            self._start_event.clear()
            self._start_result = {}

            # 起关节流必须早于 startControl (首包等待窗只有 2000ms)
            #
            # **用自己独立的 streamer** —— controller 那个是给动作库播放
            # 用的, 它的生命周期由 enter_control / stop_control 管。这里
            # 自己起自己停, 不去抢控制器的。
            streamer = JointStreamer(
                port=ctl.streamer_port,
                frequency=ctl.streamer_frequency,
            )
            streamer.set_host(conn.host or "127.0.0.1")
            streamer.set_target(name, q_start)
            streamer.start()

            cfg = ctl.joint_control_config()
            cmd_id = conn.start_control(cfg, [name])
            if cmd_id < 0:
                return self._reject(
                    "send_failed", "发送入控命令失败", stage="enter", speak=True, extra=info
                )
            entered = True

            if not self._start_event.wait(START_TIMEOUT):
                return self._reject(
                    "start_timeout", "机械臂入控超时", stage="enter", speak=True, extra=info
                )
            res = self._start_result
            if not res.get("ok"):
                from mover import translate_control_error
                text = translate_control_error(res.get("code"), res.get("message", ""))
                logger.warning("[关节流移动] 入控失败 (%s): %s",
                               res.get("code"), res.get("message"))
                info["controller_error"] = {
                    "code": res.get("code"), "message": res.get("message"),
                }
                return self._reject("enter_failed", text, stage="enter", speak=True, extra=info)

            # ---- 8. 关节空间插值下发 ----
            ctl._set_phase(Phase.PLAYING, "正在移动到目标点")
            abort_msg = self._stream_joint_trajectory(
                streamer, name, q_start, q_target, duration, info
            )
            if abort_msg:
                return self._reject(
                    info.get("abort_code", "aborted"), abort_msg,
                    stage="move", speak=True, extra=info,
                )
        finally:
            if streamer is not None:
                try:
                    streamer.stop()
                except Exception as exc:  # pragma: no cover
                    logger.warning("[关节流移动] 停关节流失败: %s", exc)
            if entered:
                conn.stop_control([name])
                logger.info("[关节流移动] 已发 realtime/stopControl")
            ctl._set_phase(Phase.IDLE)

        # ---- 9. 读回 TCP 核对 ----
        time.sleep(SETTLE_S)
        after = svc.refresh_kinematics()
        tcp_after = ((after.get("arms") or {}).get(name) or {}).get("cartesian")
        err_m = float("inf")
        if tcp_after:
            info["tcp_after"] = dict(tcp_after)
            err_m = distance(
                (tcp_after["x"], tcp_after["y"], tcp_after["z"]), (x, y, z)
            )
            actual_q = euler_to_quat(tcp_after["a"], tcp_after["b"], tcp_after["c"])
            info["error_m"] = round(err_m, 4)
            info["error_rot_rad"] = round(quat_angle(actual_q, target_world[1]), 4)

        if err_m > ARRIVE_TOL_M:
            message = f"没有完全到位，离目标还差 {err_m * 100:.1f} 厘米"
            code = "not_arrived"
            ok = False
        elif rot_dev > 5.0:
            # 位置到了, 但姿态差很多 —— 这是「位置优先回退」的结果。
            # 单独报一个码, 别混进普通的 ok 里, 否则上层以为是抓到了。
            message = (
                f"位置到了（差 {err_m * 100:.1f} 厘米），但姿态差 "
                f"{rot_dev:.1f} 度 —— 目标姿态够不着，只能退而求其次对准位置。"
            )
            code = "position_only"
            ok = False
            advice = self._suggest_poses(kin, (x, y, z), (oa, ob, oc),
                                         joints_now)
            if advice:
                message += f" 这个位置能到的姿态是: {advice}"
        else:
            message = f"已到达目标点，误差 {err_m * 100:.1f} 厘米"
            code = "ok"
            ok = True

        logger.info("[关节流移动] %s: %s", "成功" if ok else "失败", message)
        if not ok and self._speak_enabled:
            svc._speak_text(message)
        return {"ok": ok, "code": code, "stage": "done", "message": message, **info}

    # ------------------------------------------------------------ 关节轨迹

    def _stream_joint_trajectory(
        self,
        streamer: Any,
        name: str,
        q_start: Sequence[float],
        q_target: Sequence[float],
        duration: float,
        info: Dict[str, Any],
    ) -> Optional[str]:
        """在关节空间插值, 逐点下发。返回错误信息 (None = 正常)。"""
        ctl = self._svc._controller
        freq = max(10, min(200, ctl.streamer_frequency))
        steps = max(1, int(duration * freq))
        q = list(q_start)
        for i in range(1, steps + 1):
            if self._abort.is_set():
                info["abort_code"] = "aborted"
                return "移动被打断"
            u = i / steps
            s = u * u * (3.0 - 2.0 * u)   # S 曲线, 起止速度为 0
            target = [
                q_start[j] + s * (q_target[j] - q_start[j]) for j in range(7)
            ]
            streamer.set_target(name, target)
            time.sleep(1.0 / freq)

        # 终点保持
        streamer.set_target(name, list(q_target))
        return None

    # ------------------------------------------------------------ 工具

    def _resolve_arm(self, arm: str) -> Tuple[str, str]:
        """``left`` / ``right`` / 控制器里的真实名 -> (真实名, 左右)。"""
        ctl = self._svc._controller
        key = (arm or "").strip()
        low = key.lower()
        if low == "left" or key == ctl.left_arm_name:
            return ctl.left_arm_name, "left"
        if low == "right" or key == ctl.right_arm_name:
            return ctl.right_arm_name, "right"
        return "", ""

    def _reject(
        self, code: str, message: str, *, stage: str = "", speak: bool = True,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        logger.info("[关节流移动] 拒绝 (%s/%s): %s", code, stage, message)
        if speak and self._speak_enabled:
            self._svc._speak_text(message)
        return {
            "ok": False, "code": code, "stage": stage, "message": message,
            **(extra or {}),
        }

    # ------------------------------------------------------------------
    # 逆解失败时, 给出「这个位置到底能到的姿态」
    # ------------------------------------------------------------------
    #
    # 背景: 抓取姿态是**示教**的, 而示教的姿态可能与准备点姿态矛盾,
    # 于是在目标点根本不可达 —— 机械臂只报 ``unreachable``, 用户完全
    # 不知道该改成什么。
    #
    # 这里做一次粗网格扫描, 找出全姿态可解的 (a, b, c), 并挑出工具 Z
    # 轴最接近「朝下」的几个 —— 那通常就是抓取想要的姿态。
    _SUGGEST_A = range(-180, 180, 30)
    _SUGGEST_B = range(-90, 91, 15)
    _SUGGEST_C = range(-180, 180, 45)

    def _suggest_poses(
        self,
        kin: Any,
        pos: Tuple[float, float, float],
        cur_rpy: Tuple[float, float, float],
        seed: List[float],
        limit: int = 4,
    ) -> str:
        """扫描可达姿态, 返回一句人类可读的建议。

        返回空串表示扫遍了也没找到全姿态可解的解。
        """
        try:
            import numpy as np
            import pinocchio as pin

            from robot.kinematics import rpy_to_matrix
        except Exception:  # pragma: no cover - 依赖缺失时静默降级
            return ""

        found = []
        for a_deg in self._SUGGEST_A:
            for b_deg in self._SUGGEST_B:
                for c_deg in self._SUGGEST_C:
                    rpy = (math.radians(a_deg), math.radians(b_deg),
                           math.radians(c_deg))
                    try:
                        T = pin.SE3(rpy_to_matrix(rpy),
                                   np.asarray(pos, dtype=float))
                        r = kin.ik_base(kin.T_world_base.inverse() * T, seed,
                                        max_iters=120, tol_pos=1e-3,
                                        tol_rot=1e-2)
                    except Exception:
                        continue
                    if r.ok:
                        found.append((a_deg, b_deg, c_deg,
                                      float(rpy_to_matrix(rpy)[2, 2])))
        if not found:
            return ""

        # 工具 Z 轴最朝下的排前面 —— 抓取一般要夹爪朝下
        found.sort(key=lambda t: t[3])
        parts = []
        for a_deg, b_deg, c_deg, _ in found[:limit]:
            parts.append(
                f"a={a_deg:.4f} b={math.radians(b_deg):.4f} "
                f"c={math.radians(c_deg):.4f}"
            )
        return "; ".join(parts)
