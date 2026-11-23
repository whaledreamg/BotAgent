"""笛卡尔移动: 把世界系下的目标位姿交给控制器逆解并走过去。

流程 (任何一步失败都立刻收尾, 绝不留下残留会话):
    安全门阀 -> 运动学快照 (基座位姿) -> 世界系换到基座系 -> 臂展粗检
    -> 起末端流 (先于 startControl) -> startControl (cartesianIkPath=analytic)
    -> 取入控位姿 -> S 曲线轨迹下发 -> 轮询会话状态判可达性
    -> stopControl 交还通道 -> 读回 TCP 核对到位

**关于可达性**: 逆解在控制器内部做, 我们拿不到关节角, 也就没法在下发前
自己做关节限位检查。能做的有两层:
  1. 下发前: 目标到臂基座的距离不超过臂展 (粗检, 配置 ``grasp.max_reach``)
  2. 下发后: 读会话的 ``analyticIkStage`` / ``analyticIkPositionDeviation`` /
     ``error``, 控制器把目标收缩过或解不出, 就如实报告「没走到」
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from robot.cartesian import CartesianStreamer
from robot.controller import Phase
from robot.geometry import (
    Pose,
    distance,
    euler_to_quat,
    pose_from_xyzabc,
    pose_inv,
    pose_mul,
    quat_angle,
    quat_to_euler,
    world_to_base,
)

logger = logging.getLogger("arm.mover")

#: 到位判据: TCP 与目标的距离 (米)
ARRIVE_TOL_M = 0.02
#: 控制器收缩过目标超过此值就认为够不着 (米)
CLAMP_TOL_M = 0.005
#: 运动时长合法范围 (秒)
MIN_DURATION = 0.5
MAX_DURATION = 30.0
#: 入控响应等待上限 (秒)。首次为该臂构建求解器会耗数百毫秒
START_TIMEOUT = 8.0
#: 轨迹发完后再多保持多久 (秒), 让机器人收敛到终点
SETTLE_S = 0.4
#: 轮询会话状态的间隔 (秒)
POLL_S = 0.25
#: 工具偏置告警阈值
TOOL_WARN_POS_M = 0.005
TOOL_WARN_ROT_RAD = 0.02


def translate_control_error(code: Any, message: str) -> str:
    """把入控失败翻译成能念出来的话。"""
    text = str(message or "")
    code_s = str(code)
    if "10074" in text or "远程模式" in text or "手动" in text:
        return "控制器处于手动模式，请切到自动模式"
    if "未上使能" in text:
        return "机械臂没有上使能，请先上使能"
    if "analytic" in text or code_s in ("24063", "24086"):
        return "这台机械臂不支持末端位姿控制"
    if "故障" in text or "急停" in text or "安全" in text:
        return "机械臂处于故障或安全保护状态，请先处理"
    if "占用" in text or "已有" in text or "会话" in text:
        return "机械臂通道被占用，请稍后再试"
    return f"机械臂入控失败，错误码 {code_s}"


class CartesianMover:
    """笛卡尔移动执行器。依附于 ArmService。"""

    def __init__(self, service: Any) -> None:
        self._svc = service
        self._lock = threading.Lock()
        self._abort = threading.Event()
        #: 本次移动失败时是否由服务端播报 (调用方自己会念时关掉, 避免念两遍)
        self._speak_enabled = True
        #: 入控响应的同步槽 (由 ArmService._handle_start_control 填)
        self._start_event = threading.Event()
        self._start_result: Dict[str, Any] = {}

    # ------------------------------------------------------------ 外部入口

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def abort(self) -> None:
        """打断正在进行的移动 (stop() 调用)。"""
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
        TCP 姿态**, 只挪位置 (见 ``configs/arm.yaml`` 的 ``grasp``)。

        ``speak=False`` 时失败不由服务端播报。

        **控制指令总闸** (``arm.enabled=false``): 只算不动 —— 返回
        ``ok=false, code="disabled"`` 并附上算好的目标位姿, 一个控制指令
        都不下发。

        **下发之前会先做规划检查**, 任何一项不过就**一个指令都不发**:
          - 目标离基座太远 (``out_of_reach``)
          - 门阀不通过 (``safety``)
        """
        # ---- 控制指令总闸 ----
        #
        # 这是机械臂的**最后一道**安全闸: 放在这里意味着不管上层怎么调
        # (抓取 skill / 动作库 / LLM tool calling), 只要这个开关关着,
        # 就不会有任何控制指令发到控制器。
        #
        # 位置照算照报 —— 用户要看的正是算出来的坐标对不对。
        if not self._svc.control_enabled:
            logger.warning("[移动] 控制指令总闸已关闭: 只算位置, 不下发指令")
            return self._reject(
                "disabled",
                "机械臂控制已关闭，只计算了位置，机械臂没有动",
                stage="gate",
                speak=False,
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

    # ------------------------------------------------------------ 主流程

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
        #
        # 必须在入控之前, 理由同 _do_play: 带故障/急停/未使能入控, 不是被控制器
        # 拒, 就是动到一半被保护停掉。
        report = svc.check_safety()
        if not report.ok:
            logger.warning("[移动] 门阀拒绝: %s", report.reason)
            if self._speak_enabled:
                svc._speak_reject(report)
            return self._reject(
                report.code or "safety", report.reason, stage="safety", speak=False
            )

        # ---- 2. 选臂 ----
        name, side = self._resolve_arm(arm)
        if not name:
            return self._reject("no_arm", f"找不到机械臂 {arm!r}", speak=True)

        # ---- 3. 运动学快照: 基座位姿与当前 TCP ----
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
        base_pose = pose_from_xyzabc(data["base_frame"])
        tcp_world = pose_from_xyzabc(data["cartesian"])

        # ---- 4. 目标位姿 (世界系) 与臂展粗检 ----
        #
        # **姿态**: 优先用请求里给的 a/b/c; 没给就用配置里示教的;
        # 两边都没有就**保持当前 TCP 姿态** (只挪位置)。
        #
        # 「保持当前姿态」是默认且推荐的做法 —— 末端位姿一变, 七个关节
        # 全得跟着动, 很容易撞软限位被控制器停掉 (13101)。只挪位置
        # 时关节角变化最小。
        #
        # **别拿 0,0,0 当占位**: 那是「姿态全零」不是「不指定」。
        # 机械臂现在若是 (2.97, 0.74, -3.00), 硬转到全零要转 138 度,
        # 必然把某个关节推出软限位。
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
            # 保持当前 TCP 姿态: 把当前欧拉角当成示教值用。
            cur = data["cartesian"]
            oa = float(cur["a"])
            ob = float(cur["b"])
            oc = float(cur["c"])
            keep_orientation = True
            logger.info(
                "[移动] %s 没示教抓取姿态 —— 保持当前 TCP 姿态 "
                "(a=%.4f, b=%.4f, c=%.4f), 只挪位置",
                name, oa, ob, oc,
            )

        target_world: Pose = ((x, y, z), euler_to_quat(oa, ob, oc))
        target_base = world_to_base(
            base_pose[0], base_pose[1], target_world[0], target_world[1]
        )
        reach = math.sqrt(sum(v * v for v in target_base[0]))
        travel = distance(tcp_world[0], target_world[0])
        info: Dict[str, Any] = {
            "arm": name,
            "side": side,
            "duration": duration,
            "target_world": {"x": x, "y": y, "z": z, "a": oa, "b": ob, "c": oc},
            "keep_orientation": keep_orientation,
            "target_base": {
                "x": target_base[0][0], "y": target_base[0][1], "z": target_base[0][2],
            },
            "reach_m": round(reach, 4),
            "max_reach_m": grasp.max_reach,
            "travel_m": round(travel, 4),
            "tcp_before": dict(data["cartesian"]),
        }
        logger.info(
            "[移动] %s 目标(世界系)=(%.4f, %.4f, %.4f) abc=(%.3f, %.3f, %.3f) "
            "基座系=(%.4f, %.4f, %.4f) 距基座 %.3fm (上限 %.2fm) 行程 %.3fm 时长 %.1fs",
            name, x, y, z, oa, ob, oc, *target_base[0], reach, grasp.max_reach,
            travel, duration,
        )
        if reach > grasp.max_reach:
            return self._reject(
                "out_of_reach",
                f"目标离机械臂基座 {reach:.2f} 米，超过可达范围 {grasp.max_reach:.2f} 米",
                stage="plan", speak=True, extra=info,
            )

        # ---- 5. 确保这条臂上了使能 ----
        if name in ctl.disabled_arms():
            ctl.enable_arms()
            deadline = time.time() + 3.0
            while time.time() < deadline and name in ctl.disabled_arms():
                time.sleep(0.05)
            if name in ctl.disabled_arms():
                return self._reject(
                    "enable_failed", "机械臂上使能失败", stage="plan", speak=True, extra=info
                )

        # ---- 6. 清理残留会话 + 入控 ----
        conn.get_realtime_status()
        time.sleep(0.3)

        streamer = CartesianStreamer(
            host=conn.host or "127.0.0.1",
            port=ctl.streamer_port,
            robot=name,
            frequency=ctl.streamer_frequency,
        )
        entered = False
        try:
            ctl.clear_failure()
            ctl._set_phase(Phase.ENTERING, "正在进入末端控制")
            self._start_event.clear()
            self._start_result = {}

            # 起流必须早于 startControl (首包等待窗只有 2000ms)
            streamer.start()
            cfg = ctl.cartesian_control_config()
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
                text = translate_control_error(res.get("code"), res.get("message", ""))
                logger.warning("[移动] 入控失败 (%s): %s", res.get("code"), res.get("message"))
                info["controller_error"] = {
                    "code": res.get("code"), "message": res.get("message"),
                }
                return self._reject("enter_failed", text, stage="enter", speak=True, extra=info)

            # ---- 7. 入控位姿 -> 工具偏置 -> 法兰目标 ----
            start = self._entry_pose(res["db"])
            if start is None:
                return self._reject(
                    "no_entry_pose", "控制器没有返回入控位姿", stage="enter", speak=True, extra=info
                )
            flange_target, tool = self._flange_target(
                start, base_pose, tcp_world, target_base
            )
            info["tool_offset"] = {
                "pos_m": round(math.sqrt(sum(v * v for v in tool[0])), 5),
                "rot_rad": round(quat_angle(tool[1], (1.0, 0.0, 0.0, 0.0)), 5),
            }
            if (
                info["tool_offset"]["pos_m"] > TOOL_WARN_POS_M
                or info["tool_offset"]["rot_rad"] > TOOL_WARN_ROT_RAD
            ):
                logger.warning(
                    "[移动] TCP 与法兰不重合 (偏置 %.4fm / %.3frad), 已按偏置补偿。"
                    "若没配过工具, 说明 a/b/c 的欧拉约定与预期不一致, 请核对",
                    info["tool_offset"]["pos_m"], info["tool_offset"]["rot_rad"],
                )

            # ---- 8. 下发轨迹并监控 ----
            ctl._set_phase(Phase.PLAYING, "正在移动到目标点")
            streamer.set_trajectory(
                start[0], start[1], flange_target[0], flange_target[1], duration
            )
            abort_msg = self._monitor(streamer, name, duration, info)
            if abort_msg:
                return self._reject(
                    info.get("abort_code", "aborted"), abort_msg, stage="move", speak=True, extra=info
                )

            # 终点的可达性结论: 此时发的全是终点, 偏差就是最终够不着的量
            session = self._session(name)
            verdict = self._reachability(session, info)
        finally:
            streamer.stop()
            if entered:
                conn.stop_control([name])
                logger.info("[移动] 已发 realtime/stopControl")
            ctl._set_phase(Phase.IDLE)

        # ---- 9. 读回 TCP 核对 ----
        time.sleep(0.3)
        after = svc.refresh_kinematics()
        tcp_after = ((after.get("arms") or {}).get(name) or {}).get("cartesian")
        if tcp_after:
            info["tcp_after"] = dict(tcp_after)
            err_m = distance(
                (tcp_after["x"], tcp_after["y"], tcp_after["z"]), (x, y, z)
            )
            actual_q = euler_to_quat(tcp_after["a"], tcp_after["b"], tcp_after["c"])
            info["error_m"] = round(err_m, 4)
            info["error_rot_rad"] = round(quat_angle(actual_q, target_world[1]), 4)
        else:
            err_m = float("inf")

        if verdict:
            message = verdict
            code = "unreachable"
            ok = False
        elif err_m > ARRIVE_TOL_M:
            message = f"没有完全到位，离目标还差 {err_m * 100:.1f} 厘米"
            code = "not_arrived"
            ok = False
        else:
            message = f"已到达目标点，误差 {err_m * 100:.1f} 厘米"
            code = "ok"
            ok = True

        logger.info("[移动] %s: %s", "成功" if ok else "失败", message)
        if not ok and self._speak_enabled:
            svc._speak_text(message)
        return {"ok": ok, "code": code, "stage": "done", "message": message, **info}

    # ------------------------------------------------------------ 子步骤

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

    @staticmethod
    def _entry_pose(db: Dict[str, Any]) -> Optional[Pose]:
        """入控响应 ``pose.quaternion`` = [x, y, z, qw, qx, qy, qz]。"""
        pose = db.get("pose")
        if not isinstance(pose, dict):
            return None
        q = pose.get("quaternion")
        if not isinstance(q, list) or len(q) != 7:
            return None
        try:
            v = [float(i) for i in q]
        except (TypeError, ValueError):
            return None
        return ((v[0], v[1], v[2]), (v[3], v[4], v[5], v[6]))

    @staticmethod
    def _flange_target(
        entry_flange_base: Pose,
        base_world: Pose,
        tcp_world: Pose,
        target_base: Tuple[Any, Any],
    ) -> Tuple[Pose, Pose]:
        """由 TCP 目标 (基座系) 反算法兰目标。

        入控位姿是**法兰**在基座系下的位姿, 而 ``robot/status.cartesian`` 是
        含工具偏置的 TCP。两者在入控瞬间是同一时刻的同一个机器人, 所以
        ``tool = flange⁻¹ · tcp`` 能直接算出工具偏置, 不用假设「没装工具」。
        """
        tcp_base = pose_mul(pose_inv(base_world), tcp_world)
        tool = pose_mul(pose_inv(entry_flange_base), tcp_base)
        flange = pose_mul((target_base[0], target_base[1]), pose_inv(tool))
        return flange, tool

    def _session(self, name: str) -> Dict[str, Any]:
        """读控制器上该臂的会话状态 (同步)。"""
        resp = self._svc._conn.request_sync(
            "realtime/getStatus", {"robot": None}, timeout=2.0
        )
        if not isinstance(resp, dict) or resp.get("err"):
            return {}
        db = resp.get("db")
        if not isinstance(db, dict):
            return {}
        for item in db.get("sessions") or []:
            if isinstance(item, dict) and item.get("robot") == name:
                return item
        return {}

    def _monitor(
        self, streamer: CartesianStreamer, name: str, duration: float, info: Dict[str, Any]
    ) -> str:
        """轨迹期间每 POLL_S 看一次会话。返回非空表示要中止, 内容是播报文案。"""
        end = time.monotonic() + duration + SETTLE_S
        while time.monotonic() < end:
            if self._abort.is_set():
                info["abort_code"] = "stopped"
                return "已停止移动"
            if self._svc._controller.failed:
                info["abort_code"] = "controller_error"
                return self._svc._controller.last_error or "机械臂入控失败"
            time.sleep(POLL_S)
            session = self._session(name)
            err = str(session.get("error") or "none")
            state = str(session.get("state") or "")
            if err.startswith("stopped") or state in ("stopped", "stopping"):
                info["abort_code"] = "session_stopped"
                info["session_error"] = err
                return self._translate_session_error(err)
        return ""

    @staticmethod
    def _translate_session_error(err: str) -> str:
        """把控制器的会话错误翻成一句能听懂的话。

        ``stoppedBySafety`` 是**笼统**的说法 —— 控制器只告诉我们
        「被安全监控停掉了」, 没说是哪一项触发。笼统的话术会让用户
        以为是急停没松, 或者以为要切什么模式, 其实往往只是
        「关节撞软限位」或「末端太快」。

        真正的原因要去控制器的告警里找 (``log/queryLog``), 常见两类:

        - ``13101`` 关节位置超出软限位 —— 姿态转得太多, 某个关节被推出了限位
        - ``13105`` 末端 (TCP) 过速 —— 轨迹太快

        这里给一个**指向性**的提示, 让用户知道该往哪查, 而不是
        笼统地说「安全保护」。
        """
        if err == "stoppedBySafety":
            return (
                "机械臂被控制器安全停掉了, 已停止移动。"
                "通常是关节撞到软限位或末端过快, 请检查抓取姿态和运动速度"
            )
        if err == "stoppedByNumericalIkFailure":
            return "目标点够不着，机械臂已停止"
        if err == "stoppedByStreamLoss":
            return "指令流中断，机械臂已停止"
        return f"机械臂会话被控制器停止 ({err})"

    @staticmethod
    def _reachability(session: Dict[str, Any], info: Dict[str, Any]) -> str:
        """由会话观测判终点是否可达。可达返回空串。"""
        stage = str(session.get("analyticIkStage") or "")
        dev = float(session.get("analyticIkPositionDeviation") or 0.0)
        err = str(session.get("error") or "none")
        info["ik"] = {
            "stage": stage,
            "position_deviation_m": round(dev, 4),
            "error": err,
            "stage_counts": session.get("analyticIkStageCounts"),
        }
        if err == "numericalIkFailed" or stage == "held":
            return "目标点够不着，机械臂没有移动到位"
        if dev > CLAMP_TOL_M:
            return f"目标点超出可达范围，只能走到离目标 {dev * 100:.1f} 厘米处"
        return ""

    def _reject(
        self,
        code: str,
        message: str,
        stage: str = "plan",
        speak: bool = True,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        logger.warning("[移动] 拒绝 (%s/%s): %s", stage, code, message)
        if speak and self._speak_enabled:
            self._svc._speak_text(message)
        out: Dict[str, Any] = {
            "ok": False, "code": code, "stage": stage, "message": message,
        }
        if extra:
            out.update(extra)
        return out
