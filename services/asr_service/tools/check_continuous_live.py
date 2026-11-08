#!/usr/bin/env python3
"""验证: 连续对话的端到端行为 (真实模型 + 真实 Agent/TTS)。

做法: 用 --no-wake 启动一个 ASR 实例, 回放一段「两段语音 + 中间间隔」
的音频 (走的是和真实麦克风一样的 process_block 路径)。

喂两段语音, 中间不唤醒:
- 第一段: 靠 --no-wake 进入
- 第二段: 因为在连续对话窗口内, 应该**不需要唤醒词**也能识别

用法:
    conda run -n asrt python tools/check_continuous_live.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from config import load_config  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


def build_audio() -> Path:
    """造一段「两段语音 + 中间间隔」的音频。"""
    src = Path(
        "/home/gxh/workspace/project/ASRT/recordings/rec_20261106_182400.wav"
    )
    if not src.is_file():
        src = Path(
            "/home/gxh/workspace/project/ASRT/recordings/rec_20261106_152720.wav"
        )
    with wave.open(str(src)) as w:
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    arr = np.frombuffer(raw, dtype=np.int16).copy()
    seg = arr[int(1.0 * rate): int(7.0 * rate)]
    # 间隔要够长: 第一轮的 Agent + TTS 播报大约要 10~15 秒。
    # 间隔太短 -> 第二段语音在上一轮还在播报时到达, 被 busy 挡掉;
    # 间隔太长 -> 超出连续对话窗口 (30s), 变成需要重新唤醒。
    # 14 秒是这两者之间的折中。
    gap = np.zeros(int(14.0 * rate), dtype=np.int16)
    out = np.concatenate([seg, gap, seg])
    path = Path("/tmp/continuous_live.wav")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(out.tobytes())
    return path


def _pick_port() -> int:
    """找一个当前没人监听的端口。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    cfg = load_config()
    timeout = cfg.asr.continuous_timeout
    wav_path = build_audio()
    print(f"测试音频: {wav_path}")
    print(f"连续对话窗口: {timeout}s")

    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(Path.home()),
    }
    # 选一个没人用的端口, 避免和已经在跑的 ASR 实例抢
    port = _pick_port()
    print(f"测试实例端口: {port}")
    # 本脚本已经在 asrt 环境里跑, 直接用当前解释器起子进程即可,
    # 不必再套一层 conda run。
    proc = subprocess.Popen(
        [
            sys.executable,
            "server.py",
            "--replay", str(wav_path),
            "--no-wake",
            "--keep-alive", "90",
            "--continuous-timeout", str(timeout),
            # 真实时间回放: 加速回放时第二段语音会在第一轮还没跑完时就到了
            "--replay-speed", "1.0",
            "--port", str(port),
        ],
        cwd=str(ROOT / "services" / "asr_service"),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )

    out_lines = []

    def reader():
        for line in proc.stdout:
            out_lines.append(line)
            sys.stdout.write("  | " + line)
            sys.stdout.flush()

    threading.Thread(target=reader, daemon=True).start()

    print("\n等待 ASR 服务就绪 ...")
    ready = False
    for _ in range(60):
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2
            ).read()
            ready = True
            break
        except Exception:
            time.sleep(1)
    if not ready:
        print("[FAIL] ASR 服务没起来")
        proc.kill()
        return 1

    print("\n等待两轮对话处理完成 ...")
    turns = 0
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/state", timeout=3
            ) as r:
                d = json.loads(r.read())
            turns = d.get("turns", 0)
            if turns >= 2:
                break
        except Exception:
            pass
        time.sleep(1)

    time.sleep(2)
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    text = "".join(out_lines)

    print("\n" + "=" * 60)
    print("断言:")
    ok = True

    has_session = "连续对话] 已开启" in text
    print(f"  [{'PASS' if has_session else 'FAIL'}] 连续对话窗口已开启")
    ok = ok and has_session

    has_cont = "连续对话] 检测到语音" in text
    print(f"  [{'PASS' if has_cont else 'FAIL'}] 第二段语音无需唤醒词即被捕获")
    ok = ok and has_cont

    print(f"  [{'PASS' if turns >= 2 else 'FAIL'}] 完成 {turns} 轮对话 (期望 >= 2)")
    ok = ok and turns >= 2

    expired = "连续对话] 已关闭" in text
    print(f"  [{'PASS' if not expired else 'FAIL'}] 窗口未过期")
    ok = ok and not expired

    print("=" * 60)
    print("结果: 全部通过" if ok else "结果: 有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
