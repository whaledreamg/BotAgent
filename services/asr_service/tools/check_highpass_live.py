#!/usr/bin/env python3
"""用真实麦克风测高通滤波的实际效果。

做两件事:

1. **同时**录两份: 一份是原始信号, 一份是滤波后的信号
   (直接跑同一个 RawInputStream, 手动处理回调里的数据)。
2. 分别统计两者的:
   - RMS 电平 (dBFS)
   - 噪声底 (最低 10% 的块)
   - 频谱重心 —— 低频能量占比会明显下降
   - 50Hz / 100Hz 附近的能量

这能直接回答「滤波到底有没有起作用」。

用法::

    python services/asr_service/tools/check_highpass_live.py --seconds 5
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from audio_filter import HighPassFilter  # noqa: E402

SAMPLE_RATE = 16000
BLOCK = 4096


def _band_energy_db(x: np.ndarray, lo: float, hi: float) -> float:
    """某个频带内的总能量 (dB)。"""
    n = len(x)
    spec = np.abs(np.fft.rfft(x * np.hanning(n))) ** 2
    freqs = np.fft.rfftfreq(n, 1.0 / SAMPLE_RATE)
    mask = (freqs >= lo) & (freqs <= hi)
    if not mask.any():
        return -200.0
    return 10 * np.log10(spec[mask].sum() / n + 1e-20)


def main() -> int:
    parser = argparse.ArgumentParser(description="真实麦克风测高通滤波")
    parser.add_argument("--seconds", type=float, default=5.0, help="采集时长(秒)")
    parser.add_argument("--cutoff", type=float, default=80.0, help="截止频率(Hz)")
    parser.add_argument("--device", default=None, help="设备索引")
    args = parser.parse_args()

    import sounddevice as sd

    if args.device is not None:
        dev = int(args.device)
        print(f"[麦克风] 指定设备 index={dev}")
    else:
        # 用与 ASR 服务**完全相同**的选设备策略
        # (直接调 wakeup_asr.pick_device, 免得两处逻辑漂移)
        from wakeup_asr import pick_device

        cands = pick_device(None)
        dev = None
        errors = []
        for c in cands:
            probe = None
            try:
                probe = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1,
                                          dtype="int16", blocksize=BLOCK, device=c)
                dev = c
                break
            except Exception as exc:
                errors.append((c, exc))
            finally:
                # 探测用的流必须关掉, 否则设备被占住, 后面正式打开会失败
                if probe is not None:
                    try:
                        probe.close()
                    except Exception:
                        pass
        if dev is None:
            print("打不开任何输入设备:")
            for c, e in errors:
                print(f"  device {c}: {e}")
            return 1
        info = sd.query_devices(dev, "input")
        print(f"[麦克风] {info['name']} (index={dev})")

    filt = HighPassFilter(args.cutoff, SAMPLE_RATE)
    if not filt.enabled:
        print("[警告] 滤波器未启用")
        return 1
    print(f"[滤波] {filt.description}")

    raw_chunks = []
    filt_chunks = []

    def cb(indata, frames, time_info, status):
        raw = bytes(indata)
        raw_chunks.append(raw)
        filt_chunks.append(filt.process(raw))

    with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                           blocksize=BLOCK, device=dev, callback=cb):
        print(f"[采集] {args.seconds:.0f}s ...")
        time.sleep(args.seconds)

    raw = np.frombuffer(b"".join(raw_chunks), dtype="<i2").astype(np.float64)
    flt = np.frombuffer(b"".join(filt_chunks), dtype="<i2").astype(np.float64)
    if raw.size == 0:
        print("没采到音频")
        return 1

    def stats(x):
        # 按块算 RMS, 取噪声底 = 最低 10% 的块
        blocks = x[: (len(x) // BLOCK) * BLOCK].reshape(-1, BLOCK)
        rms = np.sqrt(np.mean(blocks ** 2, axis=1))
        db = 20 * np.log10(rms / 32768.0 + 1e-12)
        floor = float(np.percentile(db, 10))
        overall = float(20 * np.log10(np.sqrt(np.mean(x ** 2)) / 32768.0 + 1e-12))
        return db, floor, overall

    db_raw, floor_raw, all_raw = stats(raw)
    db_flt, floor_flt, all_flt = stats(flt)

    print("\n" + "=" * 64)
    print(f"采集 {len(raw) / SAMPLE_RATE:.1f}s, {len(db_raw)} 块")
    print("=" * 64)
    print(f"{'':22}{'原始':>18}{'滤波后':>18}")
    print("-" * 64)
    print(f"{'整体 RMS (dBFS)':22}{all_raw:>18.2f}{all_flt:>18.2f}")
    print(f"{'噪声底 (最低10%块)':20}{floor_raw:>18.2f}{floor_flt:>18.2f}")
    print(f"{'块电平中位数':20}{float(np.median(db_raw)):>18.2f}{float(np.median(db_flt)):>18.2f}")
    print("-" * 64)

    print("\n[频带能量 (整段)]")
    for lo, hi, name in [(20, 60, "20-60Hz 隆隆声"),
                         (60, 120, "60-120Hz 哼声区"),
                         (120, 250, "120-250Hz 人声基频"),
                         (250, 1000, "250-1000Hz"),
                         (1000, 4000, "1-4kHz 辅音"),
                         (4000, 7800, "4-7.8kHz")]:
        a = _band_energy_db(raw, lo, hi)
        b = _band_energy_db(flt, lo, hi)
        delta = b - a
        flag = "  <-- 滤波目标" if hi <= 120 else ""
        print(f"  {name:22} {a:>8.1f}dB -> {b:>8.1f}dB  ({delta:+6.1f}dB){flag}")

    print("\n[结论]")
    if floor_flt < floor_raw - 1.0:
        print(f"  噪声底下降 {floor_raw - floor_flt:.1f}dB "
              f"({floor_raw:.1f} -> {floor_flt:.1f} dB) —— 滤波生效")
    else:
        print(f"  噪声底变化 {floor_flt - floor_raw:+.1f}dB "
              f"(环境本来就安静, 滤波效果不明显)")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
