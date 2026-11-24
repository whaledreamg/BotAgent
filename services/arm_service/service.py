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
import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from config import Config
from joint_mover import JointSpaceMover
from library import ActionLibrary, ActionNotFound, ActionUnit
from mover import CartesianMover
from notifier import ArmNotifier
from robot.connection import RobotConnection
from robot.controller import ArmController, ControllerError, Phase
from safety import SafetyCheck, SafetyGate, SafetyReport
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

    **在 ``async def`` 里要用这个**, 不要用 :func:`run_blocking` ——
    后者是同步的, ``await`` 它会报
    "object dict can't be used in 'await' expression"。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, lambda: fn(*args, **kwargs))


def run_blocking(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """在后台线程里跑一个阻塞函数, 并等它返回。

    用于本来就在后台线程 (播放线程) 里的场景 —— 那里阻塞是允许的,
    但仍要保证不会卡住事件循环。

    **这是同步函数** —— 在 ``async def`` 里要 await 请用 :func:`await_run`。
    """
    future = _EXECUTOR.submit(fn, *args, **kwargs)
    return future.result()


class ArmService:
    """机械臂动作服务。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.arm_cfg = cfg.arm

        # 控制指令总闸的**运行时**覆盖。
        #
        # None = 没被显式设置过 -> 用配置里的值 (configs/arm.yaml 的
        # ``arm.enabled``)。运行时可以改 (POST /v1/control_enabled),
        # 不用重启服务 —— 调试抓取时特别方便: 先关掉把位置算准,
        # 再打开让机械臂真的过去。
        self._control_override: Optional[bool] = None

        self.library = ActionLibrary(
            self.arm_cfg.action_path,
            exclude_files=self.arm_cfg.exclude_files,
        )

        self._conn = RobotConnection(
            self.arm_cfg.controller_ws,
            timeout=self.arm_cfg.connect_timeout,
            on_state=self._on_robot_state,
            on_event=self._on_controller_event,
            on_reconnect=self._on_reconnected,
        )

        self._controller = ArmController(
            self._conn,
            frequency=self.arm_cfg.frequency,
            command_port=self.arm_cfg.command_port,
            lead_in_ms=self.arm_cfg.lead_in_ms,
            joint_limit=self.arm_cfg.joint_limit.to_config(),
            idle_stop_ms=self.arm_cfg.idle_stop_ms,
            hand_enabled=self.arm_cfg.hand.enabled,
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
        #: 笛卡尔移动执行器 (POST /v1/move_to)
        self._mover = CartesianMover(self)
        #: 关节流移动执行器 —— 本地逆解 + 关节流, 不走控制器逆解
        self._joint_mover = JointSpaceMover(self)
        #: 各臂的机型模板 (robot/list 里的 subType)
        #:
        #: **它决定入控位姿在哪个坐标系里解释** —— API 说明 28965:
        #: ``HM5-54-*`` / ``DA5-54-*`` 的位姿在**躯干系**, 末端是该机型
        #: 数值模型的第 7 关节坐标系, 与 ``robot/status.cartesian``
        #: (基座系 + 工具) **不是同一个系**; ``NDA-06-*`` 才是基座系法兰。
        self._arm_sub_types: Dict[str, str] = {}
        #: 本地运动学 (pinocchio) —— 客户端自己做逆解, 不用控制器的逆解接口
        #:
        #: ``{臂名: ArmKinematics}``。懒加载 —— pinocchio 不在时整个功能
        #: 降级 (但不崩), 控制器的关节流播放等老功能照常。
        self._kinematics: Dict[str, Any] = {}
        self._kinematics_lock = threading.Lock()
        #: 后台重连线程 (控制器启动时连不上时用)
        self._reconnect_thread: Optional[threading.Thread] = None

        # ---- 运动学快照 (手眼标定用) ----
        #
        # 标定时需要「工具在世界系下的位姿」来当量尺, 控制器给的是
        # `robot/status.cartesian` (世界系) + `baseFrame` (臂基座在世界系下
        # 的位姿) + `baseFrameKind` (静态/随父机动态派生)。
        #
        # 这些数据之前只存在于状态推送里, 没被存下来 —— 标定脚本没法用。
        # 这里把每条臂的最近一次状态存一份, 通过 /v1/kinematics 暴露出去。
        self._kin: Dict[str, Dict[str, Any]] = {}
        self._kin_lock = threading.Lock()
        #: 升降柱状态 (相机装在升降柱顶端时要做动态补偿)
        self._column: Optional[Dict[str, Any]] = None

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
            backend=cfg.notifier.backend,
            device=cfg.notifier.device,
        )

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """启动服务: 加载动作库, 尝试连接控制器。

        控制器连不上不阻止服务启动 —— /health 会反映 controller_connected=false,
        这样没有真机时也能验证动作库与协议逻辑。

        **连不上也会一直重试** —— 之前只试一次, 控制器晚启动 (或者
        开机时还没上电) 就永远连不上了, 得手动重启服务。
        """
        self.library.load()

        self._started = True
        # 连接是阻塞调用 (握手要等控制器的 HTTP 响应), 不能直接放在 FastAPI 的
        # 事件循环线程里 —— 那样会把整个服务卡住直到超时。丢到线程池里做。
        connected = await await_run(self._conn.connect)
        if connected:
            self._on_connected()
        else:
            logger.warning("[服务] 控制器不可达 (%s), 服务以降级模式运行, "
                           "后台会持续重连", self.arm_cfg.controller_ws)
            # 后台重连 —— 控制器起来后自动接上, 不用重启服务
            self._start_reconnect_worker()

    def _start_reconnect_worker(self) -> None:
        """起一个后台线程, 一直尝试连上控制器。"""
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return

        def worker() -> None:
            backoff = 2.0
            while self._started and not self._conn.connected:
                time.sleep(backoff)
                if not self._started or self._conn.connected:
                    return
                try:
                    if self._conn.connect():
                        logger.info("[服务] 控制器已连上")
                        self._on_connected()
                        return
                except Exception as exc:
                    logger.warning("[服务] 重连控制器失败: %s", exc)
                backoff = min(backoff * 2, 30.0)

        self._reconnect_thread = threading.Thread(
            target=worker, daemon=True, name="arm-reconnect"
        )
        self._reconnect_thread.start()

    def shutdown(self) -> None:
        self._started = False
        self._stop_playback(silent=True)
        self._controller.shutdown()
        self._conn.disconnect()
        # 重连线程靠 _started 标志退出, 这里不用 join (它是 daemon)
        self._reconnect_thread = None

    # ------------------------------------------------------------ 控制器事件

    def _on_connected(self) -> None:
        """连上控制器: 拉机器人清单以识别左右臂。"""
        self._conn.list_robots()
        # 机型模板 (``subType``) 只在 ``device/robots`` 里有 ——
        # ``robot/list`` 不带。它决定入控位姿在哪个系里解释, 必须拿到。
        self._conn.send_command("device/robots", {})

    def _on_reconnected(self) -> None:
        """重连成功后: 重新订阅状态推送 + 重新拉机器人清单。

        **为什么必须重新订阅**: 状态推送 (``robotState/sub``) 是**按连接**
        建立的 —— 新连接上并没有订阅, 不重新订阅就收不到状态,
        ``cartesian`` / 关节角会一直是旧值。

        **为什么还要清空运动学快照**: 断连期间的位姿是过期的, 留着会
        让标定脚本拿到「看起来正常其实是陈旧」的数据。清掉之后,
        新连接上的第一次推送会重新填上。
        """
        logger.info("[服务] 控制器已重连, 重新订阅状态推送")
        with self._kin_lock:
            self._kin.clear()
        self._conn.list_robots()

    def _on_robot_state(self, state: Dict[str, Any]) -> None:
        """收到状态推送: 提取关节角喂给控制器。"""
        name = str(state.get("name", ""))
        # 使能状态要先记下来: 未上使能时入控会被拒 (24077), 播放前要补使能。
        if "enabled" in state and name:
            self._controller.set_arm_enabled(name, bool(state.get("enabled")))

        # 运动学快照: 状态推送里带 cartesian / baseFrame, 标定要用
        if name:
            self._record_kinematics(name, state)

        joints = state.get("joints")
        if not isinstance(joints, list) or len(joints) < JOINT_COUNT:
            return
        try:
            values = [float(v) for v in joints[:JOINT_COUNT]]
        except (TypeError, ValueError):
            return
        self._controller.set_actual_joints(str(name), values)

    def _record_kinematics(self, name: str, payload: Dict[str, Any]) -> None:
        """记一条臂的运动学快照。

        只挑标定用得上的字段, 避免把整包状态推存下来 (推送很频繁)。

        - ``joints``         实测关节角 (rad)
        - ``cartesian``      TCP 位姿, **世界系** {x,y,z,a,b,c}
        - ``baseFrame``      臂基座在世界系下的位姿 {x,y,z,a,b,c}
        - ``baseFrameKind``  ``static`` / ``derived``
        """
        entry: Dict[str, Any] = {}

        joints = payload.get("joints")
        if isinstance(joints, list):
            try:
                entry["joints"] = [float(v) for v in joints]
            except (TypeError, ValueError):
                pass

        cart = payload.get("cartesian")
        if isinstance(cart, dict):
            entry["cartesian"] = {
                k: float(cart.get(k, 0.0) or 0.0)
                for k in ("x", "y", "z", "a", "b", "c")
            }
            # 七轴臂的臂型角 (rad), 有就带上
            if "e" in cart:
                try:
                    entry["cartesian"]["e"] = float(cart.get("e"))
                except (TypeError, ValueError):
                    pass

        base = payload.get("baseFrame")
        if isinstance(base, dict):
            entry["base_frame"] = {
                k: float(base.get(k, 0.0) or 0.0)
                for k in ("x", "y", "z", "a", "b", "c")
            }

        kind = payload.get("baseFrameKind")
        if kind:
            entry["base_frame_kind"] = str(kind)

        # 头/相机安装座在世界系下的位姿 (头会动时, 视觉服务做补偿要用)。
        #
        # **控制器的字段名目前没确认过** —— 所以这里把几种可能的键名都认了,
        # 拿到哪个用哪个, 一个都没有就不给 (而不是编一个)。
        for key in ("headFrame", "headBaseFrame", "headFramePose"):
            head = payload.get(key)
            if isinstance(head, dict):
                entry["head_frame"] = {
                    k: float(head.get(k, 0.0) or 0.0)
                    for k in ("x", "y", "z", "a", "b", "c")
                }
                hk = payload.get(f"{key}Kind")
                if hk:
                    entry["head_frame_kind"] = str(hk)
                break

        for key in ("enabled", "mode", "faulted"):
            if key in payload:
                entry[key] = payload.get(key)

        if not entry:
            return

        with self._kin_lock:
            existing = self._kin.get(name, {})
            existing.update(entry)
            existing["timestamp_ms"] = int(time.monotonic() * 1000)
            self._kin[name] = existing

    def _record_column(self, payload: Dict[str, Any]) -> None:
        """记升降柱状态 (相机装在升降柱顶端时做动态补偿要用)。"""
        entry: Dict[str, Any] = {}
        for key in ("name", "online", "enabled", "fault", "moving", "targetReached"):
            if key in payload:
                entry[key] = payload.get(key)
        if "height" in payload:
            try:
                entry["height"] = float(payload.get("height"))
            except (TypeError, ValueError):
                pass
        if "errorCode" in payload:
            try:
                entry["error_code"] = int(payload.get("errorCode"))
            except (TypeError, ValueError):
                pass
        if not entry:
            return
        with self._kin_lock:
            entry["timestamp_ms"] = int(time.monotonic() * 1000)
            self._column = entry

    def _on_controller_event(
        self, action: str, response: Dict[str, Any], meta: Optional[Dict[str, Any]] = None
    ) -> None:
        """收到控制器的请求-响应。

        ``meta`` 是发出这个请求时记下的上下文 (见 ``RobotConnection``),
        用来解决「应答里没有 name 字段, 分不清是哪条臂的」。
        """
        db = response.get("db")
        if not isinstance(db, (dict, list)):
            db = {}

        if action == "robot/list":
            self._handle_robot_list(db)
        elif action == "device/robots":
            self._handle_device_robots(db)
        elif action == "robot/status":
            self._handle_robot_status(db, meta or {})
        elif action == "realtime/getStatus":
            self._handle_realtime_status(db, response)
        elif action == "realtime/startControl":
            self._handle_start_control(db, response)
        elif action == "liftingColumn/getState":
            self._handle_column_state(db)
        elif action == "liftingColumn/list":
            self._handle_column_list(db)

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

        # 原始内容记一条, 排查「机型模板拿不到」时用
        logger.info("[服务] robot/list 原始: %s", json.dumps(db, ensure_ascii=False)[:2000])

        left = ""
        right = ""
        unnamed: List[str] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            if not name:
                continue
            # 记下机型模板 —— 它决定入控位姿在哪个坐标系里解释
            # (见 API 说明 28965: HM5/DA5 是躯干系, NDA-06 是基座系)
            sub_type = item.get("subType") or item.get("subtype") or ""
            if sub_type:
                self._arm_sub_types[name] = str(sub_type)
                logger.info("[服务] %s 机型模板 = %s", name, sub_type)
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
        """查询某臂状态。

        **臂名记在请求的 meta 里** —— 应答回来时按消息 id 带回,
        这样即使应答本身没有 name 字段, 也能确定是哪条臂的数据。

        (之前用 ``self._status_query_arm`` 这个单值槽位, 两条臂的
        查询会互相覆盖, 导致右臂的槽位里装上左臂的数据。)
        """
        self._conn.send_command("robot/status", {"name": name}, meta={"arm": name})

    def _handle_device_robots(self, db: Any) -> None:
        """处理 ``device/robots`` 应答 —— 取各臂的机型模板。

        **为什么必须拿**: ``robot/list`` 不带 ``subType``, 只有这里有。
        而机型模板决定**入控位姿在哪个坐标系里解释** (API 说明 28965):

        - ``HM5-54-*`` / ``DA5-54-*``: 位姿在**躯干系**, 末端是该机型
          数值模型的第 7 关节坐标系 —— 与 ``robot/status.cartesian``
          (基座系 + 工具偏置) **不是同一个系**, 两套位姿不可互相代入。
        - ``NDA-06-*``: 位姿在**该臂基座系**, 末端是法兰, 与
          ``robot/status.cartesian`` 同系同点。

        搞错的话, ``_flange_target`` 算出来的「工具偏置」是垃圾。
        """
        items: List[Any] = []
        if isinstance(db, list):
            items = db
        elif isinstance(db, dict):
            raw = db.get("items")
            if isinstance(raw, list):
                items = raw

        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            sub_type = str(item.get("subType") or "")
            if not name or not sub_type:
                continue
            self._arm_sub_types[name] = sub_type
            logger.info("[服务] %s 机型模板 = %s", name, sub_type)

    def _handle_robot_status(
        self, db: Any, meta: Optional[Dict[str, Any]] = None
    ) -> None:
        if not isinstance(db, dict):
            return
        # ---- 这是哪条臂的? ----
        #
        # ``robot/status`` 的应答里**没有** name 字段 (实测), 所以必须靠
        # 「发出去的时候就知道是哪条臂」来关联 —— 见 ``_query_robot_status``:
        # 请求的 meta 里带着臂名, 应答按消息 id 原样带回来。
        #
        # **不要用 ``self._status_query_arm`` 那种单值槽位** —— 那是之前
        # 的做法, 两条臂的查询会互相覆盖: 后到的应答把先到的顶掉,
        # 于是右臂的槽位里装上了左臂的数据。现场表现是基座系坐标整个
        # 翻转 (误差可达 0.88m), 逆解必然失败。
        name = str((meta or {}).get("arm") or db.get("name") or "")
        if not name:
            # 兜底: 实在认不出来就**不写** —— 宁可少一条快照,
            # 也不能把一条臂的数据写到另一条臂名下。
            logger.warning(
                "[服务] robot/status 应答认不出是哪条臂 (meta=%s, db.name=%s), 丢弃",
                (meta or {}).get("arm"), db.get("name"),
            )
            return

        # 使能状态要先记下来: 未上使能时入控会被拒 (24077), 播放前要补使能。
        if "enabled" in db and name:
            self._controller.set_arm_enabled(name, bool(db.get("enabled")))

        # 运动学快照 (标定要用)
        if name:
            self._record_kinematics(name, db)

        joints = db.get("joints")
        if not isinstance(joints, list) or len(joints) < JOINT_COUNT:
            return
        try:
            values = [float(v) for v in joints[:JOINT_COUNT]]
        except (TypeError, ValueError):
            return
        self._controller.set_actual_joints(name, values)

    def _handle_column_list(self, db: Any) -> None:
        """处理 liftingColumn/list 的应答。

        升降柱是独立设备, 名字 (比如 "Column") 要记下来, 后面查状态要用。
        """
        items: List[Any] = []
        if isinstance(db, list):
            items = db
        elif isinstance(db, dict):
            raw = db.get("items")
            if isinstance(raw, list):
                items = raw
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            with self._kin_lock:
                column = self._column or {}
                column.setdefault("name", name)
                column["online"] = bool(item.get("online", True))
                self._column = column
            break

    def _handle_column_state(self, db: Any) -> None:
        """处理 liftingColumn/getState 的应答。"""
        if isinstance(db, dict):
            self._record_column(db)

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
        # 笛卡尔移动在等这个应答 (它自己判成败并播报), 无论成败都先转给它
        #
        # **两条路径都要转** —— 关节流移动 (JointSpaceMover) 也在等入控
        # 应答, 漏了它就会一直等到超时。
        if self._joint_mover.busy:
            self._joint_mover.on_start_control(db, response)
            if "err" in response:
                err = response.get("err") or {}
                logger.warning(
                    "[服务] 关节流入控失败 (%s): %s", err.get("code", "?"), err.get("message", "")
                )
            else:
                logger.info("[服务] 关节流入控成功")
            return
        if self._mover.busy:
            self._mover.on_start_control(db, response)
            if "err" in response:
                err = response.get("err") or {}
                logger.warning(
                    "[服务] 末端入控失败 (%s): %s", err.get("code", "?"), err.get("message", "")
                )
            else:
                logger.info("[服务] 末端入控成功")
            return

        if "err" in response:
            err = response.get("err") or {}
            code = err.get("code", "?")
            message = err.get("message", "未知错误")
            # 汇总错误 (24077/24078) 里藏着每条臂自己的错误码与原因,
            # 只有一个汇总 message 是查不出根因的 —— 这里把明细打出来。
            if isinstance(db, dict):
                for item in db.get("results") or []:
                    if not isinstance(item, dict):
                        continue
                    item_err = item.get("err") or {}
                    logger.warning(
                        "[服务] 入控失败明细: %s ok=%s code=%s message=%s",
                        item.get("robot"),
                        item.get("ok"),
                        item_err.get("code", "?"),
                        item_err.get("message", "无"),
                    )
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
        wait: bool = False,
    ) -> Dict[str, Any]:
        """下发播放 (单个动作)。立即返回, 实际播放在后台线程进行。

        ``wait=True`` 时等播完再返回 (抓取流程要按顺序走)。
        """
        return self.play_many(
            [action_name], speed=speed, loop=loop, wait=wait
        )

    def play_many(
        self,
        action_names: List[str],
        speed: float = 1.0,
        loop: bool = False,
        wait: bool = False,
    ) -> Dict[str, Any]:
        """下发播放 (动作队列)。立即返回, 实际播放在后台线程进行。

        **整个队列只入控一次** —— 动作之间不松手, 机械臂运动连贯,
        也省掉每个动作重新入控的 700ms S 曲线对齐开销。

        每个动作播完时重新取一次实测关节角作为下一个动作的起点 ——
        轨迹不落盘起始姿态, 避免把机器人拽到某个固定姿态上去。

        Args:
            wait: **等播完再返回**。抓取流程要按顺序走 (回准备点 ->
                  拍照 -> 移到上方点 -> ...), 下一步必须等上一步真的
                  动完了才能开始。默认 False = 立即返回。
        """
        names = [str(x).strip() for x in (action_names or []) if str(x).strip()]
        if not names:
            raise ActionNotFound("动作名为空")

        # ---- 控制指令总闸 ----
        #
        # 与 mover.move_to 同一条闸: 关着就一个控制指令都不下发。
        # 动作库播放也是**会动**的操作, 不能绕过。
        if not self.control_enabled:
            logger.warning("[服务] 控制指令总闸已关闭: 拒绝播放 %s", ", ".join(names))
            raise ControllerError("机械臂控制已关闭，机械臂没有动")

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
        result = {
            "ok": True,
            "action": units[0].label,
            "key": units[0].key,
            "duration": round(total_duration, 3),
            "actions": [u.label for u in units],
            "phase": Phase.ENTERING,
            "message": "",
        }

        # ---- 等播完 (可选) ----
        #
        # 抓取流程要**按顺序**走: 回准备点 -> 拍照 -> 移到上方点 -> ...
        # 下一步必须等上一步真的动完了才能开始, 所以这里提供「等播完」
        # 的选项。
        if wait:
            result.update(self._wait_for_playback(total_duration))
        return result

    def _wait_for_playback(self, expected_duration: float) -> Dict[str, Any]:
        """等当前播放队列播完。

        超时时间按动作总时长算, 再留点余量 —— 动作库里的 ``duration``
        只是轨迹长度, 实际还要加上入控 + 700ms 对齐的时间。
        """
        timeout = max(5.0, float(expected_duration) * 2.0 + 5.0)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                thread = self._play_thread
            if thread is None or not thread.is_alive():
                # 播完了 (或者一开始就失败了)
                err = self._controller.last_error
                if err:
                    return {
                        "finished": False,
                        "error": err,
                        "phase": self._controller.phase,
                    }
                return {
                    "finished": True,
                    "error": "",
                    "phase": Phase.IDLE,
                }
            time.sleep(0.05)

        logger.warning("[服务] 等播放超时 (%.1fs)", timeout)
        return {
            "finished": False,
            "error": f"等待动作播完超时 ({timeout:.0f} 秒)",
            "phase": self._controller.phase,
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

        另外检查**整机操作模式** (``device/getOpsMode``): 手动模式下
        控制器会拒绝外部实时接口入控 (10074 「未开启远程模式」)。
        这项是整机属性, 不分臂, 所以在臂的循环外面查。
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

        # ---- 整机操作模式 ----
        #
        # 手动模式下手动的限速更严, 而且外部实时接口根本入不了控
        # (10074)。提前查一次, 别等入控失败才报一个天书般的错。
        #
        # 查不到 (None) 一律放行 —— 这个接口是「锦上添花」的检查,
        # 控制器版本不支持时不该把整个动作链路堵死。
        ops_mode = self._conn.get_ops_mode() if self.cfg.safety.require_auto_mode else "auto"
        ops_ok = ops_mode != "manual"
        all_checks.append(
            SafetyCheck(
                code="ops_mode",
                ok=ops_ok,
                message="控制器处于手动模式, 请切到自动模式" if not ops_ok else "",
                detail=f"opsMode={ops_mode or 'unknown'}",
            )
        )
        if not ops_ok:
            report.ok = False
            report.code = "ops_mode"
            report.reason = "控制器处于手动模式, 请切到自动模式"

        for arm_name in arms:
            status = self._fetch_status(arm_name)
            sub = self._gate.check(status, connected=self._conn.connected)
            # 把臂名带进每项检查, 便于定位是哪条臂的问题
            for c in sub.checks:
                c.detail = f"[{arm_name}] {c.detail}".strip()
            all_checks.extend(sub.checks)
            if not sub.ok and report.ok:
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
        self._speak_text(text)

    def _speak_text(self, text: str) -> None:
        """后台线程播报一段话 (移动失败、不可达等)。"""
        if not text or not self.cfg.safety.speak_on_reject:
            return

        def _worker() -> None:
            try:
                self._notifier.speak(text)
            except Exception as exc:  # pragma: no cover - 播报失败不该影响主流程
                logger.warning("[播报] 失败: %s", exc)

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
        self._controller.push_frame(
            start_frame,
            engine.evaluate_hand_at(0.0, "left"),
            engine.evaluate_hand_at(0.0, "right"),
        )

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
        self._controller.push_frame(
            start_frame,
            engine.evaluate_hand_at(0.0, "left"),
            engine.evaluate_hand_at(0.0, "right"),
        )

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
        """播放引擎送来一帧目标。

        手部值**同时**送过去 —— 手部片段不产生关节插值, 但手部指令
        跟关节指令在同一个 UDP 包里发, 所以得在同一帧里一起给。
        """
        engine = self._engine
        left_hand: Optional[List[int]] = None
        right_hand: Optional[List[int]] = None
        if engine is not None:
            left_hand = engine.evaluate_hand_at(t, "left")
            right_hand = engine.evaluate_hand_at(t, "right")
        self._controller.push_frame(state, left_hand, right_hand)

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
        # 正在做笛卡尔移动时, 先打断它 (它自己会停流并 stopControl)
        self._mover.abort()
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
            # 控制指令总闸: false 时不下发任何控制指令 (只算不动)
            "control_enabled": bool(self.control_enabled),
        }

    # ------------------------------------------------------------ 控制总闸

    @property
    def control_enabled(self) -> bool:
        """当前是否允许下发控制指令。

        运行时覆盖优先于配置 (``configs/arm.yaml`` 的 ``arm.enabled``);
        没覆盖过时用配置值。

        **false = 只算不动** —— 抓取的位置照算照报, 但一个控制指令都不发。
        """
        if self._control_override is not None:
            return bool(self._control_override)
        return bool(self.arm_cfg.enabled)

    def set_control_enabled(self, value: Optional[bool]) -> bool:
        """设置控制指令总闸。``None`` = 回到配置里的默认值。

        Returns: 生效后的状态。
        """
        if value is None:
            self._control_override = None
            logger.info("[服务] 控制指令总闸回到配置默认值")
        else:
            self._control_override = bool(value)
            logger.info(
                "[服务] 控制指令总闸切到 %s",
                "开启 (会动)" if value else "关闭 (只算不动)",
            )
        return self.control_enabled

    # ------------------------------------------------------------ 本地运动学

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
        """笛卡尔移动的统一入口 —— 按配置选实现。

        ``arm.move_mode``:

        - ``local_ik`` (缺省) —— **本地逆解 + 关节流**。用 pinocchio 在
          本地把目标位姿解成关节角, 控制器 ``cartesianIkPath="none"``
          全程不做末端逆解。**这是推荐路径**。
        - ``controller_ik`` —— 走控制器的 ``cartesianIkPath=analytic``。
          保留是为了对照/排障, 不是推荐。

        两条路的**控制指令总闸**与**安全门阀**都在各自实现里生效, 这里
        不重复检查 —— 免得两处逻辑走偏。
        """
        mode = (self.arm_cfg.move_mode or "local_ik").strip().lower()
        if mode == "controller_ik":
            return self._mover.move_to(
                arm, x, y, z, a, b, c, duration, speak
            )
        return self._joint_mover.move_to(
            arm, x, y, z, a, b, c, duration, speak
        )

    def kinematics_for(self, arm: str) -> Any:
        """拿某条臂的本地运动学对象 (pinocchio)。拿不到返回 ``None``。

        **懒加载** —— 只有真的要本地逆解时才建模型 (建模型要解析 URDF,
        几百毫秒)。pinocchio 不可用时返回 ``None``, 上层如实报「本地
        运动学不可用」, 而不是崩掉。
        """
        if not arm:
            return None
        with self._kinematics_lock:
            kin = self._kinematics.get(arm)
            if kin is not None:
                return kin
        try:
            from robot.kinematics import (
                ArmKinematics,
                KINEMATICS_AVAILABLE,
            )
        except Exception as exc:
            logger.warning("[运动学] 导入失败: %s", exc)
            return None
        if not KINEMATICS_AVAILABLE:
            logger.warning(
                "[运动学] pinocchio 不可用 —— 本地逆解已禁用。"
                "装: pip install numpy pin"
            )
            return None
        try:
            kin = ArmKinematics(arm)
        except Exception as exc:
            logger.error("[运动学] %s 加载 URDF 失败: %s", arm, exc)
            return None
        logger.info("[运动学] %s 本地运动学就绪 (URDF=%s)", arm, kin.urdf_path)
        with self._kinematics_lock:
            self._kinematics[arm] = kin
        return kin

    def sync_kinematics_base(self, arm: str, base_frame: Dict[str, float]) -> bool:
        """用控制器的 ``base_frame`` 同步本地运动学的基座位姿。

        **真机必须做这一步** —— URDF 自带的装法与实测 ``baseFrame`` 不是
        同一个数, 不同步的话 IK 目标整体偏。

        返回是否同步成功。
        """
        kin = self.kinematics_for(arm)
        if kin is None:
            return False
        try:
            return bool(kin.set_base_from_arm_frame_dict(base_frame))
        except Exception as exc:
            logger.warning("[运动学] %s 同步基座失败: %s", arm, exc)
            return False

    def _target_in_base(self, arm: str, world_pos: Sequence[float]) -> Tuple[float, float, float]:
        """目标世界系坐标 -> 该臂基座系坐标 (只算位置)。"""
        from robot.geometry import euler_to_quat, pose_from_xyzabc, world_to_base

        with self._kin_lock:
            data = dict(self._kin.get(arm) or {})
        bf = data.get("base_frame")
        if not bf:
            return tuple(world_pos)  # type: ignore[return-value]
        base = pose_from_xyzabc(bf)
        base_q = euler_to_quat(float(bf.get("a", 0.0)), float(bf.get("b", 0.0)),
                               float(bf.get("c", 0.0)))
        rel = world_to_base(base[0], base_q, tuple(world_pos), (1.0, 0.0, 0.0, 0.0))
        return tuple(rel[0])  # type: ignore[return-value]

    # ------------------------------------------------------------ 运动学

    @staticmethod
    def _detect_crossed_arms(arms: Dict[str, Dict[str, Any]]) -> str:
        """检查各臂的运动学数据有没有串台。返回问题描述 (空串 = 正常)。

        **背景**: ``_kin`` 按臂名分槽存数据。曾经有个 bug 是用单个
        变量 ``_status_query_arm`` 记住「这次查的是哪条臂」, 而
        ``robot/status`` 的应答里又没有 name 字段 —— 两条臂的应答
        互相覆盖, 右臂的槽位里装上了左臂的 TCP 和基座。

        **现场表现**: 基座系目标坐标整个翻转 (差 0.88m), 逆解失败,
        控制器报 ``numericalIkFailed`` 或直接安全停机。

        **判据**: 两条臂的 TCP 若几乎重合 —— 说明其中一个槽位里装的是
        对方的数据。两条臂分居基座两侧, 正常情况下它们的 TCP 不该
        重合 (除非机械臂真的摆到了同一处, 那极罕见)。

        另外还要看基座: 两条臂的 ``base_frame`` 正常应该**不同**
        (左右两侧, y 和绕 z 角都不一样)。如果两条臂的基座**完全
        一样**, 那更说明其中一条的数据是抄来的。
        """
        names = sorted(arms.keys())
        if len(names) < 2:
            return ""

        def _vec(d: Dict[str, Any], key: str):
            v = d.get(key)
            if not isinstance(v, dict):
                return None
            try:
                return tuple(float(v.get(k, 0.0) or 0.0) for k in ("x", "y", "z"))
            except (TypeError, ValueError):
                return None

        problems: List[str] = []
        for i, n1 in enumerate(names):
            for n2 in names[i + 1:]:
                d1, d2 = arms[n1], arms[n2]
                b1, b2 = _vec(d1, "base_frame"), _vec(d2, "base_frame")
                c1, c2 = _vec(d1, "cartesian"), _vec(d2, "cartesian")
                if not (b1 and b2 and c1 and c2):
                    continue
                # 基座差异 (它们在基座两侧, 应该差得开)
                base_gap = math.sqrt(sum((a - b) ** 2 for a, b in zip(b1, b2)))
                # TCP 差异
                tcp_gap = math.sqrt(sum((a - b) ** 2 for a, b in zip(c1, c2)))
                if tcp_gap < 0.05:
                    problems.append(
                        f"{n1} 与 {n2} 的 TCP 只差 {tcp_gap:.3f}m —— 位姿数据疑似串台"
                    )
                elif base_gap < 1e-6:
                    problems.append(
                        f"{n1} 与 {n2} 的基座位姿完全相同 (差 {base_gap:.4f}m) —— "
                        f"其中一条的数据是抄来的"
                    )
        return "; ".join(problems)

    def kinematics(self) -> Dict[str, Any]:
        """运动学快照 —— 手眼标定要用的位姿数据。

        每条臂给出:
        - ``joints``          实测关节角 (rad)
        - ``cartesian``       TCP 位姿, **世界系** {x,y,z,a,b,c}
        - ``base_frame``      臂基座在世界系下的位姿 {x,y,z,a,b,c}
        - ``base_frame_kind`` ``static`` / ``derived``

        **为什么不给笛卡尔积的世界位姿之外的东西**: 控制器
        `robot/status.cartesian` 就是世界系的 TCP 位姿, 这是标定需要的
        「量尺」。而 `base_frame` 用来把「臂基座系」和「世界系」连起来 ——
        相机装在躯干上时, 相机到臂基座的相对关系是固定的, 但臂基座到
        世界系会随升降柱/挂接安装变化。

        ``base_frame_kind == "derived"`` 说明臂基座是挂接在父机器人上的,
        基座本身在动 —— 这时静态外参不成立, 必须用这里的动态值。
        """
        with self._kin_lock:
            arms = {name: dict(data) for name, data in self._kin.items()}
            column = dict(self._column) if self._column else None
            # 头/相机安装座: 优先用任一条臂状态里带的 headFrame
            head_frame: Optional[Dict[str, float]] = None
            head_frame_kind = ""
            for data in arms.values():
                hf = data.get("head_frame")
                if isinstance(hf, dict):
                    head_frame = dict(hf)
                    head_frame_kind = str(data.get("head_frame_kind") or "")
                    break

        notes: List[str] = []
        if not arms:
            notes.append("还没有收到任何机器人的状态快照")
        else:
            missing = [
                name for name, data in arms.items()
                if "cartesian" not in data
            ]
            if missing:
                notes.append(
                    "这些臂还没有 cartesian (TCP 世界系位姿): " + ", ".join(missing)
                )
            derived = [
                name for name, data in arms.items()
                if data.get("base_frame_kind") == "derived"
            ]
            if derived:
                notes.append(
                    "这些臂是挂接安装 (base_frame_kind=derived), 基座在动, "
                    "静态外参不成立: " + ", ".join(derived)
                )

            # ---- 交叉校验: 别臂的数据串进来了 ----
            #
            # ``_kin`` 是按臂名分槽的。如果某条臂的槽位里装的其实是
            # **另一条臂**的数据 (之前 ``_status_query_arm`` 单值槽位的
            # 竞态就会造成这个), 那么基座系换算会整个算错, 逆解必然失败。
            #
            # 判据: 两条臂的 ``base_frame`` 应当**明显不同** (它们在基座
            # 两侧), 而它们的 TCP 若几乎重合, 就说明数据串了。
            crossed = self._detect_crossed_arms(arms)
            if crossed:
                notes.append(
                    "[数据串帧] " + crossed
                    + " —— 这条臂的位姿数据可能来自另一条臂, "
                    "用它算出来的目标点是错的, 不要用于运动"
                )

        if column is None:
            notes.append("没有升降柱状态 (控制器上可能没有配升降柱)")

        if head_frame is None:
            notes.append(
                "没有头/相机安装座位姿 (headFrame) —— 头会动的话需要控制器提供"
            )

        connected = self._conn.connected

        # ---- 陈旧检测 ----
        #
        # **这是标定翻车的头号原因**: 控制器断连后, 服务还在跑, 接口还返回
        # 200, 但 ``_kin`` 里的快照永远冻结在断连那一刻。之前 25 个采样的
        # ``T_world_tool`` 完全相同, 解算时被当成野值剔到只剩 1 个。
        #
        # 这里显式告诉调用方「这份数据是不是新鲜的」。
        stale = False
        age_ms = 0
        if not connected:
            stale = True
            notes.append(
                "**控制器已断开** —— 下面的位姿是断连前的陈旧快照, 不能用于标定"
            )
        elif arms:
            newest = max(
                (int(data.get("timestamp_ms") or 0) for data in arms.values()),
                default=0,
            )
            if newest:
                age_ms = int(time.monotonic() * 1000) - newest
                # 状态推送周期 50ms, 超过 2s 基本就是卡住了
                if age_ms > 2000:
                    stale = True
                    notes.append(
                        f"运动学快照已 {age_ms} ms 没更新 —— 数据可能是陈旧的"
                    )

        return {
            "ok": bool(arms) and not stale,
            "arms": arms,
            "lifting_column": column,
            "head_frame": head_frame,
            "head_frame_kind": head_frame_kind,
            #: 各臂的机型模板 —— 决定入控位姿在哪个系解释
            "arm_sub_types": dict(self._arm_sub_types),
            "controller_connected": connected,
            #: 数据是否是新鲜的 (False = 陈旧, 不能用于标定)
            "stale": stale,
            #: 快照距今多少毫秒
            "age_ms": age_ms,
            "timestamp_ms": int(time.monotonic() * 1000),
            "notes": notes,
        }

    def refresh_kinematics(self) -> Dict[str, Any]:
        """主动拉一次运动学数据并返回。

        标定脚本用这个: 每采一个位姿就调一次, 拿到的是**当下**的
        TCP 位姿 (状态推送有延迟, 推送周期 50ms, 快速运动时不够准)。

        **关键**: 必须等到**新**的数据回来, 不能只看「有没有数据」。
        之前这里只判断 ``if arms:`` —— 而 ``_kin`` 一直是满的 (状态推送
        在填), 所以每次只等 50ms 就返回**旧数据**。加上「时间戳变了
        才算收到」之后, 才真的拿到的是刚刚那一刻的位姿。
        """
        if not self._conn.connected:
            return self.kinematics()

        # 记下请求前的快照时间戳 —— 应答到了之后它必须变
        with self._kin_lock:
            before = {
                name: int(data.get("timestamp_ms") or 0)
                for name, data in self._kin.items()
            }

        # **必须走 _query_robot_status** —— 它把臂名放进请求的 meta,
        # 应答回来时靠 id 关联。直接调 ``get_robot_status`` 会丢掉这个
        # 关联, 应答就分不清是哪条臂的 (然后两条臂互相覆盖)。
        arms = self._controller.active_arms()
        for name in arms:
            self._query_robot_status(name)

        # 升降柱
        column = None
        with self._kin_lock:
            column = dict(self._column) if self._column else None
        if column and column.get("name"):
            self._conn.send_command(
                "liftingColumn/getState", {"device": column.get("name")}
            )
        elif not column:
            self._conn.send_command("liftingColumn/list", {})

        # 等应答回来 (在读线程里处理)
        #
        # **判据是「时间戳变了」, 不是「有数据」** —— 后者会立刻返回旧快照。
        #
        # **而且要等**所有**要查的臂都更新了** —— 之前用的是
        # ``any(...)`` (任意一条臂更新就返回), 于是「右臂拿到的是
        # 上一次陈旧的数据」这种问题完全查不出来。抓取要的就是
        # 每一条臂**当下**的位姿。
        deadline = time.time() + 0.6
        while time.time() < deadline:
            with self._kin_lock:
                now = {
                    name: int(data.get("timestamp_ms") or 0)
                    for name, data in self._kin.items()
                }
            if all(
                now.get(name, 0) > before.get(name, 0) for name in arms
            ):
                break
            time.sleep(0.02)

        return self.kinematics()


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
