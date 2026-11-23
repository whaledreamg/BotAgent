#!/usr/bin/env python3
"""对比两个音色的实际波形差异。

同一个文本, 分别用两个音色合成, 比较:
- 音频时长
- 基频 (F0) —— 不同音色的 F0 差异最直观
- 频谱包络相似度

用法::

    python services/asr_service/tools/compare_voices.py --a Default --b Timbre3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import numpy as np

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

# 复用客户端自己的帧解析 —— TTS 服务端发的是**带帧头的二进制协议**,
# 不是裸 PCM。
from tts_client import (  # noqa: E402
    FRAME_TYPE_AUDIO_DATA,
    FRAME_TYPE_AUDIO_END,
    unpack_frame,
)
from websockets import connect  # noqa: E402

SAMPLE_RATE = 24000


async def synth(url: str, text: str, voice: str) -> np.ndarray:
    """用指定音色合成, 返回 PCM (float32 单声道)。"""
    req = {
        "type": "tts",
        "request_id": f"cmp-{voice}",
        "text": text,
        "format": "pcm",
        "stream": True,
        "play": False,
        "voice": voice,
    }
    chunks = []
    # websockets >= 14 的 connect() 是异步 API, 不支持 `with` 语法
    async with connect(url, max_size=None, open_timeout=10) as ws:
        await ws.send(json.dumps(req, ensure_ascii=False))
        while True:
            msg = await ws.recv()
            if isinstance(msg, str):
                data = json.loads(msg)
                if data.get("type") == "error":
                    raise RuntimeError(data.get("message", "TTS error"))
                if data.get("type") == "done":
                    break
                continue

            # 二进制 = 带帧头的协议帧, 剥掉帧头才是 PCM
            ftype, _flags, _seq, _sr, _rid, payload = unpack_frame(msg)
            if ftype == FRAME_TYPE_AUDIO_DATA and payload:
                chunks.append(np.frombuffer(payload, dtype="<i2"))
            elif ftype == FRAME_TYPE_AUDIO_END:
                continue
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def estimate_f0(x: np.ndarray, sr: int = SAMPLE_RATE) -> float:
    """用自相关估基频 (只看浊音段)。"""
    # 只取中间 60% (避开首尾静音)
    n = len(x)
    a = x[int(n * 0.2): int(n * 0.8)]
    if len(a) < sr // 2:
        return 0.0
    a = a - a.mean()
    a *= np.hanning(len(a))

    spec = np.fft.rfft(a)
    ac = np.fft.irfft(spec * np.conj(spec))[: len(a)]
    ac /= (ac[0] + 1e-12)

    # 在 60~400Hz 范围找峰值
    lo = int(sr / 400)
    hi = min(int(sr / 60), len(ac) - 1)
    if hi <= lo:
        return 0.0
    peak = int(np.argmax(ac[lo:hi])) + lo
    if ac[peak] < 0.3:      # 相关性太低 = 没找到浊音
        return 0.0
    return sr / peak


def describe(x: np.ndarray, name: str) -> dict:
    dur = len(x) / SAMPLE_RATE
    rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
    f0 = estimate_f0(x)
    # 频谱质心
    if len(x):
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        freqs = np.fft.rfftfreq(len(x), 1.0 / SAMPLE_RATE)
        centroid = float(np.sum(spec * freqs) / (np.sum(spec) + 1e-12))
    else:
        centroid = 0.0
    return {
        "name": name,
        "duration": dur,
        "rms": rms,
        "f0": f0,
        "centroid": centroid,
        "wave": x,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="对比两个音色")
    ap.add_argument("--url", default="ws://127.0.0.1:8000/ws")
    ap.add_argument("--a", default="Default")
    ap.add_argument("--b", default="Timbre3")
    ap.add_argument("--text", default="你好，今天天气真不错，我们出去走走吧。")
    args = ap.parse_args()

    print("=" * 66)
    print(f"音色对比: {args.a}  vs  {args.b}")
    print(f"文本: {args.text}")
    print("=" * 66)

    results = []
    for name in (args.a, args.b):
        try:
            # 每个音色用独立的新事件循环 —— 同一个 loop 跑两次 asyncio.run
            # 会在第二次报 "__enter__" (loop 已被关闭)。
            wav = asyncio.run(synth(args.url, args.text, name))
        except Exception as exc:
            print(f"[合成失败] {name}: {exc}")
            return 1
        d = describe(wav, name)
        results.append(d)
        print(f"\n[{name}]")
        print(f"  时长     = {d['duration']:.2f}s")
        print(f"  RMS      = {d['rms']:.4f}")
        print(f"  基频 F0  = {d['f0']:.1f} Hz" if d["f0"] else "  基频 F0  = (未检出浊音)")
        print(f"  频谱质心 = {d['centroid']:.0f} Hz")

    if len(results) == 2:
        a, b = results
        # 基频差异
        if a["f0"] and b["f0"]:
            diff = b["f0"] - a["f0"]
            ratio = b["f0"] / a["f0"] if a["f0"] else 0
            print("\n[结论]")
            print(f"  基频差异 = {diff:+.1f} Hz (比值 {ratio:.3f})")
            if abs(diff) > 8:
                print(f"  -> 两个音色明显不同 ✓ ({args.b} 已生效)")
            else:
                print(f"  -> 基频接近, 但音色仍可能不同 (同一说话人换 seed 会这样)")
        else:
            print("\n[结论] 未检出稳定基频, 建议直接听两段音频对比")

    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
