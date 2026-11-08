#!/usr/bin/env python3
"""端到端验证 TTS 服务的首帧延迟 (走真实的 WebSocket 协议)。

用法:
    /home/gxh/miniconda3/envs/asrt/bin/python tools/bench_tts_ws.py
"""

from __future__ import annotations

import json
import sys
import time

from websockets.sync.client import connect

WS_URL = "ws://127.0.0.1:8000/ws"
TEXTS = [
    "你好，今天天气不错。",
    "北京今天天气晴，气温 24 度。",
    "没听清内容，请再说一遍。",
    "建议您打开手机自带的天气应用查看。",
]


def main() -> int:
    firsts = []
    totals = []
    for i, text in enumerate(TEXTS):
        t0 = time.time()
        first_at = None
        total_ms = -1
        with connect(WS_URL, max_size=None, open_timeout=10) as ws:
            ws.send(json.dumps({
                "type": "tts", "request_id": f"b{i}", "text": text,
                "format": "pcm", "stream": True, "play": False,
            }, ensure_ascii=False))
            while True:
                msg = ws.recv(timeout=60)
                if isinstance(msg, str):
                    obj = json.loads(msg)
                    if obj.get("type") == "done":
                        total_ms = int(obj["duration_ms"])
                        break
                    if obj.get("type") == "error":
                        print(f"  错误: {obj}")
                        break
                    continue
                if first_at is None:
                    first_at = time.time()
        first_ms = int((first_at - t0) * 1000) if first_at else -1
        if i == 0:
            print(f"(预热, 不计入统计)")
        else:
            firsts.append(first_ms)
            totals.append(total_ms)
        print(f"  {text!r}: 首帧 {first_ms}ms | 服务端总计 {total_ms}ms")

    n = len(firsts)
    print()
    print(f"平均首帧: {sum(firsts) // n}ms")
    print(f"平均总计: {sum(totals) // n}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
