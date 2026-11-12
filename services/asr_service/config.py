"""ASR 服务配置加载。

配置来源: configs/pipeline.yaml (+ 环境变量覆盖)。
只依赖 PyYAML, 若环境缺失则退化为内置默认值, 保证服务仍可启动。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


ROOT = Path(__file__).resolve().parents[2]
PIPELINE_CONFIG_PATH = ROOT / "configs" / "pipeline.yaml"

DEFAULTS: Dict[str, Any] = {
    "asr": {
        "health_port": 8801,
        "wake_words": "你好助手",
        "model_path": "",
        "device": "",
        "max_duration": 30.0,
        "silence_timeout": 1.2,
        "vad_threshold": -45.0,
        "session_id": "robot-01",
        # 识别不到内容时的播报文案; 留空表示不播报
        # 这句话既用于 TTS 播报, 也用于 /state 的 errors 记录, 保证两边一致。
        "empty_result_prompt": "没听清内容，请再说一遍",
        # 连续对话窗口(秒)。一轮对话结束后, 在这个时间内再次说话不需要唤醒词;
        # 超过则回到 IDLE, 需要重新说唤醒词。设为 0 则关闭连续对话。
        "continuous_timeout": 30.0,
        # 回声沉降期(秒)。TTS 播完之后, 扬声器的余音还在空气里飘
        # (声波传播 + 房间混响 + 音频缓冲排空)。这段时间内麦克风录到的
        # 全是机器人自己的声音 —— 如果当成人声触发录音, 下一轮就会把
        # 播报内容当成用户的话再识别一遍 (机器人自己跟自己对话)。
        #
        # 这个值要盖住「播完 -> 房间安静」的时间。小房间 0.4~0.6s 够,
        # 大房间/嘈杂环境要调大。设为 0 = 关闭回声抑制 (不推荐)。
        "echo_settle": 0.5,
        # 等安静的最长等待(秒)。沉降期结束后最多再等这么久确认房间安静,
        # 超时强制恢复 (免得房间一直吵导致机器人变聋)。
        "echo_quiet_timeout": 2.0,
    },
    "agent": {
        "base_url": "http://127.0.0.1:8800",
        "dispatch_path": "/v1/dispatch",
        "timeout": 90,
        "max_retries": 2,
    },
    "tts": {
        "ws_url": "ws://127.0.0.1:8000/ws",
        "voice": "Default",
        "speed": 5,
        "sample_rate": 24000,
        "timeout": 120,
    },
    "server": {"host": "127.0.0.1", "port": 8801},
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
class ASRConfig:
    health_port: int = 8801
    wake_words: str = "你好助手"
    model_path: str = ""
    device: str = ""
    max_duration: float = 30.0
    silence_timeout: float = 1.2
    vad_threshold: float = -45.0
    session_id: str = "robot-01"
    #: 识别不到内容时的播报文案; 留空表示不播报
    #: (同时用于 /state 的 errors 记录, 保证日志与播报一致)
    empty_result_prompt: str = "没听清内容，请再说一遍"
    #: 连续对话窗口(秒)。一轮结束后这段时间内说话不需要唤醒词; 0 表示关闭
    continuous_timeout: float = 30.0
        #: 回声沉降期(秒)。TTS 播完之后, 等多久才接受用户语音。
        #:
        #: 播报期间麦克风会录到机器人自己的声音, 播完之后还有余音飘着。
        #: 这段时间内不采信任何「人声」, 否则会把播报内容当成用户的话
        #: 再识别一遍 (机器人自己跟自己对话)。
        #:
        #: 0 = 关闭回声抑制 (不推荐)。
    echo_settle: float = 0.5
    #: 等安静的最长等待 (秒)。沉降期结束后, 最多再等这么久来确认
    #: 房间真的安静下来; 超时就强制恢复 (免得房间一直吵导致机器人变聋)。
    echo_quiet_timeout: float = 2.0

    @property
    def wake_word_list(self) -> list:
        """把配置里的唤醒词解析成列表。

        支持三种写法:
        - 逗号分隔字符串: "你好助手,小助手"
        - YAML 列表: ["你好助手", "小助手"]
        """
        raw = self.wake_words
        if isinstance(raw, (list, tuple)):
            items = [str(w).strip() for w in raw]
        else:
            items = [w.strip() for w in str(raw).split(",")]
        return [w for w in items if w]


@dataclass
class AgentEndpoint:
    base_url: str = "http://127.0.0.1:8800"
    dispatch_path: str = "/v1/dispatch"
    timeout: float = 90.0
    max_retries: int = 2

    @property
    def dispatch_url(self) -> str:
        return self.base_url.rstrip("/") + "/" + self.dispatch_path.lstrip("/")


@dataclass
class TTSConfig:
    ws_url: str = "ws://127.0.0.1:8000/ws"
    #: 音色名。留空 = 用 TTS 服务端的默认音色 (configs/tts.yaml 的 defaults.voice)。
    #: 填了才覆盖 —— 这样「换默认音色」只需要改服务端配置一处。
    voice: str = ""
    speed: int = 5
    #: 保音高变速倍率。0 = 用服务端默认值。
    rate: float = 0.0
    sample_rate: int = 24000
    timeout: float = 120.0


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8801


@dataclass
class Config:
    asr: ASRConfig = field(default_factory=ASRConfig)
    agent: AgentEndpoint = field(default_factory=AgentEndpoint)
    tts: TTSConfig = field(default_factory=TTSConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


def load_config(path: Path = PIPELINE_CONFIG_PATH) -> Config:
    data: Dict[str, Any] = {}
    if yaml is not None and path and Path(path).is_file():
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    merged = _deep_merge(DEFAULTS, data)

    a = merged.get("asr", {})
    asr = ASRConfig(
        health_port=int(os.environ.get("ASR_HEALTH_PORT", a.get("health_port", 8801))),
        wake_words=os.environ.get("ASR_WAKE_WORDS", a.get("wake_words", "你好助手")),
        model_path=os.environ.get("ASR_MODEL_PATH", a.get("model_path", "") or ""),
        device=os.environ.get("ASR_DEVICE", a.get("device", "") or ""),
        max_duration=float(a.get("max_duration", 30.0)),
        silence_timeout=float(a.get("silence_timeout", 1.2)),
        vad_threshold=float(a.get("vad_threshold", -45.0)),
        session_id=os.environ.get("ASR_SESSION_ID", a.get("session_id", "robot-01")),
        empty_result_prompt=os.environ.get(
            "ASR_EMPTY_RESULT_PROMPT",
            a.get("empty_result_prompt", "没听清内容，请再说一遍") or "",
        ),
        continuous_timeout=float(
            os.environ.get(
                "ASR_CONTINUOUS_TIMEOUT",
                a.get("continuous_timeout", 30.0),
            )
        ),
        echo_settle=float(
            os.environ.get(
                "ASR_ECHO_SETTLE",
                a.get("echo_settle", 0.5),
            )
        ),
        echo_quiet_timeout=float(
            os.environ.get(
                "ASR_ECHO_QUIET_TIMEOUT",
                a.get("echo_quiet_timeout", 2.0),
            )
        ),
    )

    ag = merged.get("agent", {})
    agent = AgentEndpoint(
        base_url=os.environ.get("AGENT_BASE_URL", ag.get("base_url", "http://127.0.0.1:8800")),
        dispatch_path=ag.get("dispatch_path", "/v1/dispatch"),
        timeout=float(ag.get("timeout", 90)),
        max_retries=int(ag.get("max_retries", 2)),
    )

    t = merged.get("tts", {})
    tts = TTSConfig(
        ws_url=os.environ.get("TTS_WS_URL", t.get("ws_url", "ws://127.0.0.1:8000/ws")),
        # 留空 = 用 TTS 服务端的默认音色 (configs/tts.yaml)
        voice=os.environ.get("TTS_VOICE", str(t.get("voice", "") or "")),
        speed=int(t.get("speed", 5)),
        rate=float(t.get("rate", 0.0)),
        sample_rate=int(t.get("sample_rate", 24000)),
        timeout=float(t.get("timeout", 120)),
    )

    s = merged.get("server", {})
    server = ServerConfig(
        host=os.environ.get("SERVER_HOST", s.get("host", "127.0.0.1")),
        port=int(os.environ.get("SERVER_PORT", s.get("port", 8801))),
    )

    return Config(asr=asr, agent=agent, tts=tts, server=server)
