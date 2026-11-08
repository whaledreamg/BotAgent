#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音助手: 唤醒词 -> 语音识别 -> Agnes 多模态模型对话。

在 wakeup_asr.py 的基础上接入 Agnes AI 模型, 识别出的文本会发给模型,
并把回复朗读出来(可选, 依赖 espeak-ng/pico2wave)。

用法:
    # 语音对话 (纯文本回复)
    python voice_assistant.py --wake "你好助手"

    # 边说边出字 (流式)
    python voice_assistant.py --wake "你好助手" --stream

    # 多模态: 唤醒后同时拍一张摄像头照片一起送给模型
    python voice_assistant.py --wake "你好助手" --camera 0

    # 开启 Thinking 模式
    python voice_assistant.py --wake "你好助手" --thinking

    # 用 wav 回放测试, 不占麦克风
    python voice_assistant.py --replay test.wav --wake "你好助手"
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wakeup_asr
from agnes_client import AgnesClient, AgnesError

DEFAULT_SYSTEM = (
    "你是一个中文语音助手。用户通过语音与你对话, "
    "请用简洁、口语化的中文回答, 通常不超过三句话。"
    "如果用户的问题需要看图, 请结合图像内容回答。"
)


class VoiceAssistant(wakeup_asr.WakeupASR):
    """带 LLM 对话的语音助手。"""

    def __init__(self, args):
        super().__init__(args)
        self.client = AgnesClient(model=args.llm_model, verbose=args.verbose)
        self.history = []
        self.camera = None
        self.speak = args.speak

    # ------------------------------------------------------------ 摄像头

    def open_camera(self):
        """打开摄像头设备, 供多模态提问时抓帧。"""
        dev = self.args.camera
        try:
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "v4l2", "-i", f"/dev/video{dev}",
                 "-frames:v", "1", "-q:v", "2", "-y",
                 str(Path(tempfile.gettempdir()) / f"_agnes_cam_{dev}.jpg")],
                capture_output=True, timeout=15,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.decode(errors="ignore")[:300])
            self.camera = Path(tempfile.gettempdir()) / f"_agnes_cam_{dev}.jpg"
            self._log(f"摄像头 /dev/video{dev} 就绪")
        except FileNotFoundError:
            self._log("未找到 ffmpeg, 跳过摄像头功能")
        except Exception as exc:
            self._log(f"摄像头打开失败: {exc}")

    def capture_frame(self):
        """拍一张当前画面。"""
        if self.camera is None or not self.camera.exists():
            return None
        try:
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "v4l2", "-i", f"/dev/video{self.args.camera}",
                 "-frames:v", "1", "-q:v", "2", "-y", str(self.camera)],
                capture_output=True, timeout=15,
            )
            if proc.returncode != 0:
                return None
            return self.camera
        except Exception:
            return None

    # ------------------------------------------------------------ 对话

    def on_recording_complete(self, text, wav_path=None):
        """录音结束后把文本发给模型并输出回复。"""
        if not text:
            print("[助手] 没听清, 请再说一遍。")
            return

        image = self.capture_frame() if self.args.camera is not None else None

        print(f"[助手] 你说: {text}")
        if image:
            print(f"[助手] 已附带摄像头画面 ({image.name})")

        try:
            if self.args.stream:
                reply = self._ask_stream(text, image)
            else:
                reply = self._ask_once(text, image)
        except AgnesError as exc:
            print(f"[助手] 模型调用失败: {exc}")
            return
        except Exception as exc:
            print(f"[助手] 出错: {exc}")
            return

        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": reply})
        # 只保留最近几轮, 控制上下文长度
        if len(self.history) > self.args.history_turns * 2:
            self.history = self.history[-self.args.history_turns * 2:]

        if self.speak:
            self.speak_reply(reply)

    def _ask_once(self, text, image=None):
        reply = self.client.chat(
            text,
            system=self.args.system or DEFAULT_SYSTEM,
            image=image,
            thinking=self.args.thinking,
            history=self.history or None,
            max_tokens=self.args.max_tokens,
            temperature=self.args.temperature,
        )
        print(f"[助手] {reply}")
        return reply

    def _ask_stream(self, text, image=None):
        print("[助手] ", end="", flush=True)
        parts = []
        for piece in self.client.stream(
            text,
            system=self.args.system or DEFAULT_SYSTEM,
            image=image,
            thinking=self.args.thinking,
            max_tokens=self.args.max_tokens,
            temperature=self.args.temperature,
        ):
            parts.append(piece)
            print(piece, end="", flush=True)
        print()
        return "".join(parts)

    # ------------------------------------------------------------ 语音播报

    def speak_reply(self, text):
        """把回复朗读出来(可选)。"""
        tool = find_tts()
        if tool is None:
            print(f"[助手] (未安装 TTS, 无法朗读: {text})")
            return
        try:
            if tool == "espeak-ng":
                subprocess.run(["espeak-ng", "-v", "cmn", text],
                               capture_output=True, timeout=30)
            elif tool == "espeak":
                subprocess.run(["espeak", "-v", "zh", text],
                               capture_output=True, timeout=30)
            elif tool == "pico2wave":
                out = Path(tempfile.gettempdir()) / "_agnes_tts.wav"
                subprocess.run(["pico2wave", "-w", str(out), text],
                               capture_output=True, timeout=30)
                subprocess.run(["aplay", "-q", str(out)],
                               capture_output=True, timeout=60)
            else:
                print(f"[助手] {text}")
        except Exception as exc:
            print(f"[助手] 朗读失败: {exc}")

    def _log(self, msg):
        if self.args.verbose:
            print(f"[助手] {msg}")


def find_tts():
    """找一个可用的 TTS 工具。"""
    for name in ("espeak-ng", "espeak", "pico2wave"):
        if shutil.which(name):
            return name
    return None


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="语音助手: 唤醒词 + Vosk 识别 + Agnes 多模态模型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # 复用 wakeup_asr 的参数定义
    p = argparse.ArgumentParser(
        description="语音助手: 唤醒词 + Vosk 识别 + Agnes 多模态模型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--wake", default="你好助手",
                   help="唤醒词, 多个用逗号分隔")
    p.add_argument("--model", default=None,
                   help="本地 Vosk 模型目录")
    p.add_argument("--device", default=None,
                   help="输入设备索引")
    p.add_argument("--duration", type=float, default=30.0,
                   help="单次录音最长秒数")
    p.add_argument("--silence", type=float, default=1.2,
                   help="静音多少秒后结束录音")
    p.add_argument("--vad-threshold", type=float, default=-45.0,
                   help="语音活动检测最低电平(dB)")
    p.add_argument("--replay", default=None,
                   help="用 16kHz wav 文件回放测试")
    p.add_argument("--no-save", dest="save", action="store_false",
                   help="不保存 WAV")
    p.add_argument("--quiet", action="store_true", help="不显示电平条")

    # Agnes 相关
    p.add_argument("--llm-model", default=None,
                   help="Agnes 模型名, 默认取 .env 里的 AGNES_MODEL")
    p.add_argument("--system", default=None,
                   help="系统提示词")
    p.add_argument("--stream", action="store_true", help="流式输出回复")
    p.add_argument("--thinking", action="store_true", help="开启 Thinking 模式")
    p.add_argument("--camera", type=int, default=None,
                   help="摄像头设备号, 启用多模态提问")
    p.add_argument("--speak", action="store_true", help="朗读模型回复")
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--history-turns", type=int, default=3,
                   help="保留最近几轮对话上下文")
    p.add_argument("--verbose", action="store_true", help="输出调试信息")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    app = VoiceAssistant(args)
    if args.camera is not None:
        app.open_camera()
    if args.replay:
        app.run_replay(args.replay)
    else:
        app.run_live()


if __name__ == "__main__":
    main()
