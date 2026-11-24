"""机械臂动作服务配置加载。

配置来源优先级 (后者覆盖前者):
1. 本文件内的默认值
2. configs/arm.yaml
3. 环境变量 (ARM_* / CONTROLLER_*)

只依赖 PyYAML, 缺失时退化为内置默认值, 保证服务仍可启动。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

logger = logging.getLogger("arm.config")


#: 项目根目录 (Bot Agent/)
ROOT = Path(__file__).resolve().parents[2]
ARM_CONFIG_PATH = ROOT / "configs" / "arm.yaml"
#: 动作库默认目录 (services/arm_service/data)
DEFAULT_ACTION_DIR = Path(__file__).resolve().parent / "data"

DEFAULTS: Dict[str, Any] = {
    "arm": {
        # **控制指令总闸**。false = 不下发任何控制指令, 只算不动。
        # 详见 configs/arm.yaml 的说明。
        "enabled": True,
        # 机器人控制器的 WebSocket 控制面 (默认按 ActionPlanner 的机械臂地址)
        "controller_ws": "ws://192.168.100.16:9000",
        # 控制器实时指令面 (UDP), 端口以控制器 getStatus 回读为准
        "command_port": 9030,
        # 关节流下发频率 (Hz), 协议允许 10~1000
        "frequency": 100,
        # 入控后从实测角平滑过渡到轨迹起点的时长 (毫秒)
        "lead_in_ms": 700,
        # 左右臂在控制器里的真实名字; 留空则连接后用 robot/list 自动识别
        "left_arm": "",
        "right_arm": "",
        # 入控时开关节级限幅。协议缺省是 false (即没有任何关节级限速),
        # 必须显式打开, 否则一次错误的轨迹就能把手臂甩出去。
        "joint_limit": {
            "enable": True,
            "max_velocity": 1.5,       # rad/s
            "max_acceleration": 5.0,   # rad/s^2
        },
        # 动作库目录
        "action_dir": str(DEFAULT_ACTION_DIR),
        # 动作库中要跳过的文件名 (不含路径)。用于排除旧版「一文件装多动作」
        # 的大文件, 它们只是拆分前的素材, 不该被当成一个动作加载。
        "exclude_files": [],
        # 静止多久后暂停下发 (毫秒)。配合控制器的 holdOnIdle=true,
        # 会话保持 active, 目标变化即自动恢复。
        "idle_stop_ms": 1000,
        # 控制面连接超时 (秒)
        "connect_timeout": 5.0,
        # 单次动作调用的最长等待 (毫秒); 超时视为失败
        "play_timeout_ms": 15000,
        # 安全门阀: 动作执行前必须先检查状态
        "safety": {
            # 门阀总开关。关掉后不再检查 (不推荐 —— 会静默失败)
            "enabled": True,
            # 必须已上使能才允许动作
            "require_enabled": True,
            # 必须连上控制器才允许动作
            "require_connection": True,
            # 检查故障锁定 (faulted / faultCode)
            "check_fault": True,
            # 允许动作的运行模式。只有 CSPPassthrough (位置控制模式) 能接受
            # 实时位置流 —— 日常运行的机械臂就在这个模式。
            "allowed_modes": ["CSPPassthrough"],
            # 门阀检查的超时 (毫秒); 超时按「不通过」处理
            "timeout_ms": 3000,
            # 门阀拒绝时是否用 TTS 播报原因
            "speak_on_reject": True,
        },
        # 安全门阀拒绝时的 TTS 播报
        "notifier": {
            "enabled": True,
            "ws_url": "ws://127.0.0.1:8000/ws",
            # 音色名。留空 = 用 TTS 服务端的默认音色 (configs/tts.yaml)
            "voice": "",
            "speed": 5,
            "sample_rate": 24000,
            "timeout": 30.0,
            # 由 TTS 服务端自己播 (true) 还是 arm_service 本地播 (false)
            "server_play": True,
            # 本地播放后端。留空 = 自动选路 (PulseAudio -> ALSA -> sounddevice)
            "backend": "",
            # 本地播放设备。留空 = 自动挑 (优先板载模拟输出 = 接喇叭的那个口)
            "device": "",
        },
    },
    "server": {"host": "0.0.0.0", "port": 8802},
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class JointLimit:
    """入控时的关节级限幅。"""

    enable: bool = True
    max_velocity: float = 1.5
    max_acceleration: float = 5.0

    def to_config(self) -> Dict[str, Any]:
        return {
            "enable": bool(self.enable),
            "maxVelocity": float(self.max_velocity),
            "maxAcceleration": float(self.max_acceleration),
        }


@dataclass
class GraspConfig:
    """抓取移动 (/v1/move_to) 配置。"""

    #: 默认运动时长 (秒)
    duration: float = 3.0
    #: 工作空间粗检: 目标到臂基座的最大距离 (米)
    max_reach: float = 1.2
    #: 各臂示教的抓取姿态 a/b/c (rad), key 为 left / right。
    #:
    #: **空 dict = 不指定姿态** —— 移动时保持当前 TCP 姿态, 只改位置。
    #: 这是默认且推荐的做法: 姿态不变则关节角变化最小, 撞软限位的
    #: 风险最低。
    #:
    #: 填了三个值 = 强制转到那个姿态, 须先示教 (见 configs/arm.yaml)。
    orientation: Dict[str, Dict[str, float]] = field(default_factory=dict)


@dataclass
class HandConfig:
    """灵巧手 (夹爪) 配置。

    **enabled 默认 false** —— 控制器认不认 UDP 报文里的手部字段还没在
    真机上验证过 (``tools/probe_hand.py`` 就是干这个的)。没验证就发,
    万一格式不对, 控制器会**整包丢弃** (invalidDatagram) —— 那时候
    连手臂都不动了, 比不发手部指令糟得多。

    探针确认控制器接受之后, 把这个打开即可, 不用改代码。
    """

    #: 是否在报文里带手部字段
    enabled: bool = False


@dataclass
class GraspFlowConfig:
    """抓取流程 (5 步) 配置。

    流程: 回准备点 -> 拍照识别 -> 移到上方点 -> 移到抓取点 -> 回安全点。
    """

    #: 第 1 步: 回准备点的动作名。留空 = 跳过这一步。
    ready_action: str = "准备点"
    #: 第 5 步: 回安全点的动作名。留空 = 跳过这一步。
    safe_action: str = "安全点"
    #: 上方点相对目标点的高度 (米)。世界系 Z 向上。
    above_distance: float = 0.10
    #: 抓取点相对目标点的偏移 (米), 可分轴。
    grasp_offset_x: float = 0.0
    grasp_offset_y: float = 0.0
    grasp_offset_z: float = 0.0
    #: 闭合/张开夹爪的动作名。留空 = 不做这一步。
    close_action: str = "闭合夹爪"
    open_action: str = "张开夹爪"
    #: 移到上方点的运动时长 (秒)
    above_duration: float = 3.0
    #: 移到抓取点的运动时长 (秒)
    grasp_duration: float = 2.0


@dataclass
class ArmConfig:
    #: **控制指令总闸**。false = 不下发任何控制指令 (只算不动)。
    #:
    #: 这是机械臂的**最后一道**安全闸 —— 无论上层走哪条路 (抓取 skill /
    #: 动作库播放 / 笛卡尔移动), 这里关着就不会向控制器发控制指令。
    enabled: bool = True
    #: 机器人控制器的 WebSocket 控制面地址
    controller_ws: str = "ws://192.168.100.16:9000"
    #: 控制器实时指令面 (UDP) 端口
    command_port: int = 9030
    #: 关节流下发频率 (Hz)
    frequency: int = 100
    #: 入控后从实测角过渡到轨迹起点的时长 (毫秒)
    lead_in_ms: int = 700
    #: 左右臂在控制器里的真实名字; 留空则自动识别
    left_arm: str = ""
    right_arm: str = ""
    joint_limit: JointLimit = field(default_factory=JointLimit)
    #: 动作库目录
    action_dir: str = str(DEFAULT_ACTION_DIR)
    #: 要跳过的文件名 (旧版一文件多动作的大文件)
    exclude_files: List[str] = field(default_factory=list)
    #: 静止停发门限 (毫秒)
    idle_stop_ms: int = 1000
    connect_timeout: float = 5.0
    play_timeout_ms: int = 15000
    grasp: GraspConfig = field(default_factory=GraspConfig)
    #: 抓取流程 (5 步) 配置
    flow: GraspFlowConfig = field(default_factory=GraspFlowConfig)
    #: 灵巧手 (夹爪) 配置
    hand: HandConfig = field(default_factory=HandConfig)
    #: 笛卡尔移动走哪条路
    move_mode: str = "local_ik"

    @property
    def action_path(self) -> Path:
        """动作库目录。

        相对路径按项目根目录 (Bot Agent/) 解析 —— 服务可能从任意目录启动
        (start_arm.sh 会 cd 到服务目录), 用 cwd 解析会找不到数据文件。
        """
        path = Path(self.action_dir).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        return path


@dataclass
class SafetyConfig:
    """安全门阀配置。"""

    #: 门阀总开关
    enabled: bool = True
    #: 必须已上使能才允许动作
    require_enabled: bool = True
    #: 必须连上控制器才允许动作
    require_connection: bool = True
    #: 检查故障锁定 (faulted / faultCode)
    check_fault: bool = True
    #: 检查整机操作模式必须是 auto (手动模式控制器拒绝入控, 10074)
    require_auto_mode: bool = True
    #: 允许动作的运行模式 (只有 CSPPassthrough 能接受实时位置流)
    allowed_modes: List[str] = field(default_factory=lambda: ["CSPPassthrough"])
    #: 门阀检查超时 (毫秒); 超时按「不通过」处理
    timeout_ms: int = 3000
    #: 门阀拒绝时是否用 TTS 播报原因
    speak_on_reject: bool = True


@dataclass
class NotifierConfig:
    """安全门阀拒绝时的 TTS 播报配置。"""

    enabled: bool = True
    ws_url: str = "ws://127.0.0.1:8000/ws"
    voice: str = "Default"
    speed: int = 5
    sample_rate: int = 24000
    timeout: float = 30.0
    #: 由 TTS 服务端自己播 (True) 还是本地播 (False)。
    #: arm_service 的 conda 环境通常没有 sounddevice, 所以默认走服务端播。
    server_play: bool = True
    #: 本地播放后端。留空 = 自动选路 (PulseAudio -> ALSA -> sounddevice)。
    #: 可选值: auto / pulse / alsa / sounddevice
    backend: str = ""
    #: 本地播放设备。留空 = 自动挑 (优先板载模拟输出 = 接喇叭的那个口)。
    device: str = ""


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8802


@dataclass
class Config:
    arm: ArmConfig = field(default_factory=ArmConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    notifier: NotifierConfig = field(default_factory=NotifierConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


def load_config(path: Path = ARM_CONFIG_PATH) -> Config:
    data: Dict[str, Any] = {}
    if yaml is not None and path and Path(path).is_file():
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    merged = _deep_merge(DEFAULTS, data)

    a = merged.get("arm", {})
    jl = a.get("joint_limit") or {}
    gr = a.get("grasp") or {}

    def _abc(side: str) -> Dict[str, float]:
        """读一条示教姿态。

        **留空 = 不指定姿态** (返回空 dict) —— 移动时保持当前 TCP 姿态,
        只改位置。这是默认且推荐的做法。

        为什么不默认填 ``0,0,0``: 那是「姿态全零」而非「不指定」。
        机械臂当前姿态若是 (2.97, 0.74, -3.00), 硬转到全零要转 138 度,
        关节角大幅变化, 很容易撞软限位被控制器停掉。

        **三个值要么都给, 要么都不给** —— 给一半会变成「一半用示教值、
        一半用全零」, 那是用户没想要的姿态。
        """
        raw = gr.get(side) or {}
        if not isinstance(raw, dict):
            return {}

        # ---- 防呆: 位置写错成列表 ----
        #
        # ``right: {-1.485, -0.331, 0.260}`` 会被 YAML 解析成
        # ``{-1.485: null, -0.331: null, 0.26: null}`` —— 数字当键名。
        # 这时 ``raw.get("a")`` 全是 None, 下面会当成「没填」而**静默
        # 丢掉整条姿态**: 机械臂照常动, 但姿态不是你设的那个, 而且
        # 没有任何报错, 极难排查。
        #
        # 所以这里显式识别「键不是 a/b/c」的情况, 直接把用户写的原样
        # 报出来, 让他知道该改成三行写法。
        keys = ("a", "b", "c")
        stray = [k for k in raw if k not in keys]
        if stray:
            logger.error(
                "grasp.%s 姿态的键 %s 不是 a/b/c —— "
                "多半写成了 YAML 列表写法 (如 `right: {1.0, 2.0, 3.0}`), "
                "那样会被解析成数字键而整条丢弃。请改成三行: "
                "`%s:` 下面写 `a: ... / b: ... / c: ...`",
                side, ", ".join(str(x) for x in stray), side,
            )
            return {}

        present = [k for k in keys if raw.get(k) is not None]
        if not present:
            return {}
        if len(present) != len(keys):
            logger.warning(
                "grasp.%s 姿态只给了 %s, 缺的值没法猜 —— 整条按「不指定」处理",
                side, "/".join(present),
            )
            return {}
        try:
            return {k: float(raw[k]) for k in keys}
        except (TypeError, ValueError):
            logger.warning("grasp.%s 姿态不是数字 —— 整条按「不指定」处理", side)
            return {}

    grasp = GraspConfig(
        duration=float(gr.get("duration", 3.0)),
        max_reach=float(gr.get("max_reach", 1.2)),
        orientation={"left": _abc("left"), "right": _abc("right")},
    )

    # ---- 抓取流程 (5 步) ----
    fl = (gr.get("flow") or {})
    if not isinstance(fl, dict):
        fl = {}
    offset = (fl.get("grasp_offset") or {})
    if not isinstance(offset, dict):
        offset = {}
    durations = (fl.get("durations") or {})
    if not isinstance(durations, dict):
        durations = {}

    def _num(value: Any, fallback: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    flow = GraspFlowConfig(
        ready_action=str(fl.get("ready_action", "准备点") or ""),
        safe_action=str(fl.get("safe_action", "安全点") or ""),
        above_distance=_num(fl.get("above_distance"), 0.10),
        grasp_offset_x=_num(offset.get("x"), 0.0),
        grasp_offset_y=_num(offset.get("y"), 0.0),
        grasp_offset_z=_num(offset.get("z"), 0.0),
        close_action=str(fl.get("close_action", "闭合夹爪") or ""),
        open_action=str(fl.get("open_action", "张开夹爪") or ""),
        above_duration=_num(durations.get("above"), 3.0),
        grasp_duration=_num(durations.get("grasp"), 2.0),
    )

    # ---- 灵巧手 ----
    hd = (a.get("hand") or {})
    if not isinstance(hd, dict):
        hd = {}
    hand = HandConfig(
        enabled=bool(
            hd.get("enabled", os.environ.get("ARM_HAND_ENABLED", False))
        )
    )
    if hand.enabled:
        logger.info("灵巧手通道已启用 (报文里会带 hand 字段)")

    # 控制指令总闸: 环境变量优先 (ARM_ENABLED), 否则用 yaml。
    # 空字符串当作「没设置」 -> 用 yaml 的值。
    env_enabled = os.environ.get("ARM_ENABLED")
    if env_enabled is None or env_enabled.strip() == "":
        enabled = bool(a.get("enabled", True))
    else:
        enabled = env_enabled.strip().lower() not in ("0", "false", "no", "off")

    arm = ArmConfig(
        enabled=enabled,
        controller_ws=os.environ.get(
            "ARM_CONTROLLER_WS", a.get("controller_ws", "ws://192.168.100.16:9000")
        ),
        command_port=int(os.environ.get("ARM_COMMAND_PORT", a.get("command_port", 9030))),
        frequency=int(os.environ.get("ARM_FREQUENCY", a.get("frequency", 100))),
        lead_in_ms=int(os.environ.get("ARM_LEAD_IN_MS", a.get("lead_in_ms", 700))),
        left_arm=os.environ.get("ARM_LEFT_ARM", a.get("left_arm", "") or ""),
        right_arm=os.environ.get("ARM_RIGHT_ARM", a.get("right_arm", "") or ""),
        joint_limit=JointLimit(
            enable=bool(jl.get("enable", True)),
            max_velocity=float(jl.get("max_velocity", 1.5)),
            max_acceleration=float(jl.get("max_acceleration", 5.0)),
        ),
        action_dir=os.environ.get("ARM_ACTION_DIR", a.get("action_dir", str(DEFAULT_ACTION_DIR))),
        exclude_files=[
            str(x) for x in (a.get("exclude_files") or [])
        ],
        idle_stop_ms=int(os.environ.get("ARM_IDLE_STOP_MS", a.get("idle_stop_ms", 1000))),
        connect_timeout=float(a.get("connect_timeout", 5.0)),
        play_timeout_ms=int(a.get("play_timeout_ms", 15000)),
        grasp=grasp,
        flow=flow,
        hand=hand,
        move_mode=os.environ.get(
            "ARM_MOVE_MODE", a.get("move_mode", "local_ik")
        ).strip().lower(),
    )

    s = merged.get("server", {})
    server = ServerConfig(
        host=os.environ.get("ARM_SERVER_HOST", s.get("host", "0.0.0.0")),
        port=int(os.environ.get("ARM_SERVER_PORT", s.get("port", 8802))),
    )

    sf = a.get("safety") or {}
    safety = SafetyConfig(
        enabled=bool(sf.get("enabled", True)),
        require_enabled=bool(sf.get("require_enabled", True)),
        require_connection=bool(sf.get("require_connection", True)),
        check_fault=bool(sf.get("check_fault", True)),
        # 检查整机操作模式是 auto (手动模式控制器拒绝入控: 10074)
        require_auto_mode=bool(sf.get("require_auto_mode", True)),
        allowed_modes=[
            str(x) for x in (sf.get("allowed_modes") or ["CSPPassthrough"])
        ],
        timeout_ms=int(sf.get("timeout_ms", 3000)),
        speak_on_reject=bool(sf.get("speak_on_reject", True)),
    )

    nf = a.get("notifier") or {}
    notifier = NotifierConfig(
        enabled=bool(nf.get("enabled", True)),
        ws_url=os.environ.get(
            "ARM_TTS_WS_URL", nf.get("ws_url", "ws://127.0.0.1:8000/ws")
        ),
        voice=str(nf.get("voice", "") or ""),
        speed=int(nf.get("speed", 5)),
        sample_rate=int(nf.get("sample_rate", 24000)),
        timeout=float(nf.get("timeout", 30.0)),
        server_play=bool(nf.get("server_play", True)),
        backend=os.environ.get("ARM_TTS_BACKEND", str(nf.get("backend", "") or "")),
        device=os.environ.get("ARM_TTS_DEVICE", str(nf.get("device", "") or "")),
    )

    return Config(arm=arm, safety=safety, notifier=notifier, server=server)
