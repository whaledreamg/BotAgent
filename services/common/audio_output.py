"""TTS 播放后端 —— 把 PCM 字节流真正送到电脑的喇叭上。

背景 (踩过的坑)
--------------
ChatTTS 输出的 PCM 是 **24000Hz 单声道**。以前直接用 sounddevice 按
24000Hz 开硬件流, 在大部分机器上没问题, 但换机器就翻车了:

    [TTS] 无可用输出设备, 只合成不播放:
          Error opening RawOutputStream: Invalid sample rate [PaErrorCode -9997]

原因是**声卡支持的采样率是硬件相关的**。实测一台 sof-hda-dsp 声卡只吃
48000Hz, 于是流根本开不起来 -> 只合成不播放 -> 「语音合成后没声音」。
另外, 桌面系统上真喇叭 (模拟输出) 通常被 PulseAudio 占着, 用 ALSA 直接
开硬件会得到「设备或资源忙」。

所以这里提供三种后端, 按可用性自动挑一个:

===========  ====================================================================
``pulse``    走 PulseAudio (``paplay``)。**桌面机首选**:
             - 采样率不匹配由 PulseAudio 自动重采样 (24k -> 48k)
             - 路由到系统默认输出 (用户真正接喇叭的那个口), 音量/静音也跟着桌面混音器
===========  ====================================================================
``alsa``     走 ALSA ``aplay`` + ``plughw`` 插件。**无桌面环境 / 机器人本体首选**:
             ``plughw`` 插件做采样率转换, 不依赖 PulseAudio。
===========  ====================================================================
``sounddevice`` 直接开硬件流, 不做任何重采样。**要求声卡原生支持该采样率**,
             换机器最容易翻车, 所以只当兜底。
===========  ====================================================================

三者都实现了同一个 :class:`AudioOutput` 接口 (write / close), 上层
(asr_service / arm_service) 不用关心底下用的是哪个。

用法::

    out = open_output(sample_rate=24000, channels=1)
    if out is None:
        ...  # 没有可用输出, 只合成不播放
    try:
        out.write(pcm_bytes)
    finally:
        out.close()
"""

from __future__ import annotations

import array
import os
import re
import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

#: ALSA 采样格式 —— ChatTTS 的 PCM 就是小端 int16
ALSA_FORMAT = "S16_LE"

#: 解析 ``aplay -l`` 里的设备行, 例如:
#:   card 1: sofhdadsp [sof-hda-dsp], device 0: HDA Analog (*) []
_APLAY_LIST_RE = re.compile(
    r"^card\s+(?P<card>\d+):.*?,\s*device\s+(?P<device>\d+):\s*(?P<desc>.*)$"
)

#: 优先挑这些描述的设备 (板载声卡的模拟输出 = 接喇叭/耳机的那个口)
_PREFERRED_DESC = ("analog", "speaker", "headphone", "built-in")


class AudioOutput:
    """PCM 播放后端的统一接口。

    子类实现 :meth:`write` 与 :meth:`close`。
    """

    #: 后端名字, 便于日志/诊断里看出来到底走的哪条路
    backend = "none"

    #: 实际使用的设备标识 (子设备/设备名), 可能为空
    device = ""

    def write(self, data: bytes) -> None:
        """写入一段 PCM 字节。"""
        raise NotImplementedError

    def close(self) -> None:
        """关闭输出, 冲刷缓冲。"""
        raise NotImplementedError

    def __enter__(self) -> "AudioOutput":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# ----------------------------------------------------------------------
# 后端 1: PulseAudio
# ----------------------------------------------------------------------


class PulseAudioOutput(AudioOutput):
    """通过 ``paplay`` 把 PCM 喂给 PulseAudio。

    PulseAudio 负责两件关键的事:

    1. **重采样** —— 24k 的 PCM 喂给 48k 的声卡, 它会自己转换
       (实测 ``重采样方法：speex-float-1``)。
    2. **路由** —— 送到系统默认输出, 也就是用户真正接了喇叭的那个口。
       桌面混音器里的音量/静音也跟着生效。

    ``paplay`` 读 ``/dev/stdin``, 所以可以边收 TTS 的帧边播, 不用落盘。
    """

    backend = "pulse"

    def __init__(self, sample_rate: int, channels: int = 1,
                 binary: str = "paplay", stream_name: str = "botagent-tts") -> None:
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.stream_name = stream_name
        self.device = binary

        cmd: List[str] = [
            binary,
            "--raw",
            "--rate", str(self.sample_rate),
            "--channels", str(self.channels),
            "--format", "s16le",
            "--stream-name", stream_name,
            "/dev/stdin",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_pulse_env(),
        )
        # paplay 可能因为「连不上 PulseAudio」立刻退出 —— 这里探测一下,
        # 免得后面的 write 全部静默失败。
        if self._proc.poll() is not None:
            raise RuntimeError(f"{binary} 启动后立即退出 (rc={self._proc.returncode})")

    def write(self, data: bytes) -> None:
        if not data or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise RuntimeError(f"PulseAudio 输出管道已断开: {exc}") from exc

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass


# ----------------------------------------------------------------------
# 后端 2: ALSA
# ----------------------------------------------------------------------


class AlsaOutput(AudioOutput):
    """通过 ``aplay`` + ``plughw`` 插件播放。

    ``plughw`` 插件会在声卡不支持时自动做采样率转换, 所以 24k 的 PCM
    也能在只吃 48k 的声卡上播出来 —— 而且不依赖 PulseAudio,
    适合机器人本体这种没有桌面音频栈的环境。

    设备默认用 ``plughw:<card>,<device>`` (从 ``aplay -l`` 里挑一个),
    优先挑板载模拟输出 (Speaker / Headphones / Analog), 那才是接喇叭的口。

    ⚠️ 桌面系统上真喇叭通常被 PulseAudio 占着, 直接开硬件会得到
    「设备或资源忙」。所以这里会**逐个候选设备试**, 第一个能开成就用它;
    全都开不成才算失败 (自动选路会先试 PulseAudio, 正常不会走到这里)。
    """

    backend = "alsa"

    def __init__(self, sample_rate: int, channels: int = 1,
                 binary: str = "aplay", device: str = "") -> None:
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.device = device or ""

        candidates = [device] if device else pick_alsa_devices()
        if not candidates:
            candidates = ["default"]

        last_err: Optional[Exception] = None
        for cand in candidates:
            try:
                self._spawn(binary, cand)
                self.device = cand
                return
            except Exception as exc:
                last_err = exc
        raise RuntimeError(
            f"所有 ALSA 设备都打不开 (试过: {', '.join(candidates)}): {last_err}"
        )

    def _spawn(self, binary: str, device: str) -> None:
        cmd: List[str] = [
            binary,
            "-q",
            "-D", device,
            "-f", ALSA_FORMAT,
            "-r", str(self.sample_rate),
            "-c", str(self.channels),
            "-t", "raw",
            "-",
        ]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._proc = proc
        # aplay 遇到「设备或资源忙」会**立刻退出**。这里主动探测一次,
        # 免得后面的 write 全变成静默的 broken pipe。
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            return  # 还活着 -> 开成了
        raise RuntimeError(f"aplay -D {device} 启动后立即退出 (rc={proc.returncode})")

    def write(self, data: bytes) -> None:
        if not data or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise RuntimeError(f"ALSA 输出管道已断开: {exc}") from exc

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass


# ----------------------------------------------------------------------
# 后端 3: sounddevice
# ----------------------------------------------------------------------


class SoundDeviceOutput(AudioOutput):
    """直接开 PortAudio 硬件流 (历史上的做法)。

    **不做任何重采样** —— 声卡必须原生支持 ``sample_rate``。
    这正是换机器后没声音的元凶, 所以这里只当兜底:

    - 打开失败时**自动降采样**到设备支持的采样率, 并对 PCM 做线性重采样,
      保证「至少能出声」;
    - 重采样用纯 Python 实现 (:class:`_StreamResampler`), 不依赖 numpy ——
      arm_service 所在的 conda 环境没有 numpy。
    """

    backend = "sounddevice"

    def __init__(self, sample_rate: int, channels: int = 1, device=None) -> None:
        import sounddevice as sd  # 延迟导入: 没装也不影响其它后端

        self._sd = sd
        self.channels = int(channels)
        self.device = device
        self._target_rate = int(sample_rate)
        self._resampler: Optional["_StreamResampler"] = None

        try:
            self._stream = sd.RawOutputStream(
                samplerate=sample_rate,
                channels=channels,
                dtype="int16",
                blocksize=0,
                device=device,
            )
        except Exception:
            # 多半就是「Invalid sample rate」—— 换设备支持的采样率再试。
            fallback = _device_default_rate(sd, device)
            if fallback is None or fallback == sample_rate:
                raise
            self._target_rate = fallback
            self._resampler = _StreamResampler(int(sample_rate), fallback)
            self._stream = sd.RawOutputStream(
                samplerate=fallback,
                channels=channels,
                dtype="int16",
                blocksize=0,
                device=device,
            )

        self._stream.start()

    def write(self, data: bytes) -> None:
        if not data:
            return
        if self._resampler is not None:
            data = self._resampler.process(data)
        self._stream.write(data)

    def close(self) -> None:
        try:
            self._stream.stop()
        except Exception:
            pass
        try:
            self._stream.close()
        except Exception:
            pass


class _StreamResampler:
    """流式线性重采样 (int16 单声道 -> int16 单声道)。

    只在 sounddevice 兜底路径上用。逐块重采样会在块边界引入极小的
    阶跃, 对语音播报听不出来, 换来的是「任何机器都能出声」。
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self._tail = array.array("h")

    def process(self, data: bytes) -> bytes:
        src = array.array("h")
        src.frombytes(data)
        if sys.byteorder == "big":
            src.byteswap()
        self._tail.extend(src)

        n_src = len(self._tail)
        if n_src < 2:
            return b""

        step = self.src_rate / self.dst_rate
        n_dst = int((n_src - 1) / step)
        out = array.array("h", bytes(2 * n_dst))
        for i in range(n_dst):
            pos = i * step
            j = int(pos)
            frac = pos - j
            out[i] = int(self._tail[j] + (self._tail[j + 1] - self._tail[j]) * frac)

        # 留最后 1 个采样点, 下次接着用, 避免块边界丢样
        consumed = int((n_dst - 1) * step) + 1
        self._tail = array.array("h", self._tail[consumed:])
        if sys.byteorder == "big":
            out.byteswap()
        return out.tobytes()


def resample_pcm_s16le(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """一次性线性重采样 (小端 int16 单声道)。"""
    if src_rate == dst_rate or not data:
        return data
    src = array.array("h")
    src.frombytes(data)
    if sys.byteorder == "big":
        src.byteswap()
    n_src = len(src)
    if n_src == 0:
        return data
    step = src_rate / dst_rate
    n_dst = max(1, int(n_src / step))
    out = array.array("h", bytes(2 * n_dst))
    for i in range(n_dst):
        pos = i * step
        j = int(pos)
        if j >= n_src - 1:
            out[i] = src[n_src - 1]
        else:
            frac = pos - j
            out[i] = int(src[j] + (src[j + 1] - src[j]) * frac)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------


def _pulse_env() -> dict:
    """给 paplay 准备环境变量。

    ``paplay`` 通过 ``XDG_RUNTIME_DIR`` 找 PulseAudio 的 socket。这个变量
    通常由登录会话带下来, 但服务被 systemd / nohup 拉起时可能没有 ——
    这时候按 uid 猜一下 ``/run/user/<uid>``。
    """
    env = dict(os.environ)
    if not env.get("XDG_RUNTIME_DIR"):
        guess = f"/run/user/{os.getuid()}"
        if os.path.isdir(guess):
            env["XDG_RUNTIME_DIR"] = guess
    return env


def _device_default_rate(sd, device) -> Optional[int]:
    """查设备默认采样率。查不到返回 None。"""
    try:
        if device is None:
            info = sd.query_devices(kind="output")
        else:
            info = sd.query_devices(device, "output")
        rate = float(info.get("default_samplerate") or 0)
        return int(rate) if rate > 0 else None
    except Exception:
        return None


def pick_alsa_devices() -> List[str]:
    """列出候选的 ALSA 播放设备 (按优先级)。

    从 ``aplay -l`` 里挑, 优先板载模拟输出 (Speaker / Headphones / Analog)
    —— 那是接喇叭的那个口; HDMI/DisplayPort 多半是接显示器的, 声音会从
    显示器出来。找不到就退回第一个播放设备。

    返回形如 ``["plughw:1,0", "plughw:1,3", ...]``。
    """
    aplay = shutil.which("aplay")
    if not aplay:
        return []
    try:
        out = subprocess.run(
            [aplay, "-l"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except Exception:
        return []

    devices: List[Tuple[int, int, str]] = []
    for line in out.splitlines():
        m = _APLAY_LIST_RE.match(line.strip())
        if not m:
            continue
        devices.append((
            int(m.group("card")),
            int(m.group("device")),
            m.group("desc").lower(),
        ))

    if not devices:
        return []

    preferred = [
        f"plughw:{card},{device}"
        for card, device, desc in devices
        if any(k in desc for k in _PREFERRED_DESC)
    ]
    others = [
        f"plughw:{card},{device}"
        for card, device, desc in devices
        if not any(k in desc for k in _PREFERRED_DESC)
    ]
    return preferred + others


def pick_alsa_device() -> str:
    """挑一个 ALSA 播放设备 (第一个候选)。"""
    devices = pick_alsa_devices()
    return devices[0] if devices else "default"


def list_backends() -> List[str]:
    """列出当前可用的后端 (按优先级)。"""
    avail: List[str] = []
    if shutil.which("paplay") or shutil.which("pacat"):
        avail.append("pulse")
    if shutil.which("aplay"):
        avail.append("alsa")
    try:
        import sounddevice  # noqa: F401
        avail.append("sounddevice")
    except Exception:
        pass
    return avail


def open_output(
    sample_rate: int = 24000,
    channels: int = 1,
    backend: str = "auto",
    device: str = "",
    stream_name: str = "botagent-tts",
) -> Optional[AudioOutput]:
    """打开一个播放输出。失败返回 None (调用方降级成「只合成不播放」)。

    ``backend``:

    - ``auto`` / ``""`` (默认): 按 pulse -> alsa -> sounddevice 依次尝试
    - ``pulse`` / ``alsa`` / ``sounddevice``: 只用指定的那个
    """
    # 配置里留空表示「自动」, 与显式的 "auto" 等价
    backend = (backend or "auto").strip().lower()

    if backend in ("auto", "pulse"):
        binary = shutil.which("paplay") or shutil.which("pacat")
        if binary:
            try:
                return PulseAudioOutput(
                    sample_rate, channels, binary=binary, stream_name=stream_name
                )
            except Exception:
                if backend == "pulse":
                    raise

    if backend in ("auto", "alsa"):
        if shutil.which("aplay"):
            try:
                return AlsaOutput(
                    sample_rate, channels, device=device
                )
            except Exception:
                if backend == "alsa":
                    raise

    if backend in ("auto", "sounddevice"):
        try:
            return SoundDeviceOutput(sample_rate, channels)
        except Exception:
            if backend == "sounddevice":
                raise

    return None
