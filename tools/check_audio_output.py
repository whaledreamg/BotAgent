#!/usr/bin/env python3
"""TTS 播放链路自检 —— 回答「语音合成后电脑喇叭为什么没声音」。

按顺序检查:

1. **可用后端** —— 这台机器上有哪些播放方式 (PulseAudio / ALSA / sounddevice)
2. **声卡能力** —— 每个输出设备支持哪些采样率
   (ChatTTS 的 PCM 是 24000Hz; 声卡只吃 48000Hz 时就会没声音)
3. **实际播放** —— 真的写一段 1kHz 正弦波进去, 并确认 PulseAudio 那边
   真的收到了音频流 (路由到了哪个 sink)

用法::

    python tools/check_audio_output.py            # 只检查
    python tools/check_audio_output.py --play     # 顺便真的播一段 (会出声)

⚠️ 跟 asr_service / arm_service 用的是同一套后端
   (services/common/audio_output.py), 所以这里通过了, 语音就一定能出声。
"""

from __future__ import annotations

import argparse
import math
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

from common.audio_output import (  # noqa: E402
    list_backends,
    open_output,
    pick_alsa_devices,
)

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    tag = PASS if ok else FAIL
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED = 1
    return ok


def warn(name: str, detail: str = "") -> None:
    print(f"  [{WARN}] {name}" + (f"  ({detail})" if detail else ""))


def test_backends() -> list[str]:
    print("\n[1] 可用的播放后端")
    backends = list_backends()
    if not backends:
        check("至少有一个后端可用", False, "装了 pulseaudio / alsa-utils 吗?")
        return []
    for b in backends:
        print(f"  [ok] {b}")
    return backends


def test_device_rates() -> None:
    print("\n[2] 输出设备支持的采样率 (TTS 用的是 24000Hz)")
    try:
        import sounddevice as sd
    except ImportError:
        warn("sounddevice 没装, 跳过采样率检查",
             "脉冲/ALSA 后端不受这个限制")
        return

    try:
        devices = sd.query_devices()
    except Exception as exc:
        warn("查询设备失败", str(exc))
        return

    outputs = [
        (i, d) for i, d in enumerate(devices)
        if d["max_output_channels"] > 0
    ]
    if not outputs:
        warn("没有输出设备")
        return

    for idx, dev in outputs:
        rates = []
        for sr in (24000, 44100, 48000):
            try:
                stream = sd.RawOutputStream(
                    samplerate=sr, channels=1, dtype="int16", device=idx
                )
                stream.start()
                stream.stop()
                stream.close()
                rates.append(sr)
            except Exception:
                pass
        name = dev["name"]
        native = int(dev.get("default_samplerate") or 0)
        detail = f"默认 {native}Hz, 可用 {rates or '无'}"
        if 24000 in rates:
            check(f"{name} 直接支持 24000Hz", True, detail)
        else:
            # 这正是「没声音」的根因: 硬开流会报 Invalid sample rate。
            # PulseAudio / plughw 后端会自动重采样, 所以不算致命。
            warn(f"{name} 不支持 24000Hz (需靠重采样)", detail)


def test_alsa_devices() -> None:
    print("\n[3] ALSA 播放设备候选")
    devices = pick_alsa_devices()
    if not devices:
        warn("没找到 ALSA 播放设备")
        return
    for d in devices:
        print(f"  [ok] {d}")


def test_play(play: bool) -> None:
    print("\n[4] 实际播放测试")
    try:
        out = open_output(sample_rate=24000, channels=1)
    except Exception as exc:
        check("打开播放输出", False, str(exc))
        return

    if out is None:
        check("打开播放输出", False, "没有可用后端")
        return

    check(
        "打开播放输出",
        True,
        f"后端={out.backend} 设备={out.device or '-'}",
    )

    # 1 秒的 1kHz 正弦波
    pcm = b"".join(
        struct.pack("<h", int(15000 * math.sin(2 * math.pi * 440 * i / 24000)))
        for i in range(24000)
    )
    try:
        out.write(pcm)
        check("写入 PCM", True, f"{len(pcm)} 字节")
    except Exception as exc:
        check("写入 PCM", False, str(exc))
        out.close()
        return

    if not play:
        print("  (跳过实际出声, 加 --play 才会真的播)")
        out.close()
        return

    # 播放期间盯着 PulseAudio, 看音频流有没有真的落到某个 sink 上
    sink = _watch_sink()
    out.close()
    if sink:
        check("音频流已路由到输出设备", True, sink)
    else:
        warn("没抓到 PulseAudio 音频流",
             "可能是没有桌面音频栈; 肉耳听一下有没有声音")


def _watch_sink() -> str:
    """播放期间轮询 PulseAudio, 返回路由信息 (抓不到就返回空串)。"""
    pactl = _which("pactl")
    if not pactl:
        return ""
    for _ in range(20):
        try:
            out = subprocess.run(
                [pactl, "list", "sink-inputs"],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout
        except Exception:
            return ""
        if "sink-input" in out or "信宿输入" in out:
            sink = ""
            rate = ""
            for line in out.splitlines():
                if "信宿：" in line or "Sink:" in line:
                    sink = line.strip()
                if "重采样方法" in line or "Sample Specification" in line:
                    rate = line.strip()
            return f"{sink} {rate}".strip()
        time.sleep(0.15)
    return ""


def _which(name: str) -> str:
    import shutil
    return shutil.which(name) or ""


def main() -> int:
    parser = argparse.ArgumentParser(description="TTS 播放链路自检")
    parser.add_argument(
        "--play", action="store_true",
        help="真的播一段 1kHz 正弦波 (会出声)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("TTS 播放链路自检")
    print("=" * 60)

    test_backends()
    test_device_rates()
    test_alsa_devices()
    test_play(args.play)

    print("\n" + "=" * 60)
    if FAILED:
        print("存在失败项")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
