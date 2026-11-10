"""动作服务: 把动作库、控制面、关节流编排成一个可远程驱动的播放器。

对外:
- HTTP  : 下发播放/停止, 查询动作库与状态
- WS    : 实时推送阶段与进度

播放流程 (严格按顺序, 踩错就是机器人不动):
    查动作库 -> 切出片段组 -> 时间轴
    -> **安全门阀检查状态** (不通过则拒绝并 TTS 播报原因)
    -> 清理控制器残留会话 -> 确保实测关节角
    -> 起流 (独立线程) -> 并行发 startControl
    -> 等入控成功 -> 700ms S 曲线对齐
    -> 时间轴推进 -> 播放结束 -> stopControl 交还通道
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from config import Config
from library import ActionLibrary, ActionNotFound, ActionUnit
from notifier import ArmNotifier
from robot.connection import RobotConnection
from robot.controller import ArmController, ControllerError, Phase
from safety import SafetyGate, SafetyReport
from timeline.model import JOINT_COUNT, JointState
from timeline.playback import PlaybackEngine

logger = logging.getLogger("arm.service")

#: 播放循环的 tick 间隔 (毫秒)。16ms 足够顺滑, 也不会把 CPU 压满。
TICK_MS = 16

#: 阻塞调用 (连接控制器等) 用的线程池
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="arm-io")


async def await_run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """在线程池里跑一个阻塞函数, 不阻塞事件循环。

    控制器的 WebSocket 握手、HTTP 响应都是阻塞的, 直接调会把 FastAPI 的
    事件循环卡住, 期间所有 HTTP 请求都超时。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, lambda: fn(*args, **kwargs))


def run_blocking(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """在后台线程里跑一个阻塞函数, 并等它返回。

    用于本来就在后台线程 (播放线程) 里的场景 —— 那里阻塞是允许的,
    但仍要保证不会卡住事件循环。
    """
    future = _EXECUTOR.submit(fn, *args, **kwargs)
    return future.result()


class ArmService:
    """机械臂动作服务。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.arm_cfg = cfg.arm

        self.library = ActionLibrary(
            self.arm_cfg.action_path,
            exclude_files=self.arm_cfg.exclude_files,
        )

        self._conn = RobotConnection(
            self.arm_cfg.controller_ws,
            timeout=self.arm_cfg.connect_timeout,
            on_state=self._on_robot_state,
            on_event=self._on_controller_event,
        )

        self._controller = ArmController(
            self._conn,
            frequency=self.arm_cfg.frequency,
            command_port=self.arm_cfg.command_port,
            lead_in_ms=self.arm_cfg.lead_in_ms,
            joint_limit=self.arm_cfg.joint_limit.to_config(),
            idle_stop_ms=self.arm_cfg.idle_stop_ms,
            on_phase=self._on_phase,
            on_error=self._on_error,
        )

        # 当前播放上下文
        self._lock = threading.RLock()
        self._engine: Optional[PlaybackEngine] = None
        self._current_action: Optional[str] = None
        self._current_key: str = ""
        self._current_duration: float = 0.0
        self._play_thread: Optional[threading.Thread] = None
        self._stop_requested = False
        self._loop = False
        self._speed = 1.0

        # ---- 播放队列 ----
        #
        # 队列里存的是**还没播**的动作名。整个队列只入控一次, 动作之间
        # 不松手 —— 机械臂运动连贯, 也省掉每个动作重新入控的 700ms 对齐开销。
        self._queue: List[str] = []
        #: 队列里所有动作 (含正在播的), 用于状态查询
        self._queue_all: List[str] = []
        #: 队列的总时长
        self._queue_duration: float = 0.0
        #: 当前队列中正在播的那个是不是最后一个 (最后一个播完就要停控)
        self._queue_started: bool = False

        # WebSocket 订阅者
        self._subscribers: List[Any] = []
        self._sub_lock = threading.Lock()

        self._started = False
        #: 最近一次查询 robot/status 的臂名 (应答里没有 name 字段, 只能记住)
        self._status_query_arm = ""

        # ---- 安全门阀 ----
        #
        # 动作执行前必须先检查控制器状态: 急停、安全锁定、伺服故障、没使能
        # 都会导致「下发成功但机械臂不动」或动作中途卡死, 而且静默失败时
        # 用户完全不知道出了什么事。
        self._gate = SafetyGate(
            require_enabled=cfg.safety.require_enabled,
            require_connection=cfg.safety.require_connection,
            allowed_modes=frozenset(cfg.safety.allowed_modes),
            check_fault=cfg.safety.check_fault,
        )
        #: 最近一次门阀检查结果 (供 /v1/status 与 /health 查看)
        self._last_safety: Optional[SafetyReport] = None
        #: 门阀拒绝时的 TTS 播报器
        self._notifier = ArmNotifier(
            ws_url=cfg.notifier.ws_url,
            voice=cfg.notifier.voice,
            speed=cfg.notifier.speed,
            sample_rate=cfg.notifier.sample_rate,
            timeout=cfg.notifier.timeout,
            enabled=cfg.notifier.enabled and cfg.safety.speak_on_reject,
            server_play=cfg.notifier.server_play,
        )

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """启动服务: 加载动作库, 尝试连接控制器。

        控制器连不上不阻止服务启动 —— /health 会反映 controller_connected=false,
        这样没有真机时也能验证动作库与协议逻辑。
        """
        self.library.load()

        self._started = True
        # 连接是阻塞调用 (握手要等控制器的 HTTP 响应), 不能直接放在 FastAPI 的
        # 事件循环线程里 —— 那样会把整个服务卡住直到超时。丢到线程池里做。
        connected = await await_run(self._conn.connect)
        if connected:
            self._on_connected()
        else:
            logger.warning("[服务] 控制器不可达 (%s), 服务以降级模式运行",
                           self.arm_cfg.controller_ws)

    def shutdown(self) -> None:
        self._started = False
        self._stop_playback(silent=True)
        self._controller.shutdown()
        self._conn.disconnect()

    # ------------------------------------------------------------ 控制器事件

    def _on_connected(self) -> None:
        """连上控制器: 拉机器人清单以识别左右臂。"""
        self._conn.list_robots()

    def _on_robot_state(self, state: Dict[str, Any]) -> None:
        """收到状态推送: 提取关节角喂给控制器。"""
        name = str(state.get("name", ""))
        # 使能状态要先记下来: 未上使能时入控会被拒 (24077), 播放前要补使能。
        if "enabled" in state and name:
            self._controller.set_arm_enabled(name, bool(state.get("enabled")))

        joints = state.get("joints")
        if not isinstance(joints, list) or len(joints) < JOINT_COUNT:
            return
        try:
            values = [float(v) for v in joints[:JOINT_COUNT]]
        except (TypeError, ValueError):
            return
        self._controller.set_actual_joints(str(name), values)

    def _on_controller_event(self, action: str, response: Dict[str, Any]) -> None:
        """收到控制器的请求-响应。"""
        db = response.get("db")
        if not isinstance(db, (dict, list)):
            db = {}

        if action == "robot/list":
            self._handle_robot_list(db)
        elif action == "robot/status":
            self._handle_robot_status(db)
        elif action == "realtime/getStatus":
            self._handle_realtime_status(db, response)
        elif action == "realtime/startControl":
            self._handle_start_control(db, response)

    def _handle_robot_list(self, db: Any) -> None:
        """从 robot/list 识别左右臂。

        db 有两种形态 (实测真实控制器):
        - 直接是数组: [{"id":0,"name":"LeftArm",...}, ...]
        - 对象带 items: {"items":[...]}
        两种都要认。

        不假设「第 0 个就是左臂」—— 逐个按名字归类, 判断不出左右的按剩余顺序补齐。
        """
        items: List[Any] = []
        if isinstance(db, list):
            items = db
        elif isinstance(db, dict):
            raw = db.get("items")
            if isinstance(raw, list):
                items = raw

        left = ""
        right = ""
        unnamed: List[str] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            if not name:
                continue
            arm = _arm_of(name)
            if arm == "left" and not left:
                left = name
            elif arm == "right" and not right:
                right = name
            else:
                unnamed.append(name)

        # 名称判断不出左右的, 按剩余顺序补齐
        for name in unnamed:
            if not left:
                left = name
            elif not right:
                right = name
                break

        # 配置里显式指定了臂名就以配置为准
        if self.arm_cfg.left_arm:
            left = self.arm_cfg.left_arm
        if self.arm_cfg.right_arm:
            right = self.arm_cfg.right_arm

        self._controller.set_arm_names(left, right)
        logger.info("[服务] 识别到左臂=%s 右臂=%s", left or "(无)", right or "(无)")

        for name in (left, right):
            if name:
                self._conn.subscribe_state(name, 50)
                self._query_robot_status(name)

    def _query_robot_status(self, name: str) -> None:
        """查询某臂状态, 并记住是哪条臂 (应答里没有 name 字段)。"""
        self._status_query_arm = name
        self._conn.get_robot_status(name)

    def _handle_robot_status(self, db: Any) -> None:
        if not isinstance(db, dict):
            return
        # robot/status 的应答里**没有** name 字段 (实测), 所以用查询时记住的臂名。
        name = str(db.get("name") or self._status_query_arm)
        # 使能状态要先记下来: 未上使能时入控会被拒 (24077), 播放前要补使能。
        if "enabled" in db and name:
            self._controller.set_arm_enabled(name, bool(db.get("enabled")))

        joints = db.get("joints")
        if not isinstance(joints, list) or len(joints) < JOINT_COUNT:
            return
        try:
            values = [float(v) for v in joints[:JOINT_COUNT]]
        except (TypeError, ValueError):
            return
        self._controller.set_actual_joints(name, values)

    def _handle_realtime_status(self, db: Any, response: Dict[str, Any]) -> None:
        """处理 realtime/getStatus 的响应。

        两件事:
        1. 端口以控制器回读为准
        2. 清掉残留会话 —— 控制器上残留的会话 (尤其 state="stopped" 的) 会占住
           该臂, 表现为「发了指令但机械臂不动」而且不报任何错。
        """
        if not isinstance(db, dict):
            return

        port = db.get("commandPort")
        if isinstance(port, int) and 0 < port <= 65535:
            self._controller._streamer.set_port(port)

        if db.get("commandPortReady") is False:
            msg = "控制器实时指令端口未就绪 (commandPortReady=false), 无法入控"
            self._controller._fail(msg)
            return

        sessions = db.get("sessions")
        if not isinstance(sessions, list):
            return

        active_arms = set(self._controller.active_arms())
        stale = []
        for item in sessions:
            if not isinstance(item, dict):
                continue
            robot = str(item.get("robot", ""))
            if robot and robot in active_arms:
                stale.append(robot)

        if stale:
            logger.warning("[服务] 清理控制器上的遗留会话: %s", stale)
            for robot in stale:
                self._conn.stop_control([robot])

    def _handle_start_control(self, db: Any, response: Dict[str, Any]) -> None:
        """入控响应。"""
        if "err" in response:
            err = response.get("err") or {}
            code = err.get("code", "?")
            message = err.get("message", "未知错误")
            self._controller._fail(f"入控失败 ({code}): {message}")
            return

        if isinstance(db, dict):
            state = db.get("state")
            if state and state != "active":
                logger.warning("[服务] 入控后状态异常: %s", state)

        logger.info("[服务] 入控成功, 等待起点对齐")
        # 对齐由播放线程推进: 拿到轨迹起点后进入 ALIGNING, 对齐完成再推时间轴

    # ------------------------------------------------------------ 阶段/错误

    def _on_phase(self, phase: str, detail: str) -> None:
        self._broadcast({"type": "state", "phase": phase, "detail": detail})

    def _on_error(self, message: str) -> None:
        self._broadcast({"type": "error", "message": message})

    # ------------------------------------------------------------ 订阅

    def subscribe(self, queue: Any) -> None:
        with self._sub_lock:
            self._subscribers.append(queue)

    def unsubscribe(self, queue: Any) -> None:
        with self._sub_lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def _broadcast(self, message: Dict[str, Any]) -> None:
        with self._sub_lock:
            targets = list(self._subscribers)
        for queue in targets:
            try:
                queue.put_nowait(message)
            except Exception:
                pass

    # ------------------------------------------------------------ 播放

    def play(
        self,
        action_name: str,
        speed: float = 1.0,
        loop: bool = False,
    ) -> Dict[str, Any]:
        """下发播放 (单个动作)。立即返回, 实际播放在后台线程进行。"""
        return self.play_many([action_name], speed=speed, loop=loop)

    def play_many(
        self,
        action_names: List[str],
        speed: float = 1.0,
        loop: bool = False,
    ) -> Dict[str, Any]:
        """下发播放 (动作队列)。立即返回, 实际播放在后台线程进行。

        **整个队列只入控一次** —— 动作之间不松手, 机械臂运动连贯,
        也省掉每个动作重新入控的 700ms S 曲线对齐开销。

        每个动作播完时重新取一次实测关节角作为下一个动作的起点 ——
        轨迹不落盘起始姿态, 避免把机器人拽到某个固定姿态上去。
        """
        names = [str(x).strip() for x in (action_names or []) if str(x).strip()]
        if not names:
            raise ActionNotFound("动作名为空")

        # 全部动作先查一遍 —— 有不存在的就别开始了, 免得播到一半才发现
        units = [self.library.get(name) for name in names]
        total_duration = sum(u.duration for u in units)

        with self._lock:
            if self._play_thread is not None and self._play_thread.is_alive():
                raise ControllerError("已有动作正在播放, 请先停止")

            self._stop_requested = False
            self._loop = loop
            self._speed = max(0.1, min(3.0, speed))
            self._current_action = units[0].label
            self._current_key = units[0].key
            self._current_duration = units[0].duration

            # 队列里存**还没播**的动作 (第一个马上要播, 不入队)
            self._queue = [u.label for u in units[1:]]
            self._queue_all = [u.label for u in units]
            self._queue_duration = total_duration
            self._queue_started = False

            thread = threading.Thread(
                target=self._play_loop,
                args=(units[0],),
                name="arm-playback",
                daemon=True,
            )
            self._play_thread = thread

        thread.start()
        logger.info(
            "[服务] 下发播放队列 (%d 个动作, 共 %.2fs): %s",
            len(units),
            total_duration,
            " -> ".join(u.label for u in units),
        )
        return {
            "ok": True,
            "action": units[0].label,
            "key": units[0].key,
            "duration": round(total_duration, 3),
            "actions": [u.label for u in units],
            "phase": Phase.ENTERING,
            "message": "",
        }

    def _play_loop(self, unit: ActionUnit) -> None:
        """后台播放线程。

        播完当前动作后, 如果队列里还有, 就接着播下一个 ——
        **不重新入控**, 直接推新轨迹。
        """
        try:
            self._do_play(unit)
        except ActionNotFound as exc:
            self._controller._fail(str(exc))
        except ControllerError as exc:
            self._controller._fail(str(exc))
        except Exception as exc:  # pragma: no cover - 兜底
            logger.exception("[服务] 播放异常")
            self._controller._fail(f"播放异常: {exc}")
        finally:
            with self._lock:
                self._play_thread = None

    # ------------------------------------------------------------ 安全门阀

    def _fetch_status(self, arm_name: str) -> Optional[Dict[str, Any]]:
        """同步取一份 `robot/status` 快照。

        用请求-响应而不是状态推送: 门阀要的是「当下的」确定状态,
        而推送是异步的、只在订阅周期内到达。
        """
        timeout = max(0.5, self.cfg.safety.timeout_ms / 1000.0)
        try:
            resp = self._conn.request_sync("robot/status", {"name": arm_name}, timeout)
        except Exception as exc:
            logger.warning("[门阀] 查询 %s 状态失败: %s", arm_name, exc)
            return None
        if not isinstance(resp, dict):
            return None
        if resp.get("err"):
            err = resp.get("err") or {}
            logger.warning(
                "[门阀] robot/status 返回错误 (%s): %s",
                err.get("code"),
                err.get("message"),
            )
            return None
        db = resp.get("db")
        return db if isinstance(db, dict) else None

    def check_safety(self) -> SafetyReport:
        """跑一次安全门阀检查。

        检查**所有**臂 —— 任何一条臂有异常, 都不该动 (双臂共用一条通道时,
        一条臂异常意味着另一条也处于不确定状态)。
        """
        report = SafetyReport()

        if not self.cfg.safety.enabled:
            report.ok = True
            report.code = "disabled"
            self._last_safety = report
            return report

        arms = self._controller.active_arms()
        if not arms:
            report.ok = False
            report.code = "no_arm"
            report.reason = "还没认到机械臂"
            self._last_safety = report
            return report

        all_checks = []
        for arm_name in arms:
            status = self._fetch_status(arm_name)
            sub = self._gate.check(status, connected=self._conn.connected)
            # 把臂名带进每项检查, 便于定位是哪条臂的问题
            for c in sub.checks:
                c.detail = f"[{arm_name}] {c.detail}".strip()
            all_checks.extend(sub.checks)
            if not sub.ok:
                report.ok = False
                report.code = sub.code
                report.reason = sub.reason
                break

        report.checks = all_checks
        if report.ok:
            report.reason = ""
            report.code = ""
        self._last_safety = report
        logger.info(
            "[门阀] %s (%d 项检查, %dms)",
            "放行" if report.ok else f"拒绝: {report.reason}",
            len(all_checks),
            report.elapsed_ms,
        )
        return report

    def _speak_reject(self, report: SafetyReport) -> None:
        """门阀拒绝时用 TTS 播报原因。

        **在后台线程里播** —— TTS 合成要一两秒, 不能让它卡住控制流:
        门阀已经判定拒绝了, 状态与阶段必须**立刻**更新, 播报是稍后的事。
        """
        if not self.cfg.safety.speak_on_reject:
            return
        text = self._gate.speak_text(report)
        if not text:
            return

        def _worker() -> None:
            try:
                self._notifier.speak(text)
            except Exception as exc:  # pragma: no cover - 播报失败不该影响主流程
                logger.warning("[门阀] 播报失败: %s", exc)

        threading.Thread(target=_worker, name="arm-notify", daemon=True).start()

    def _do_play(self, unit: ActionUnit) -> None:
        """播放一个动作 (队列里的第一个)。

        之后如果队列里还有, 由 `_play_next_in_queue()` 接着播 ——
        **不重新入控**。
        """
        # ---- 1. 确保控制器连接 ----
        if not self._conn.connected:
            self._controller._set_phase(Phase.CONNECTING, "正在连接控制器")
            # 连接是阻塞的 (握手要等控制器的 HTTP 响应), 放到线程池里做,
            # 否则会把 FastAPI 的事件循环卡住, 期间所有 HTTP 请求都超时。
            if not run_blocking(self._conn.connect):
                raise ControllerError(f"无法连接控制器: {self.arm_cfg.controller_ws}")
            self._on_connected()
            # 给 robot/list 一点时间回来
            deadline = time.time() + 3.0
            while time.time() < deadline and not self._controller.active_arms():
                time.sleep(0.05)

        if not self._controller.active_arms():
            raise ControllerError("尚未识别到机器人, 无法播放")

        # ---- 2. 安全门阀 ----
        #
        # 必须在入控之前。带故障/急停/未使能的状态去入控, 要么被控制器拒
        # (24077), 要么动到一半被安全保护停掉 —— 两种都不该由用户来承担后果。
        self._controller._set_phase(Phase.ENTERING, "正在检查机械臂状态")
        report = self.check_safety()
        if not report.ok:
            logger.warning("[门阀] 拒绝执行 %r: %s", unit.label, report.reason)
            self._speak_reject(report)
            raise ControllerError(f"安全门阀拒绝执行: {report.reason}")

        # ---- 3. 确保实测关节角 ----
        #
        # 先把没使能的臂使能上: 未上使能时入控会被控制器拒 (24077
        # 「机器人 X 未上使能, 不能入控」), 表现为「下发成功但机械臂不动」。
        if self._controller.disabled_arms():
            self._controller._set_phase(Phase.ENTERING, "正在给机械臂上使能")
            self._controller.enable_arms()
            deadline = time.time() + 3.0
            while time.time() < deadline and self._controller.disabled_arms():
                time.sleep(0.05)
            if self._controller.disabled_arms():
                raise ControllerError(
                    "机械臂上使能失败: " + ", ".join(self._controller.disabled_arms())
                )

        if not self._controller.has_actual_joints():
            self._controller._set_phase(Phase.ENTERING, "正在获取实测关节角")
            for arm_name in self._controller.active_arms():
                self._query_robot_status(arm_name)
            deadline = time.time() + 2.0
            while time.time() < deadline and not self._controller.has_actual_joints():
                time.sleep(0.05)
        if not self._controller.has_actual_joints():
            raise ControllerError("缺少实测关节角, 无法安全入控 (请确认机器人已使能并推送状态)")

        # ---- 4. 构造时间轴 ----
        origin = self._origin_pose()
        model = unit.to_timeline(origin)

        engine = PlaybackEngine(
            model,
            on_frame=self._on_frame,
            on_finished=self._on_playback_finished,
        )
        with self._lock:
            self._engine = engine

        # ---- 5. 清理残留会话 + 入控 ----
        #
        # 残留会话会占住该臂, 表现为「发了指令但不动」而且不报错。
        # 每次入控前必须先清一遍。
        self._conn.get_realtime_status()
        time.sleep(0.3)

        self._controller.enter_control()

        # ---- 6. 冻结起点 -> 对齐 -> 播放 ----
        #
        # prime_start_frame 冻结出发姿态并算出时间轴 0 时刻的那一帧。
        # 把这一帧推给控制器, 它会启动 700ms 的 S 曲线对齐。
        start_frame = engine.prime_start_frame()
        self._controller.push_frame(start_frame)

        # 等对齐完成
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self._controller.advance_lead_in():
                break
            if self._stop_requested:
                return
            # 入控失败时立刻中止, 不要把失败拖成「对齐超时」掩盖真实原因
            if self._controller.failed:
                raise ControllerError(self._controller.last_error or "入控失败")
            time.sleep(TICK_MS / 1000.0)

        if not self._controller.playback_active:
            if self._stop_requested:
                return
            if self._controller.failed:
                raise ControllerError(self._controller.last_error or "入控失败")
            raise ControllerError("起点对齐超时")

        # 对齐完成, 现在才推进时间轴
        engine.start()
        with self._lock:
            self._queue_started = True

        # ---- 7. 播放循环 ----
        self._run_engine(engine)

        # ---- 8. 队列推进 ----
        #
        # 当前动作播完。如果队列里还有下一个, 就接着播 —— **不重新入控**,
        # 直接推新轨迹。这样整个队列只入控一次, 动作之间不松手。
        if not self._stop_requested:
            self._play_next_in_queue()

    def _run_engine(self, engine: PlaybackEngine) -> None:
        """推进一个时间轴直到播完 (或被停止)。"""
        while True:
            if self._stop_requested:
                return
            # 倍速: 用缩放后的时间推进
            if self._speed != 1.0:
                engine._accumulated_ms += TICK_MS * (self._speed - 1.0)
            finished = engine.tick()
            if finished:
                if self._loop and not self._stop_requested:
                    engine.start()
                    continue
                return
            time.sleep(TICK_MS / 1000.0)

    def _play_next_in_queue(self) -> None:
        """播队列里的下一个动作。

        **不重新入控** —— 控制器通道还开着, 直接推新轨迹即可。

        下一个动作的起点取**当前实测姿态** (上一个动作刚停下的位置),
        轨迹不落盘起始姿态, 避免把机器人拽到某个固定姿态上去。
        """
        with self._lock:
            if not self._queue:
                # 队列空了 -> 收尾: 停控交还通道
                self._stop_playback(silent=True)
                return
            next_name = self._queue.pop(0)
            self._current_action = next_name

        unit = self.library.find(next_name)
        if unit is None:
            logger.warning("[服务] 队列里的动作 %r 不存在, 跳过", next_name)
            self._play_next_in_queue()
            return

        with self._lock:
            self._current_key = unit.key
            self._current_duration = unit.duration

        logger.info("[服务] 队列继续: %s (还剩 %d 个)", unit.label, len(self._queue))

        # ---- 每个动作切换前再查一次门阀 ----
        #
        # 队列可能播十几秒, 期间用户可能按了急停。下一个动作带故障去播,
        # 后果比第一个动作更严重。
        report = self.check_safety()
        if not report.ok:
            logger.warning("[门阀] 队列中断 (%s): %s", unit.label, report.reason)
            self._speak_reject(report)
            self._controller._fail(f"安全门阀拒绝执行: {report.reason}")
            self._stop_playback(silent=True)
            return

        # ---- 构造新时间轴 (起点 = 当前实测姿态) ----
        origin = self._origin_pose()
        model = unit.to_timeline(origin)
        engine = PlaybackEngine(
            model,
            on_frame=self._on_frame,
            on_finished=self._on_playback_finished,
        )
        with self._lock:
            self._engine = engine

        # ---- 冻结起点 -> 对齐 -> 播放 ----
        #
        # 这里仍然要对齐 —— 控制器要求第一帧必须贴近实测角, 否则拒收
        # (firstFrameJumpRejected)。对齐期间机器人平滑过渡到新轨迹起点。
        start_frame = engine.prime_start_frame()
        self._controller.push_frame(start_frame)

        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self._controller.advance_lead_in():
                break
            if self._stop_requested:
                self._stop_playback(silent=True)
                return
            if self._controller.failed:
                raise ControllerError(self._controller.last_error or "入控失败")
            time.sleep(TICK_MS / 1000.0)

        if not self._controller.playback_active:
            if self._stop_requested:
                self._stop_playback(silent=True)
                return
            if self._controller.failed:
                raise ControllerError(self._controller.last_error or "入控失败")
            raise ControllerError("起点对齐超时")

        engine.start()
        self._run_engine(engine)

        # 播完, 继续下一个
        if not self._stop_requested:
            self._play_next_in_queue()

    def _origin_pose(self) -> JointState:
        """当前实测姿态, 作为轨迹的出发姿态。

        轨迹不落盘, 每次都从机器人当前实际位置出发, 避免把机器人拽到某个
        固定姿态上去。
        """
        state = JointState.zero()
        left = self._controller._left_actual
        right = self._controller._right_actual
        if len(left) >= JOINT_COUNT:
            state.left = list(left[:JOINT_COUNT])
        if len(right) >= JOINT_COUNT:
            state.right = list(right[:JOINT_COUNT])
        return state

    def _on_frame(self, t: float, state: JointState) -> None:
        """播放引擎送来一帧目标。"""
        self._controller.push_frame(state)

    def _on_playback_finished(self) -> None:
        self._broadcast({"type": "finished"})

    def stop(self) -> Dict[str, Any]:
        """停止播放并交还通道。

        **返回时播放线程已经退出** —— 这是 API 契约的一部分。
        不等的话, 调用方紧接着下发新指令会撞上「已有动作正在播放」(409),
        因为播放线程要等到下一次 tick (最长 16ms) 才看到停止标志。
        """
        with self._lock:
            thread = self._play_thread
        was_playing = thread is not None and thread.is_alive()

        self._stop_requested = True
        self._stop_playback(silent=True)

        # 等播放线程真的退出 (不持锁, 免得和线程里的加锁操作互等)
        if was_playing and thread is not None:
            thread.join(timeout=3.0)
            if thread.is_alive():  # pragma: no cover - 极端情况
                logger.warning("[服务] 播放线程未在 3s 内退出")

        return {
            "ok": True,
            "phase": Phase.IDLE,
            "message": "已停止" if was_playing else "当前没有动作在播放",
        }

    def _stop_playback(self, silent: bool = False) -> None:
        """停掉时间轴并停控交还通道。

        队列也一并清空 —— 停了就意味着后面的都不播了。
        """
        with self._lock:
            engine = self._engine
            self._engine = None
            self._current_action = None
            # 清空队列
            self._queue = []
            self._queue_all = []
            self._queue_duration = 0.0
            self._queue_started = False

        if engine is not None:
            try:
                engine.stop()
            except Exception:
                pass

        try:
            self._controller.stop_control()
        except Exception as exc:  # pragma: no cover
            logger.warning("[服务] 停控失败: %s", exc)

        if not silent:
            self._broadcast({"type": "state", "phase": Phase.IDLE, "detail": "已停止"})

    # ------------------------------------------------------------ 状态

    def status(self) -> Dict[str, Any]:
        with self._lock:
            engine = self._engine
            action = self._current_action
            duration = self._current_duration
            queue = list(self._queue)
            queue_all = list(self._queue_all)
            queue_duration = self._queue_duration

        current_time = engine.current_time if engine is not None else 0.0
        total = engine.total_duration if engine is not None else duration
        progress = (current_time / total) if total > 0 else 0.0

        return {
            "phase": self._controller.phase,
            "controller_connected": self._conn.connected,
            "streaming": self._controller.streaming,
            "playback_active": self._controller.playback_active,
            "action": action,
            "current_time": round(current_time, 3),
            "total_duration": round(total, 3),
            "progress": round(min(1.0, max(0.0, progress)), 4),
            "left_arm": self._controller.left_arm_name,
            "right_arm": self._controller.right_arm_name,
            "last_error": self._controller.last_error,
            # 最近一次安全门阀检查结果 (没查过就是 None)
            "safety": self._last_safety.to_dict() if self._last_safety else None,
            # 队列状态: queue 是还没播的, queue_all 是整个队列
            "queue": queue,
            "queue_total": len(queue_all),
            "queue_duration": round(queue_duration, 3),
        }

    def health(self) -> Dict[str, Any]:
        return {
            "status": "ok",
            "controller_connected": self._conn.connected,
            "controller_ws": self.arm_cfg.controller_ws,
            "actions": self.library.count,
            "phase": self._controller.phase,
            "streaming": self._controller.streaming,
            "safety_enabled": self.cfg.safety.enabled,
        }


def _arm_of(robot_name: str) -> str:
    """判断机器人名属于左臂还是右臂。

    优先用 controller/list 得到的真实名做精确匹配, 再按名称关键字判断。
    """
    if not robot_name:
        return ""
    lower = robot_name.lower()
    if "left" in lower or "_l" in lower or lower.endswith("l"):
        return "left"
    if "right" in lower or "_r" in lower or lower.endswith("r"):
        return "right"
    return ""
