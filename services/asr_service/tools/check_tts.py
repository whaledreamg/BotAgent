#!/usr/bin/env python3
"""独立验证脚本: 直连 ChatTTS, 合成一段中文并统计收到的 PCM。

用法:
    conda run -n asrt python tools/check_tts.py --text "你好，世界"

不播放, 只统计帧数与字节数, 用于在无音频设备的环境下验证 TTS 链路。
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tts_client import (
    FRAME_TYPE_AUDIO_DATA,
    FRAME_TYPE_AUDIO_END,
    TTSError,
    unpack_frame,
)

MAGIC = b"TTS1"
HEADER_STRUCT = struct.Struct("<4sBBIIHH")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--text", default="你好, 这是一段测试语音。")
    p.add_argument("--url", default="ws://127.0.0.1:8000/ws")
    p.add_argument("--voice", default="Default")
    args = p.parse_args()

    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        print(f"缺少 websockets 依赖: {exc}")
        return 1

    frames = 0
    total_bytes = 0
    sample_rate = 0
    req_id = "check-tts"

    try:
        with connect(args.url, max_size=None, open_timeout=10) as ws:
            ws.send(
                json.dumps(
                    {
                        "type": "tts",
                        "request_id": req_id,
                        "text": args.text,
                        "voice": args.voice,
                        "speed": 5,
                        "format": "pcm",
                        "stream": True,
                        "play": False,
                    },
                    ensure_ascii=False,
                )
            )
            while True:
                message = ws.recv(timeout=180)
                if isinstance(message, str):
                    print("  事件:", message.strip())
                    continue
                ftype, flags, seq, rate, rid, payload = unpack_frame(message)
                if ftype == FRAME_TYPE_AUDIO_END:
                    print(f"  收到 AUDIO_END (seq={seq})")
                    break
                if ftype == FRAME_TYPE_AUDIO_DATA:
                    frames += 1
                    total_bytes += len(payload)
                    sample_rate = rate
    except Exception as exc:
        print(f"TTS 连接/合成失败: {exc}")
        return 1

    duration = total_bytes / 2 / sample_rate if sample_rate else 0
    print(
        f"OK: {frames} 帧, {total_bytes} 字节, {sample_rate}Hz, "
        f"约 {duration:.2f} 秒"
    )
    return 0 if frames > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
