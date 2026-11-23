#!/usr/bin/env python3
"""高通滤波自检 —— 验证降噪滤波的实际效果。

分三部分:

1. **频响** —— 各频率被衰减/保留了多少 dB
2. **信号处理** —— 用合成的「人声 + 低频噪声」验证:
   - 低频噪声确实被压掉了
   - 人声频段基本保留
3. **识别效果** (可选, 需要 wav 文件) —— 同一段音频, 滤波前后各识别一次,
   对比文本与「噪声触发次数」

用法::

    python services/asr_service/tools/check_highpass.py
    python services/asr_service/tools/check_highpass.py --wav path/to/16k.wav
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

import numpy as np

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from audio_filter import HighPassFilter, measure_response  # noqa: E402

SAMPLE_RATE = 16000

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    tag = PASS if ok else FAIL
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED = 1
    return ok


def _tone(freq: float, seconds: float, amp: float = 8000.0) -> np.ndarray:
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float64)


def test_response(cutoff: float) -> None:
    print(f"\n[1] 频响 (截止频率 {cutoff:.0f}Hz)")
    resp = measure_response(cutoff, SAMPLE_RATE)
    for freq, gain in resp:
        print(f"    {freq:>6.0f}Hz  {gain:>8.2f} dB")

    by_freq = dict(resp)
    # 截止频率处应接近 -3dB
    check(
        "截止频率处约 -3dB",
        -6.0 < by_freq.get(cutoff, 0) < -1.0,
        f"{by_freq.get(cutoff, 0):.2f}dB",
    )
    # 低频应该被明显压制
    check(
        "50Hz 被显著衰减 (< -10dB)",
        by_freq.get(50, 0) < -10,
        f"{by_freq.get(50, 0):.2f}dB",
    )
    # 人声基频区应该基本无损
    check(
        "200Hz 以上基本保留 (>-1dB)",
        all(by_freq.get(f, -99) > -1.0 for f in (200, 400, 1000, 4000)),
        f"200Hz={by_freq.get(200, 0):.3f}dB",
    )


def _tone_mag_db(x: np.ndarray, freq: float) -> float:
    """用 FFT 测某个频率的幅度 (dB)。比 Goertzel 快得多。"""
    n = len(x)
    spec = np.fft.rfft(x * np.hanning(n))
    freqs = np.fft.rfftfreq(n, 1.0 / SAMPLE_RATE)
    idx = int(np.argmin(np.abs(freqs - freq)))
    # 取目标频点附近 ±1 bin 的能量
    lo = max(0, idx - 1)
    hi = min(len(spec), idx + 2)
    mag = np.sqrt(np.sum(np.abs(spec[lo:hi]) ** 2)) * 2.0 / n
    return 20 * np.log10(mag + 1e-12)


def test_processing(cutoff: float) -> None:
    print(f"\n[2] 信号处理 (人声 + 低频噪声)")
    f = HighPassFilter(cutoff, SAMPLE_RATE)

    if not f.enabled:
        check("滤波器已启用", False, "没装 scipy?")
        return

    # 低频噪声: 50Hz 电源哼声 + 100Hz 隆隆声
    # (100Hz 在截止频率之上, 本来就该保留 —— 它靠近人声基频区)
    noise = _tone(50, 1.0) + _tone(100, 1.0, 4000.0)
    # 人声近似: 基频 + 共振峰
    speech = _tone(220, 1.0) + _tone(1200, 1.0, 5000.0) + _tone(2500, 1.0, 3000.0)

    mixed = speech + noise
    peak = np.max(np.abs(mixed))
    if peak > 32767:
        mixed = mixed * (32767.0 / peak)

    def rms_db(x):
        r = float(np.sqrt(np.mean(x ** 2)))
        return 20 * np.log10(r / 32768.0 + 1e-12)

    # ---- 纯 50Hz 应该被大幅压掉 ----
    tone50 = _tone(50, 1.0)
    i50 = np.clip(tone50, -32768, 32767).astype("<i2")
    out50 = np.frombuffer(f.process(i50.tobytes()), dtype="<i2").astype(np.float64)
    d50 = _tone_mag_db(out50, 50) - _tone_mag_db(i50.astype(np.float64), 50)
    check(
        "纯 50Hz 被压掉 (>12dB)",
        d50 < -12,
        f"变化 {d50:+.1f}dB",
    )

    # ---- 100Hz 在截止频率之上, 应当保留 ----
    tone100 = _tone(100, 1.0)
    i100 = np.clip(tone100, -32768, 32767).astype("<i2")
    out100 = np.frombuffer(f.process(i100.tobytes()), dtype="<i2").astype(np.float64)
    d100 = _tone_mag_db(out100, 100) - _tone_mag_db(i100.astype(np.float64), 100)
    check(
        "100Hz 保留 (>-3dB, 靠截止频率以上)",
        d100 > -3.0,
        f"变化 {d100:+.2f}dB",
    )

    # ---- 混合信号: 人声基频不变, 50Hz 被压掉 ----
    mixed_i16 = np.clip(mixed, -32768, 32767).astype("<i2")
    out = np.frombuffer(f.process(mixed_i16.tobytes()), dtype="<i2").astype(np.float64)

    v_before = _tone_mag_db(mixed_i16.astype(np.float64), 220)
    v_after = _tone_mag_db(out, 220)
    check(
        "人声基频 220Hz 保留 (>-2dB)",
        v_after - v_before > -2.0,
        f"{v_before:.1f}dB -> {v_after:.1f}dB (变 {v_after - v_before:+.2f}dB)",
    )

    n_before = _tone_mag_db(mixed_i16.astype(np.float64), 50)
    n_after = _tone_mag_db(out, 50)
    check(
        "混合信号里的 50Hz 被压掉",
        n_after - n_before < -12,
        f"{n_before:.1f}dB -> {n_after:.1f}dB (变 {n_after - n_before:+.1f}dB)",
    )

    # ---- 整体 RMS 应该下降 (低频噪声被去掉) ----
    r_before = rms_db(mixed_i16.astype(np.float64))
    r_after = rms_db(out)
    check(
        "混合信号整体 RMS 下降",
        r_after < r_before,
        f"{r_before:.1f}dB -> {r_after:.1f}dB",
    )


def test_block_continuity(cutoff: float) -> None:
    print(f"\n[3] 分块连续性 (不能有咔哒声)")
    f = HighPassFilter(cutoff, SAMPLE_RATE)
    if not f.enabled:
        return

    sig = (_tone(300, 1.0) + _tone(60, 1.0, 5000.0)).astype("<i2")

    # 分块处理 (模拟实时 256ms 一块)
    block = 4096
    out = b""
    for i in range(0, len(sig.tobytes()), block * 2):
        out += f.process(sig.tobytes()[i:i + block * 2])
    chunked = np.frombuffer(out, dtype="<i2").astype(np.float64)

    # 一次性处理
    f2 = HighPassFilter(cutoff, SAMPLE_RATE)
    whole = np.frombuffer(f2.process(sig.tobytes()), dtype="<i2").astype(np.float64)

    n = min(len(chunked), len(whole))
    diff = np.max(np.abs(chunked[:n] - whole[:n]))
    check(
        "分块与整块结果一致",
        diff < 50,
        f"最大差异 {diff:.0f} (int16 量化误差量级)",
    )


def test_recognition(wav_path: str, cutoff: float) -> None:
    print(f"\n[4] 识别对比 ({wav_path})")
    try:
        from vosk import Model, KaldiRecognizer, SetLogLevel
        import json
        import wave

        SetLogLevel(-1)
        models = sorted(Path.home().joinpath(".cache/vosk").glob("vosk-model-cn-0.22"))
        if not models:
            check("找到 vosk 模型", False, "~/.cache/vosk 下没有 vosk-model-cn-0.22")
            return
        model = Model(str(models[0]))

        with wave.open(wav_path, "rb") as wf:
            if wf.getframerate() != SAMPLE_RATE:
                check("wav 是 16kHz", False, f"实际 {wf.getframerate()}Hz")
                return
            raw = wf.readframes(wf.getnframes())

        def recognize(data: bytes) -> str:
            rec = KaldiRecognizer(model, SAMPLE_RATE)
            rec.AcceptWaveform(data)
            return json.loads(rec.FinalResult()).get("text", "").strip()

        text_raw = recognize(raw)

        f = HighPassFilter(cutoff, SAMPLE_RATE)
        filtered = b""
        for i in range(0, len(raw), 8192):
            filtered += f.process(raw[i:i + 8192])
        text_filt = recognize(filtered)

        print(f"    未滤波: {text_raw!r}")
        print(f"    已滤波: {text_filt!r}")

        # 不要求文本完全一致 (滤波会轻微影响识别), 但两边都不该是空的
        check("滤波后仍能识别出内容", bool(text_filt) or not text_raw,
              f"raw={bool(text_raw)} filtered={bool(text_filt)}")
    except ImportError as exc:
        check("vosk 可用", False, str(exc))
    except Exception as exc:
        check("识别对比", False, str(exc))


def main() -> int:
    parser = argparse.ArgumentParser(description="高通滤波自检")
    parser.add_argument("--cutoff", type=float, default=80.0, help="截止频率(Hz)")
    parser.add_argument("--wav", default=None, help="16kHz wav 文件, 做识别对比")
    args = parser.parse_args()

    print("=" * 60)
    print(f"高通滤波自检 (截止频率 {args.cutoff:.0f}Hz)")
    print("=" * 60)

    test_response(args.cutoff)
    test_processing(args.cutoff)
    test_block_continuity(args.cutoff)
    if args.wav:
        test_recognition(args.wav, args.cutoff)

    print("\n" + "=" * 60)
    if FAILED:
        print("存在失败项")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
