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
#: 热词词表 (专业名词纠错)
HOTWORDS_CONFIG_PATH = ROOT / "configs" / "hotwords.yaml"

DEFAULTS: Dict[str, Any] = {
    "asr": {
        "health_port": 8801,
        "wake_words": "你好助手",
        # 结束词 (会话结束语)。用户说完结束词后, 这一轮对话立即结束,
        # 连续对话窗口关闭, 需要重新说唤醒词才能继续。
        # 留空 = 不启用结束词。
        "end_words": "",
        # 结束词的**别名** (同义/近形的告别词), 多个用逗号分隔。
        # 命中任意一个都算结束 —— 用来兜住「拜拜」被听成「白白」这类情况。
        "end_aliases": "",
        # 是否启用**拼音模糊兜底**匹配结束词。true = 读音相近也算命中。
        "end_match_fuzzy": True,
        # 拼音模糊匹配的相似度阈值 (0~1)。越高越严格。
        "end_match_threshold": 0.6,
        # 识别到结束词时的播报文案。留空 = 不播报, 只结束会话。
        "end_prompt": "好的，我走了",
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
        # 高通滤波截止频率 (Hz)。0 = 关闭。
        # 空调隆隆声/电源 50Hz 哼声这类低频噪声会抬高静默时的 RMS 电平,
        # 让 VAD 门限跟着抬高。80Hz 的 4 阶高通把 50Hz 压掉 16dB,
        # 200Hz 以上几乎无损。
        "highpass_hz": 80.0,
        # 高通阶数 (默认 4 = 两个二阶节)
        "highpass_order": 4,
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
        # 播放后端。留空 = 自动选路 (PulseAudio -> ALSA -> sounddevice)。
        # 换机器后没声音基本都源于此: 24000Hz 的 PCM 遇上只支持 48000Hz 的
        # 声卡, 直接开流会报 Invalid sample rate。自动选路会自动重采样。
        # 可选值: auto / pulse / alsa / sounddevice
        "backend": "",
        # 播放设备。留空 = 自动挑 (优先板载模拟输出 = 接喇叭的那个口)。
        "device": "",
        "timeout": 120,
    },
    "server": {"host": "127.0.0.1", "port": 8801},
}


def _as_bool(raw: Any, default: bool = False) -> bool:
    """把配置值/env 变量解析成布尔。

    YAML 里的 ``true``/``false`` 已经是布尔了, 但 **env 变量都是字符串**
    —— ``ASR_END_MATCH_FUZZY=false`` 不能靠 ``bool(str)`` 判 (那是 True)。

    认这些写法 (大小写不敏感): ``1/true/yes/on`` -> True,
    ``0/false/no/off`` -> False。认不出来就用默认值。
    """
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    return default


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
    #: 结束词 (会话结束语), 多个用逗号分隔。留空 = 不启用。
    #:
    #: 用户说完结束词后, 这一轮对话立即结束 —— 连续对话窗口关闭,
    #: 需要重新说唤醒词才能继续对话。
    end_words: str = ""
    #: 结束词的**别名**, 多个用逗号分隔。
    #:
    #: 命中**任意一个**触发词 (主结束词 + 别名) 都算结束。存在的意义是
    #: 兜住 ASR 的常见误识别 —— 「拜拜」听成「白白」「百拜」这类。
    end_aliases: str = ""
    #: 是否启用**拼音模糊兜底**匹配结束词。
    #:
    #: 纯文本子串匹配对「白白」「百拜」这种**同音/近音**误识别完全
    #: 无能为力。打开后, 会把文本切成与结束词等音节数的窗口, 按拼音
    #: 相似度比对, 超过 ``end_match_threshold`` 就算命中。
    #:
    #: 代价是可能误触发 —— 所以留成可关的开关。
    end_match_fuzzy: bool = True
    #: 拼音模糊匹配的相似度阈值 (0~1)。越高越严格。
    end_match_threshold: float = 0.6
    #: 识别到结束词时的播报文案。留空 = 不播报, 只结束会话。
    end_prompt: str = "好的，我走了"
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
    #: 麦克风输入的**高通滤波**截止频率 (Hz)。0 = 关闭。
    #:
    #: 空调隆隆声、电源 50Hz 哼声这类低频噪声会把静默时的 RMS 电平拉高,
    #: 让 VAD 门限跟着抬高, 说话声更容易被判成噪声。80Hz 的 4 阶高通把
    #: 50Hz 压掉 16dB, 而 200Hz 以上几乎无损 (人声内容基本不损失)。
    #:
    #: 滤波在**音频入队前**应用, 所以 VAD 与识别看到的是同一份信号。
    highpass_hz: float = 80.0
    #: 高通滤波阶数。默认 4 (两个二阶节)。越大低频衰减越狠, 越容易有相位失真。
    highpass_order: int = 4

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

    @property
    def end_word_list(self) -> list:
        """把配置里的结束词解析成列表。

        写法和唤醒词一样 (逗号分隔字符串或 YAML 列表)。
        空的 = 不启用结束词。
        """
        raw = self.end_words
        if isinstance(raw, (list, tuple)):
            items = [str(w).strip() for w in raw]
        else:
            items = [w.strip() for w in str(raw).split(",")]
        return [w for w in items if w]

    @property
    def end_alias_list(self) -> list:
        """把配置里的结束词**别名**解析成列表。

        写法和结束词一样 (逗号分隔字符串或 YAML 列表)。
        """
        raw = self.end_aliases
        if isinstance(raw, (list, tuple)):
            items = [str(w).strip() for w in raw]
        else:
            items = [w.strip() for w in str(raw).split(",")]
        return [w for w in items if w]

    @property
    def end_trigger_list(self) -> list:
        """结束词的**完整触发词集合** —— 主结束词 + 别名。

        命中任意一个都算结束。顺序: 主结束词在前, 别名在后。
        """
        seen = set()
        out = []
        for w in self.end_word_list + self.end_alias_list:
            if w and w not in seen:
                seen.add(w)
                out.append(w)
        return out


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
    #: 播放后端。留空 = 自动选路 (PulseAudio -> ALSA -> sounddevice)。
    #:
    #: ⚠️ 换机器后「合成成功但没声音」基本都是这个的原因: ChatTTS 的 PCM
    #:    是 24000Hz, 而声卡原生采样率因机器而异 (有的只吃 48000Hz)。
    #:    自动选路会用 PulseAudio / plughw 做重采样, 任何机器都能出声。
    #:
    #: 可选值: auto / pulse / alsa / sounddevice
    backend: str = ""
    #: 播放设备。留空 = 自动挑 (优先板载模拟输出, 也就是接喇叭的那个口)。
    #: 只对 alsa 后端有意义 (形如 "plughw:1,0")。
    device: str = ""
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
    #: 热词配置 (专业名词纠错), 来自 configs/hotwords.yaml
    hotwords: Dict[str, Any] = field(default_factory=dict)


def load_hotwords(path: Path = HOTWORDS_CONFIG_PATH) -> Dict[str, Any]:
    """加载热词配置。

    词表放在**独立文件** (configs/hotwords.yaml) 而不是塞进 pipeline.yaml ——
    词表会频繁增删, 混在服务配置里容易改坏。

    读取失败不致命 —— 返回空配置, 服务照常启动 (只是没有热词纠错)。
    """
    if yaml is None or not path or not Path(path).is_file():
        return {}
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:  # pragma: no cover - 配置文件坏了不该让服务起不来
        return {}
    if not isinstance(data, dict):
        return {}
    return data.get("hotwords") or {}


def load_config(path: Path = PIPELINE_CONFIG_PATH) -> Config:
    data: Dict[str, Any] = {}
    if yaml is not None and path and Path(path).is_file():
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    merged = _deep_merge(DEFAULTS, data)

    a = merged.get("asr", {})
    asr = ASRConfig(
        health_port=int(os.environ.get("ASR_HEALTH_PORT", a.get("health_port", 8801))),
        wake_words=os.environ.get("ASR_WAKE_WORDS", a.get("wake_words", "你好助手")),
        end_words=os.environ.get("ASR_END_WORDS", a.get("end_words", "") or ""),
        end_aliases=os.environ.get(
            "ASR_END_ALIASES", a.get("end_aliases", "") or ""
        ),
        end_match_fuzzy=_as_bool(
            os.environ.get("ASR_END_MATCH_FUZZY"),
            a.get("end_match_fuzzy", True),
        ),
        end_match_threshold=float(
            os.environ.get(
                "ASR_END_MATCH_THRESHOLD",
                a.get("end_match_threshold", 0.6),
            )
        ),
        end_prompt=os.environ.get(
            "ASR_END_PROMPT", a.get("end_prompt", "好的，我走了") or ""
        ),
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
        highpass_hz=float(
            os.environ.get(
                "ASR_HIGHPASS_HZ",
                a.get("highpass_hz", 80.0),
            )
        ),
        highpass_order=int(
            os.environ.get(
                "ASR_HIGHPASS_ORDER",
                a.get("highpass_order", 4),
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
        backend=os.environ.get("TTS_BACKEND", str(t.get("backend", "") or "")),
        device=os.environ.get("TTS_DEVICE", str(t.get("device", "") or "")),
        timeout=float(t.get("timeout", 120)),
    )

    s = merged.get("server", {})
    server = ServerConfig(
        host=os.environ.get("SERVER_HOST", s.get("host", "127.0.0.1")),
        port=int(os.environ.get("SERVER_PORT", s.get("port", 8801))),
    )

    return Config(
        asr=asr,
        agent=agent,
        tts=tts,
        server=server,
        hotwords=load_hotwords(),
    )
