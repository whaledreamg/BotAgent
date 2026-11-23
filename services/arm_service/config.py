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
        keys = ("a", "b", "c")
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
