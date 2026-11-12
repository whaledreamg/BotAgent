#!/usr/bin/env python3
"""回声抑制自检 —— 验证 TTS 播报不会被自己当成用户说话。

## 要验证什么

场景: 机器人播报「没听清内容，请再说一遍」, 麦克风把这段播报录了进去。
如果不做回声抑制, 下一轮就会把「没听清内容请在说起」当成用户说的话 ——
表现为机器人自己跟自己对话。

本脚本**不接麦克风**, 用合成的音频块模拟:
1. 播报期间 (高电平)   -> 必须被守卫挡住, 不触发录音
2. 播报余音 (中电平)   -> 必须被挡住
3. 房间安静 (低电平)   -> 允许通过
4. 用户真的说话 (高电平) -> 必须能触发录音

## 用法

    conda run -n asrt python tools/check_echo_guard.py
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import time
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config  # noqa: E402

SAMPLE_RATE = 16000
BLOCK_SIZE = 4096  # 16kHz 下约 256ms 一块


def make_block(amplitude: float, freq: float = 220.0) -> bytes:
    """合成一块指定幅度的正弦波 (int16 单声道)。"""
    n = BLOCK_SIZE
    samples = [
        int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        for i in range(n)
    ]
    return struct.pack(f"<{n}h", *samples)


LOUD = make_block(0.30)     # 播报声 (~-20dB)
MEDIUM = make_block(0.05)   # 余音/混响 (~-32dB)
QUIET = make_block(0.001)   # 房间底噪 (~-66dB)

PASSED: List[str] = []
FAILED: List[str] = []


def check(desc: str, ok: bool, detail: str = "") -> bool:
    if ok:
        PASSED.append(desc)
        print(f"  [PASS] {desc}" + (f"  {detail}" if detail else ""))
    else:
        FAILED.append(desc)
        print(f"  [FAIL] {desc}  {detail}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="回声抑制自检")
    ap.add_argument("--settle", type=float, default=None,
                    help="覆盖沉降期秒数 (默认取配置)")
    args = ap.parse_args()

    cfg = load_config()
    settle = args.settle if args.settle is not None else cfg.asr.echo_settle

    print("=" * 60)
    print("回声抑制自检")
    print("=" * 60)
    print()
    print(f"  沉降期: {settle}s")
    print()

    # --------------------------------------------------------------
    # 不启动真实的 vosk 模型, 直接用 VoiceASR 的逻辑做单元级验证。
    # 构造一个最小可用的替身, 只借用 process_block / _voice_detected
    # 这两个方法的回声相关行为。
    # --------------------------------------------------------------

    class FakeASR:
        """复用 server.VoiceASR 的回声逻辑, 但跳过 vosk 模型加载。"""

        def __init__(self, settle: float, noise_floor: float = -55.0) -> None:
            self.echo_settle_sec = settle
            self.echo_guard_until = 0.0
            self.echo_reject_until = 0.0
            self.echo_quiet_at = 0.0
            self.noise_floor = noise_floor
            self._voice_run = 0
            self._quiet_run = 0
            self.echo_quiet_timeout = 2.0
            self._session_started_ts = 0.0
            self.args = type("A", (), {"vad_threshold": -45.0})()
            # 借用真实实现
            from server import VoiceASR
            self._voice_detected = VoiceASR._voice_detected.__get__(self)
            self.process_block = VoiceASR.process_block.__get__(self)

        # 借用回声守卫的两个方法
        from server import VoiceASR as _V

        arm_echo_guard = _V.arm_echo_guard
        finalize_echo_guard = _V.finalize_echo_guard

    asr = FakeASR(settle)

    # ---- 场景 1: 播报期间, 高电平必须被挡住 ----
    print("[场景 1] 播报期间 (高电平)")
    asr.arm_echo_guard(settle)
    triggered = False
    t0 = time.time()
    # 模拟播报持续 1 秒 (4 块)
    for _ in range(4):
        if asr._voice_detected(LOUD, time.time()):
            triggered = True
    check("播报期间不触发人声检测", not triggered)
    check("守卫仍然架着", asr.echo_guard_until > time.time(),
          f"剩余 {asr.echo_guard_until - time.time():.2f}s")

    # ---- 场景 2: 播报结束 + 余音, 仍必须被挡住 ----
    print()
    print("[场景 2] 播报结束后的余音 (中电平)")
    asr.finalize_echo_guard(settle)
    triggered = False
    for _ in range(4):
        if asr._voice_detected(MEDIUM, time.time()):
            triggered = True
    check("余音期间不触发人声检测", not triggered)
    check("余音仍在拒绝期", asr.echo_reject_until > time.time(),
          f"剩余 {asr.echo_reject_until - time.time():.2f}s")

    # ---- 场景 3: 房间安静下来, 解除拒绝 ----
    print()
    print("[场景 3] 房间安静 (低电平)")
    before = asr.echo_reject_until
    from server import ECHO_QUIET_BLOCKS

    # 一块安静**不够** —— 余音是断断续续的, 间隙里电平会掉下去。
    # 必须连续安静 ECHO_QUIET_BLOCKS 块才认账。
    asr._voice_detected(QUIET, time.time())
    check("一块安静不足以解除拒绝", asr.echo_reject_until != 0.0,
          f"拒绝期还在: {asr.echo_reject_until - time.time():.2f}s")

    for _ in range(ECHO_QUIET_BLOCKS - 1):
        asr._voice_detected(QUIET, time.time())
    check(f"连续安静 {ECHO_QUIET_BLOCKS} 块后解除拒绝", asr.echo_reject_until == 0.0,
          f"{before:.2f} -> {asr.echo_reject_until}")

    # ---- 场景 3b: 余音间隙的短暂安静不能解除拒绝 ----
    print()
    print("[场景 3b] 余音间隙的短暂安静")
    asr2 = FakeASR(settle)
    asr2.arm_echo_guard(settle)
    asr2.finalize_echo_guard(settle)
    # 响 -> 静 -> 响 -> 静 (模拟余音起伏)
    for blk in (LOUD, QUIET, LOUD, QUIET):
        asr2._voice_detected(blk, time.time())
    check("余音起伏中不解除拒绝", asr2.echo_reject_until != 0.0)

    # ---- 场景 4: 用户说话必须能触发 ----
    print()
    print("[场景 4] 用户真的说话 (高电平)")
    asr._voice_run = 0
    triggered = False
    for _ in range(3):
        if asr._voice_detected(LOUD, time.time()):
            triggered = True
            break
    check("用户说话能触发录音", triggered)

    # ---- 场景 5: 守卫期内 process_block 直接丢弃 ----
    print()
    print("[场景 5] 守卫期内音频被 process_block 丢弃")
    asr.arm_echo_guard(settle)
    asr.state = "IDLE"
    asr._session_until = time.time() + 30
    asr._waiting_speech = True
    asr.in_continuous_session = property(lambda self: True)
    asr.pipeline = type("P", (), {"busy": False})()
    # 守卫期内调用 —— 应该直接返回 False (不录音)
    ret = asr.process_block(LOUD, time.time())
    check("守卫期内不进入录音", ret is False)
    check("仍在等待用户开口", asr._waiting_speech is True)

    # ---- 场景 6: 硬上限: 一直很吵也不能永远拒绝 ----
    print()
    print("[场景 6] 硬上限 (环境一直很吵)")
    asr2 = FakeASR(settle)
    asr2.arm_echo_guard(settle)
    asr2.finalize_echo_guard(settle)
    # 把拒绝期设成过去 —— 模拟已经超时
    asr2.echo_reject_until = time.time() - 0.01
    asr2._voice_run = 0
    triggered = False
    for _ in range(3):
        if asr2._voice_detected(LOUD, time.time()):
            triggered = True
            break
    check("超过等待上限后强制恢复", triggered)

    # --------------------------------------------------------------
    print()
    print("=" * 60)
    print(f"通过 {len(PASSED)}, 失败 {len(FAILED)}")
    if FAILED:
        print("\n失败项:")
        for name in FAILED:
            print(f"  - {name}")
    print("=" * 60)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
