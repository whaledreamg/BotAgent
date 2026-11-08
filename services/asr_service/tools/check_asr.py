#!/usr/bin/env python3
"""直接用 vosk 识别一个 wav, 便于排查唤醒词没识别出来的问题。

用法:
    conda run -n asrt python tools/check_asr.py /tmp/asr_e2e_test_16k.wav
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vosk import KaldiRecognizer, Model, SetLogLevel

SetLogLevel(-1)

MODEL_DIR = str(Path.home() / ".cache" / "vosk" / "vosk-model-cn-0.22")
SAMPLE_RATE = 16000


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: check_asr.py <wav>")
        return 1
    path = Path(sys.argv[1])

    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != SAMPLE_RATE or wf.getnchannels() != 1:
            print(
                f"警告: wav 必须是 {SAMPLE_RATE}Hz 单声道, "
                f"当前 {wf.getframerate()}Hz {wf.getnchannels()}ch"
            )
        raw = wf.readframes(wf.getnframes())

    model = Model(MODEL_DIR)
    rec = KaldiRecognizer(model, SAMPLE_RATE)
    rec.SetWords(True)

    texts = []
    step = 4096 * 2
    for i in range(0, len(raw), step):
        chunk = raw[i:i + step]
        if rec.AcceptWaveform(chunk):
            texts.append(json.loads(rec.Result()).get("text", ""))
    texts.append(json.loads(rec.FinalResult()).get("text", ""))

    print("分句结果:")
    for t in texts:
        if t:
            print(f"  - {t}")
    full = "".join(texts)
    print(f"完整: {full}")

    for wake in ("你好助手", "你好", "助手"):
        print(f"包含 [{wake}]: {wake in full}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
