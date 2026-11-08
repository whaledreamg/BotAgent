#!/usr/bin/env python3
"""验证: 连续对话窗口的行为。

覆盖:
1. 窗口内不需要唤醒词, 检测到人声就进入录音
2. 窗口过期后必须重新唤醒
3. 窗口计时从「播报结束」开始 (TTS 播放期间不计入)
4. 前滚音频排除了 TTS 播放期间录到的声音

用法:
    conda run -n asrt python tools/check_continuous.py
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

import server as server_mod  # noqa: E402
from config import load_config  # noqa: E402
from pipeline import Pipeline  # noqa: E402


def make_silence(ms: int = 512) -> bytes:
    """静音块 (16kHz / 16bit)。"""
    n = int(16000 * ms / 1000)
    return (np.zeros(n, dtype=np.int16)).tobytes()


def make_speech(ms: int = 512, freq: float = 220.0) -> bytes:
    """有声音的块 (用正弦波模拟人声)。

    freq 用来区分不同来源的音频 —— 测试里要判断「前滚里有没有
    混入 TTS 播放期的那一段」, 两段音频必须字节不同。
    """
    n = int(16000 * ms / 1000)
    t = np.arange(n) / 16000.0
    wave = 0.25 * np.sin(2 * np.pi * freq * t)
    return (wave * 32767).astype(np.int16).tobytes()


def dummy_rec():
    """不加载 vosk 的识别器替身。"""
    return types.SimpleNamespace(
        AcceptWaveform=lambda d: False,
        Result=lambda: "{}",
        PartialResult=lambda: "{}",
    )


class FakeASR:
    """不加载 vosk 模型的极简替身, 只保留 VoiceASR 关心的字段与行为。"""

    def __init__(self, args):
        self.args = args
        self.state = "IDLE"
        self.tokens = {"小助手": ["小", "助手"]}
        self.grammar = json.dumps(["小", "助手"])
        self.grammar_supported = False
        self.ring = []
        self.noise_floor = -55.0
        self.preroll_sec = 0.5
        self.min_record_sec = 1.0
        self.kw_rec = None
        self.gen_rec = dummy_rec()
        self.kw_text = ""
        self.gen_text = ""
        self.rec_chunks = []
        self.rec_start_ts = 0.0
        self.last_voice_ts = 0.0
        self.final_texts = []
        self.partial_text = ""
        self.asr_rec = None

    def _feed_detectors(self, data):
        return None

    def check_wake(self, data):
        return None

    def start_recording(self, ts, wake):
        self.state = "RECORDING"
        self.rec_start_ts = ts
        self.last_voice_ts = ts
        self.rec_chunks = []
        self.final_texts = []
        self.partial_text = ""
        self.asr_rec = types.SimpleNamespace(AcceptWaveform=lambda d: False)
        # 前滚: 基类的行为
        preroll = []
        for rts, chunk in list(self.ring):
            if ts - rts <= self.preroll_sec:
                preroll.append(chunk)
        for chunk in preroll:
            self.rec_chunks.append(chunk)

    def process_block(self, data, ts):
        if self.state == "IDLE":
            return False
        return False

    def stop_recording(self, reason):
        self.state = "IDLE"

    def update_noise_floor(self, db):
        if db < self.noise_floor:
            self.noise_floor = db
        else:
            self.noise_floor = self.noise_floor * 0.995 + db * 0.005

    def _print_line(self, *a, **kw):
        pass

    def print_level(self, db):
        pass


@contextlib.contextmanager
def patched_vosk():
    """把 vosk 的模型加载与识别器创建都替换成替身。

    补丁必须在整个测试期间保持生效 —— KaldiRecognizer 是在
    开始录音的那一刻才创建的, 构造完就还原会漏掉。
    """
    original_init = server_mod.VoiceASR.__init__
    original_rec = server_mod.wakeup_asr.KaldiRecognizer

    def patched_init(self, a, p):
        self.pipeline = p
        self.skip_wake = bool(getattr(a, "skip_wake", False))
        self.continuous_timeout = float(getattr(a, "continuous_timeout", 0.0) or 0.0)
        self._session_until = None
        self._session_started_ts = 0.0
        self._waiting_speech = False
        self._voice_run = 0
        self.model = None
        FakeASR.__init__(self, a)

    def patched_rec(model, rate, grammar=None):
        return dummy_rec()

    server_mod.VoiceASR.__init__ = patched_init
    server_mod.wakeup_asr.KaldiRecognizer = patched_rec
    try:
        yield
    finally:
        server_mod.VoiceASR.__init__ = original_init
        server_mod.wakeup_asr.KaldiRecognizer = original_rec


def build_voice_asr(cfg, timeout: float) -> server_mod.VoiceASR:
    """构造一个不加载模型的 VoiceASR 实例 (需在 patched_vosk 上下文内)。"""
    args = types.SimpleNamespace(
        wake="小助手",
        model=None,
        device=None,
        duration=30.0,
        silence=1.2,
        vad_threshold=-45.0,
        replay=None,
        skip_wake=False,
        save=False,
        quiet=True,
        continuous_timeout=timeout,
    )
    pipeline = Pipeline(cfg)
    return server_mod.VoiceASR(args, pipeline)


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def main() -> int:
    cfg = load_config()
    results = []

    with patched_vosk():
        # ---------------------------------------------------- 1. 窗口内直接录音
        print("\n1) 窗口内不需要唤醒词")
        app = build_voice_asr(cfg, timeout=30.0)
        app.open_session()
        ts = time.time()
        app.ring.append((ts, make_silence()))
        # 先喂一块静音: 不应该开始录音
        app.process_block(make_silence(), ts)
        still_idle = app.state == "IDLE"
        results.append(check("静音时不进入录音", still_idle, f"state={app.state}"))

        # 再喂两块有声: 应该开始录音
        app.process_block(make_speech(), ts + 0.3)
        app.process_block(make_speech(), ts + 0.6)
        started = app.state == "RECORDING"
        results.append(check("检测到人声后进入录音", started, f"state={app.state}"))

        # ---------------------------------------------------- 2. 窗口过期
        print("\n2) 窗口过期后需要重新唤醒")
        app2 = build_voice_asr(cfg, timeout=30.0)
        app2.open_session()
        # 手动把窗口推到过去
        app2._session_until = time.time() - 1
        ts2 = time.time()
        app2.ring.append((ts2, make_silence()))
        app2.process_block(make_speech(), ts2)
        app2.process_block(make_speech(), ts2 + 0.3)
        expired_idle = app2.state == "IDLE"
        results.append(check("过期后不自动录音", expired_idle, f"state={app2.state}"))
        results.append(check("过期后窗口被关闭", app2._session_until is None))

        # ---------------------------------------------------- 3. 计时起点
        print("\n3) 窗口从播报结束开始计时")
        app3 = build_voice_asr(cfg, timeout=30.0)
        before = time.time()
        app3.open_session()
        after = time.time()
        opened_in_range = app3._session_until is not None and (
            before + 30.0 - 0.5 <= app3._session_until <= after + 30.0 + 0.5
        )
        results.append(
            check("窗口到期时间 = 开启时刻 + 30s", opened_in_range,
                  f"remaining={app3._session_remaining():.1f}s")
        )

        # ---------------------------------------------------- 4. 前滚排除 TTS 音频
        print("\n4) 前滚排除 TTS 播放期间的音频")
        app4 = build_voice_asr(cfg, timeout=30.0)
        t0 = time.time()
        # 先模拟 TTS 播放期间录到的音频 (早于窗口起点)
        tts_audio = make_speech(freq=180.0)
        app4.ring.append((t0, tts_audio))
        app4.open_session()          # 窗口在 TTS 之后开启
        ts4 = time.time()
        user_audio = make_speech(freq=320.0)
        app4.ring.append((ts4, user_audio))
        app4._waiting_speech = False  # 直接进入录音, 不做 VAD 判定
        app4._start_continuous_recording(ts4)
        has_tts_audio = any(c == tts_audio for c in app4.rec_chunks)
        has_user_audio = any(c == user_audio for c in app4.rec_chunks)
        results.append(
            check("前滚里没有 TTS 播放期的音频", not has_tts_audio,
                  f"chunks={len(app4.rec_chunks)}")
        )
        results.append(
            check("前滚里保留了窗口内的用户语音", has_user_audio)
        )

        # ---------------------------------------------------- 5. 关闭功能
        print("\n5) continuous_timeout=0 时关闭连续对话")
        app5 = build_voice_asr(cfg, timeout=0.0)
        app5.open_session()
        results.append(check("timeout=0 不开启窗口", app5._session_until is None))
        results.append(check("timeout=0 不在连续会话中", not app5.in_continuous_session))

    print()
    if all(results):
        print(f"结果: 全部通过 ({len(results)} 项)")
        return 0
    failed = len(results) - sum(results)
    print(f"结果: {failed} 项失败")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
