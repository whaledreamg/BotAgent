#!/usr/bin/env python3
"""分段耗时测量: 把语音链路拆成几块, 各自单独测, 定位瓶颈。

测的段落:
  1) ASR 识别      — vosk 对一段 wav 的转写耗时
  2) Agent 分流    — 规则命中 与 LLM 分类各要多久
  3) Agent 对话    — 走 chat 分支真实生成回复要多久
  4) TTS 合成      — 连接 / 首帧 / 纯合成 / 播放收尾

用法:
    conda run -n asrt python tools/profile_latency.py
    conda run -n asrt python tools/profile_latency.py --wav /path/to/16k.wav
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config  # noqa: E402
from tts_client import TTSClient  # noqa: E402


def bar(label: str, ms: int, width: int = 40) -> str:
    """把毫秒画成横条, 直观看出占比。"""
    blocks = min(width, max(0, ms // 50))
    return f"{label:<22} {ms:>7}ms | {'█' * blocks}{'·' * (width - blocks)}"


def section(title: str) -> None:
    print()
    print("=" * 68)
    print(f" {title}")
    print("=" * 68)


def bench_asr(wav_path) -> None:
    section("1) ASR 识别 (vosk 转写)")
    if not wav_path:
        print("  未提供 wav, 跳过")
        return
    try:
        from vosk import KaldiRecognizer, Model, SetLogLevel
    except ImportError:
        print("  缺少 vosk, 跳过")
        return

    SetLogLevel(-1)
    model_dir = Path.home() / ".cache" / "vosk" / "vosk-model-cn-0.22"
    if not model_dir.is_dir():
        print(f"  模型不存在: {model_dir}, 跳过")
        return

    with wave.open(wav_path, "rb") as wf:
        if wf.getframerate() != 16000:
            print(f"  wav 必须是 16kHz (当前 {wf.getframerate()}), 跳过")
            return
        raw = wf.readframes(wf.getnframes())
    dur = len(raw) / 2 / 16000

    t0 = time.time()
    model = Model(str(model_dir))
    load_ms = int((time.time() - t0) * 1000)

    rec = KaldiRecognizer(model, 16000)
    t0 = time.time()
    step = 4096 * 2
    for i in range(0, len(raw), step):
        rec.AcceptWaveform(raw[i:i + step])
    text = json.loads(rec.FinalResult()).get("text", "")
    infer_ms = int((time.time() - t0) * 1000)

    print(bar("模型加载", load_ms))
    print(bar("识别 (纯计算)", infer_ms))
    print(f"  音频时长 {dur:.2f}s, 实时率 RTF = {infer_ms / 1000 / max(dur, 0.01):.3f}")
    print(f"  识别结果: {text!r}")


def _post(url, payload, timeout=180):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def bench_agent(cfg) -> None:
    section("2) Agent 分流")
    url = cfg.agent.dispatch_url
    for text in ("打开客厅的灯", "今天天气怎么样"):
        t0 = time.time()
        try:
            body = _post(url, {"text": text, "session_id": "profile"})
        except Exception as exc:
            print(f"  [{text}] 失败: {exc}")
            continue
        rtt = int((time.time() - t0) * 1000)
        server = body.get("latency_ms", 0)
        trace = " ".join(
            f"{s.get('step')}:{s.get('ms')}ms" for s in body.get("trace", [])
        )
        print(f"  [{text}]")
        print(bar("  总往返", rtt))
        print(bar("  服务端内部", server))
        print(bar("  网络+解析", rtt - server))
        print(f"    intent={body.get('intent')} speak={body.get('speak')}")
        print(f"    trace: {trace}")
        print()


def bench_chat(cfg) -> None:
    section("3) Agent 对话 (chat 分支, 真实调 LLM)")
    url = cfg.agent.dispatch_url
    t0 = time.time()
    try:
        body = _post(url, {"text": "讲个笑话", "session_id": "profile-chat"})
    except Exception as exc:
        print(f"  失败: {exc}")
        return
    rtt = int((time.time() - t0) * 1000)
    server = body.get("latency_ms", 0)
    print(bar("总往返", rtt))
    print(bar("服务端内部", server))
    print(bar("网络+解析", rtt - server))
    print(f"  回复: {body.get('reply', '')[:80]!r}")


def bench_tts(cfg) -> None:
    section("4) TTS 合成")
    client = TTSClient(cfg.tts)
    try:
        result = client.speak("这是一段用于测速的语音合成文本，用来观察各阶段耗时。")
    except Exception as exc:
        print(f"  失败: {exc}")
        client.close()
        return
    print(bar("总计", result.duration_ms))
    print(bar("  连接", result.connect_ms))
    print(bar("  首帧", result.first_frame_ms))
    print(bar("  合成", result.synth_ms))
    print(bar("  播放收尾", result.play_ms))
    print(bar("  音频时长", result.audio_ms))
    if result.server_total_ms:
        print(bar("  服务端总计", result.server_total_ms))
        print(bar("  服务端排队", result.server_queued_ms))
    print(f"  {result.frames} 帧 / {result.audio_bytes} 字节 / played={result.played}")
    client.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--wav", default=None, help="16kHz 单声道 wav, 用于测 ASR")
    p.add_argument("--skip", default="", help="逗号分隔: asr,agent,chat,tts")
    args = p.parse_args()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    cfg = load_config()
    print("配置:")
    print(f"  Agent: {cfg.agent.dispatch_url}")
    print(f"  TTS  : {cfg.tts.ws_url}")
    print(f"  超时 : agent={cfg.agent.timeout}s tts={cfg.tts.timeout}s")

    if "asr" not in skip:
        bench_asr(args.wav)
    if "agent" not in skip:
        bench_agent(cfg)
    if "chat" not in skip:
        bench_chat(cfg)
    if "tts" not in skip:
        bench_tts(cfg)

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())