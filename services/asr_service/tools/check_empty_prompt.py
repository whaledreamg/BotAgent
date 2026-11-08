#!/usr/bin/env python3
"""验证: 没识别到内容时, 会用 TTS 播报提示语 (而不是只记一条错误日志)。

用法:
    conda run -n asrt python tools/check_empty_prompt.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config          # noqa: E402
from pipeline import Pipeline, State    # noqa: E402


def main() -> int:
    cfg = load_config()
    prompt = (cfg.asr.empty_result_prompt or "").strip()
    print(f"配置的提示文案: {prompt!r}")

    if not prompt:
        print("[SKIP] empty_result_prompt 为空, 不播报")
        return 0

    pipeline = Pipeline(cfg)

    # 拦截 TTS 调用, 只看有没有发起播报, 不真的出声
    spoken = []
    original_speak = pipeline.tts.speak

    def fake_speak(text, request_id=None):
        spoken.append(text)
        from tts_client import TTSResult

        return TTSResult(duration_ms=1, audio_bytes=0, frames=0, played=False)

    pipeline.tts.speak = fake_speak  # type: ignore[assignment]

    print("提交空文本 ...")
    pipeline.submit("")

    deadline = time.time() + 10
    while time.time() < deadline and pipeline.state != State.IDLE:
        time.sleep(0.05)

    pipeline.tts.speak = original_speak  # type: ignore[assignment]

    if not spoken:
        print("[FAIL] 没有发起 TTS 播报")
        print(f"       errors={pipeline.errors}")
        return 1

    if spoken[0] != prompt:
        print(f"[FAIL] 播报内容不对: {spoken[0]!r} != {prompt!r}")
        return 1

    print(f"[PASS] 已播报: {spoken[0]!r}")

    # 错误记录里存的话必须和播报的话一致 ——
    # 否则会出现「日志说一套、机器人说另一套」, 排查时容易误判。
    if not pipeline.errors:
        print("[FAIL] errors 里没有记录")
        return 1
    if pipeline.errors[-1] != prompt:
        print(f"[FAIL] errors 记录与播报内容不一致:")
        print(f"       errors={pipeline.errors[-1]!r}")
        print(f"       播报  ={prompt!r}")
        return 1

    print(f"[PASS] errors 记录与播报一致: {pipeline.errors[-1]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
