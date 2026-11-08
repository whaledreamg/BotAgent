#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时语音识别: 自定义唤醒词 -> 录音 -> 流式转文字 -> 保存 WAV

用法:
    python wakeup_asr.py --wake "你好助手"
    python wakeup_asr.py --wake "你好助手,小助手" --device 5
    python wakeup_asr.py --replay test.wav --wake "你好助手"   # 用文件回放测试

依赖: vosk, sounddevice, numpy
模型: vosk-model-small-cn-0.22 (默认从 ~/.cache/vosk 自动查找)
"""

import argparse
import collections
import json
import os
import queue
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

SetLogLevel(-1)  # 关闭 vosk 自己的日志输出

SAMPLE_RATE = 16000
BLOCK_SIZE = 4096  # 16kHz 下约 256ms 一块

DEFAULT_MODEL_NAMES = [
#    "vosk-model-small-cn-0.22",
    "vosk-model-cn-0.22",
]
MODEL_SEARCH_DIRS = [
    Path(os.environ["VOSK_MODEL_PATH"]) if os.environ.get("VOSK_MODEL_PATH") else None,
    Path.home() / ".cache" / "vosk",
    Path("/usr/share/vosk"),
    Path(__file__).resolve().parent / "models",
]

# 物理麦克风设备名关键字(按优先级), 优先挑原生 16kHz 的设备
MIC_HINTS = ["dmic16k", "dmic", "analog", "mic", "webcam", "capture"]


# --------------------------------------------------------------------- 工具

def find_model(explicit):
    """定位模型目录: 优先 --model, 其次在常见目录里找中文模型。"""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise SystemExit(f"指定的模型路径不存在: {p}")
        return p

    for base in MODEL_SEARCH_DIRS:
        if base is None or not base.is_dir():
            continue
        for name in DEFAULT_MODEL_NAMES:
            cand = base / name
            if cand.is_dir():
                return cand
        for cand in sorted(base.glob("vosk-model-*")):
            if cand.is_dir():
                return cand

    raise SystemExit(
        "未找到 vosk 模型。请下载:\n"
        "  curl -L -o /tmp/vosk-cn.zip "
        "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip\n"
        "  unzip -q /tmp/vosk-cn.zip -d ~/.cache/vosk/\n"
        "或使用 --model 指定路径。"
    )


def rms_db(data):
    """整块音频的 RMS 电平(dBFS)。"""
    if not data:
        return -120.0
    arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    if arr.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(arr ** 2)))
    if rms <= 1e-6:
        return -120.0
    return 20.0 * np.log10(rms / 32768.0)


def norm(text):
    """去掉所有空白, 便于匹配中文短语。"""
    return "".join(text.split())


# --------------------------------------------------------------- 唤醒词分词

def segment_wake_word(model, word):
    """把唤醒词切分成模型词表里存在的最小单元(最长优先贪心)。

    vosk 的语法受限解码只能使用词表里有的词, 直接传 "你好助手" 会被忽略
    (Ignoring word missing in vocabulary), 所以这里切成 "你好" + "助手"。
    返回分词列表; 无法完整切分返回 None。
    """
    chars = list(word)
    tokens = []
    i = 0
    while i < len(chars):
        matched = None
        for j in range(len(chars), i, -1):  # 最长优先
            cand = "".join(chars[i:j])
            if model.vosk_model_find_word(cand) != -1:
                matched = cand
                break
        if matched is None:
            return None
        tokens.append(matched)
        i += len(matched)
    return tokens


# ----------------------------------------------------------------- 设备选择

def pick_device(requested):
    """选择输入设备候选列表: 优先用户指定, 否则找原生 16kHz 的物理麦克风。

    返回按优先级排序的设备索引列表, 打开失败时会自动回退到下一个。
    """
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = None

    candidates = [d for d in sd.query_devices() if d["max_input_channels"] > 0]

    def rank(d):
        name = d["name"].lower()
        bonus = 0
        for i, hint in enumerate(MIC_HINTS):
            if hint in name:
                bonus = 100 - i * 10
                break
        native16 = 20 if int(d["default_samplerate"]) == SAMPLE_RATE else 0
        is_hw = 10 if d["hostapi"] == 0 else 0
        return bonus + native16 + is_hw

    candidates.sort(key=rank, reverse=True)
    ordered = [int(d["index"]) for d in candidates]
    if default_in is not None and default_in not in ordered:
        ordered.append(int(default_in))

    if requested is not None:
        req = int(requested)
        # 用户指定的排最前, 其余作为回退
        return [req] + [d for d in ordered if d != req]
    return ordered or [None]


def open_input_stream(device, callback=None):
    """打开 16kHz 单声道输入流, 失败时自动回退到其它候选设备。"""
    errors = []
    for dev in device:
        try:
            stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=BLOCK_SIZE,
                device=dev,
                callback=callback,
            )
            return stream, dev
        except Exception as exc:
            errors.append((dev, exc))
    msg = "\n".join(f"  device {d}: {e}" for d, e in errors)
    raise SystemExit(
        f"无法以 {SAMPLE_RATE}Hz 打开任何输入设备:\n{msg}\n"
        "提示: 用 --device 指定设备索引, 或用 arecord -l 查看可用麦克风。"
    )


# ------------------------------------------------------------------- 主程序

class WakeupASR:
    def __init__(self, args):
        self.args = args
        self.model_path = find_model(args.model)
        print(f"[模型] {self.model_path}")
        self.model = Model(str(self.model_path))

        self.wake_words = [w.strip() for w in args.wake.split(",") if w.strip()]
        if not self.wake_words:
            raise SystemExit("唤醒词不能为空, 请用 --wake 指定")

        # 唤醒词 -> 词表切分
        self.tokens = {}
        failed = []
        for w in self.wake_words:
            toks = segment_wake_word(self.model, w)
            if toks:
                self.tokens[w] = toks
            else:
                failed.append(w)
        if failed:
            print(f"[错误] 这些唤醒词无法用模型词表切分, 已忽略: {failed}")
            print("       提示: 换成模型词表里有的词, 例如 '你好助手' -> '你好 助手'")
        if not self.tokens:
            raise SystemExit("没有可用的唤醒词")

        # 语法受限识别器: 所有切分后的词作为一个词袋, 灵敏度最高
        grammar_tokens = []
        for toks in self.tokens.values():
            grammar_tokens.extend(toks)
        # 保持首次出现顺序, 避免输出乱序
        seen = set()
        ordered = [t for t in grammar_tokens if not (t in seen or seen.add(t))]
        self.grammar = json.dumps(ordered, ensure_ascii=False)

        # 大模型 (vosk-model-cn-0.22) 只有静态 HCLG.fst, 不支持运行时语法图
        # (Runtime graphs are not supported by this model), 此时退化为只用通用识别器匹配
        self.grammar_supported = self._model_supports_grammar()
        if self.grammar_supported:
            print(f"[语法] {self.grammar}")
        else:
            print(f"[语法] 不支持运行时语法 (当前模型无 Gr.fst), "
                  f"改用通用识别器匹配唤醒词: {list(self.tokens)}")

        # 关键词识别器(高灵敏度) + 通用识别器(高精度), 两者同时命中才唤醒
        self.kw_rec = (KaldiRecognizer(self.model, SAMPLE_RATE, self.grammar)
                       if self.grammar_supported else None)
        self.gen_rec = KaldiRecognizer(self.model, SAMPLE_RATE)

        self.state = "IDLE"  # IDLE / RECORDING
        self.audio_q = queue.Queue()
        self.ring = collections.deque(maxlen=64)  # (ts, bytes), 约 16s
        self.rec_chunks = []
        self.rec_start_ts = 0.0
        self.last_voice_ts = 0.0
        self.noise_floor = -55.0
        self.final_texts = []
        self.partial_text = ""
        self.preroll_sec = 0.5
        self.min_record_sec = 1.0
        self.running = True
        self.wake_word_hit = None
        self.kw_text = ""
        self.gen_text = ""

    # ------------------------------------------------------------- 设备

    def open_stream(self):
        candidates = pick_device(self.args.device)
        self.stream, device = open_input_stream(candidates, self._callback)
        info = sd.query_devices(device, "input")
        print(f"[麦克风] {info['name']} (index={device}, "
              f"输入声道={info['max_input_channels']}, "
              f"原生采样率={int(info['default_samplerate'])})")

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"[状态] {status}", file=sys.stderr)
        try:
            self.audio_q.put_nowait(bytes(indata))
        except queue.Full:
            pass

    # ------------------------------------------------------------ 唤醒检测

    def _model_supports_grammar(self):
        """检测当前模型是否支持运行时语法图(grammar)。

        小模型 (vosk-model-small-cn-0.22) 带 Gr.fst, 支持动态语法;
        大模型 (vosk-model-cn-0.22) 只有静态 HCLG.fst, 不支持。
        """
        graph_dir = self.model_path / "graph"
        return (graph_dir / "Gr.fst").is_file()

    def _feed_detectors(self, data):
        """同时喂给语法识别器和通用识别器, 更新两者的文本。"""
        if self.kw_rec is not None:
            if self.kw_rec.AcceptWaveform(data):
                self.kw_text = norm(json.loads(self.kw_rec.Result()).get("text", ""))
            else:
                self.kw_text = norm(
                    json.loads(self.kw_rec.PartialResult()).get("partial", "")
                ) or self.kw_text

        if self.gen_rec.AcceptWaveform(data):
            self.gen_text = norm(json.loads(self.gen_rec.Result()).get("text", ""))
        else:
            self.gen_text = norm(
                json.loads(self.gen_rec.PartialResult()).get("partial", "")
            ) or self.gen_text

    def _match_wake(self):
        """双重判定: 语法识别器命中 + 通用识别器包含同一个唤醒词。

        只靠语法识别器误触发很高(实测 3/4), 只靠通用识别器会漏掉模糊发音,
        两者取交集可以在保持召回的同时把误触发降到 0。

        若模型不支持运行时语法图(大模型), 则只依赖通用识别器判定。
        """
        for w, toks in self.tokens.items():
            key = norm("".join(toks))
            if norm(w) not in self.gen_text:
                continue
            if self.kw_rec is None:
                # 无语法识别器: 通用识别器命中即触发
                return w
            if key in self.kw_text:
                return w
        return None

    def check_wake(self, data):
        self._feed_detectors(data)
        return self._match_wake()

    # ------------------------------------------------------------ 录音

    def start_recording(self, ts, wake):
        self.state = "RECORDING"
        self.rec_chunks = []
        self.rec_start_ts = ts
        self.last_voice_ts = ts
        self.final_texts = []
        self.partial_text = ""
        self.asr_rec = KaldiRecognizer(self.model, SAMPLE_RATE)

        # 重置两个检测器, 避免连续唤醒词立刻二次触发
        self.kw_rec = (KaldiRecognizer(self.model, SAMPLE_RATE, self.grammar)
                       if self.grammar_supported else None)
        self.gen_rec = KaldiRecognizer(self.model, SAMPLE_RATE)
        self.kw_text = ""
        self.gen_text = ""

        # 前滚: 带上唤醒词之前 ~0.5s 的音频, 避免吞掉紧跟唤醒词的第一句话
        preroll = []
        for rts, chunk in list(self.ring):
            if ts - rts <= self.preroll_sec:
                preroll.append(chunk)
        for chunk in preroll:
            self.rec_chunks.append(chunk)
            self.asr_rec.AcceptWaveform(chunk)

        print(f"\n{'=' * 60}")
        print(f"已唤醒! 唤醒词: [{wake}]  请开始说话...")
        print("=" * 60)

    def feed_asr(self, data, ts):
        self.rec_chunks.append(data)

        if self.asr_rec.AcceptWaveform(data):
            try:
                res = json.loads(self.asr_rec.Result())
            except json.JSONDecodeError:
                res = {}
            text = res.get("text", "").strip()
            if text:
                self.final_texts.append(text)
                self.partial_text = ""
                self._print_line(f"✓ {text}", newline=True)
            else:
                self.partial_text = ""
        else:
            try:
                res = json.loads(self.asr_rec.PartialResult())
            except json.JSONDecodeError:
                res = {}
            self.partial_text = res.get("partial", "").strip()
            if self.partial_text:
                self._print_line(f"… {self.partial_text}", newline=False)

    def stop_recording(self, reason):
        if self.state != "RECORDING":
            return
        self.state = "IDLE"
        try:
            res = json.loads(self.asr_rec.FinalResult())
            text = res.get("text", "").strip()
            if text:
                self.final_texts.append(text)
        except Exception:
            pass
        self.asr_rec = None
        self.partial_text = ""
        self._print_line("", newline=True)

        full = norm("".join(self.final_texts))
        # 去掉开头重复的唤醒词
        for w in self.tokens:
            if full.startswith(norm(w)):
                full = full[len(norm(w)):]
                break
        dur = max(0.0, time.time() - self.rec_start_ts)
        print(f"\n[结束] 原因: {reason}   时长: {dur:.1f}s")
        print(f"[完整文本] {full if full else '(未识别到内容)'}")

        wav_path = None
        if self.args.save and self.rec_chunks:
            wav_path = self.save_wav()
            if wav_path:
                print(f"[已保存] {wav_path}")
        print("=" * 60 + "\n")

        # 扩展点: 子类可在这里接入 LLM 等后处理
        self.on_recording_complete(full, wav_path)

    def on_recording_complete(self, text, wav_path=None):
        """录音结束回调, 默认什么都不做。子类可覆盖以接入 LLM。"""
        return None

    def save_wav(self):
        outdir = Path(__file__).resolve().parent / "recordings"
        outdir.mkdir(parents=True, exist_ok=True)
        path = outdir / time.strftime("rec_%Y%m%d_%H%M%S.wav")
        data = b"".join(self.rec_chunks)
        try:
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(data)
        except Exception as exc:
            print(f"[保存失败] {exc}")
            return None
        return path

    # ------------------------------------------------------------ 打印

    def _print_line(self, text, newline):
        if newline:
            sys.stdout.write("\r\033[K" + text + "\n")
        else:
            sys.stdout.write("\r\033[K    " + text)
        sys.stdout.flush()

    def print_level(self, db):
        if self.args.quiet:
            return
        ratio = max(0.0, min(1.0, (db + 60.0) / 60.0))
        n = int(ratio * 20)
        bar = "|" * n + "." * (20 - n)
        sys.stdout.write(f"\r[电平] {bar} {db:6.1f} dB  状态: {self.state}   ")
        sys.stdout.flush()

    # ------------------------------------------------------------ 主循环

    def process_block(self, data, ts):
        """处理一块音频, 返回是否发生状态切换。"""
        if self.state == "IDLE":
            hit = self.check_wake(data)
            if hit:
                self.start_recording(ts, hit)
                return True
            return False

        self.feed_asr(data, ts)
        db = rms_db(data)
        thresh = max(self.noise_floor + 10.0, self.args.vad_threshold)
        if db > thresh:
            self.last_voice_ts = ts
        elapsed = ts - self.rec_start_ts
        if elapsed >= self.args.duration:
            self.stop_recording(f"达到最长时长 {self.args.duration}s")
        elif elapsed > self.min_record_sec and (ts - self.last_voice_ts) >= self.args.silence:
            self.stop_recording(f"静音 {self.args.silence}s")
        return False

    def update_noise_floor(self, db):
        if db < self.noise_floor:
            self.noise_floor = db
        else:
            self.noise_floor = self.noise_floor * 0.995 + db * 0.005

    def run_live(self):
        self.open_stream()
        self.stream.start()
        print("\n[*] 正在监听唤醒词, 按 Ctrl+C 退出")
        for w, toks in self.tokens.items():
            print(f"[*] 唤醒词: {w}  (词表切分: {' + '.join(toks)})")
        print()

        last_level_print = 0.0
        try:
            while self.running:
                try:
                    data = self.audio_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                ts = time.time()
                self.ring.append((ts, data))
                self.update_noise_floor(rms_db(data))
                self.process_block(data, ts)

                if ts - last_level_print > 0.5:
                    self.print_level(rms_db(data))
                    last_level_print = ts
        except KeyboardInterrupt:
            print()
        finally:
            self.running = False
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            if self.state == "RECORDING":
                self.stop_recording("程序退出")

    def run_replay(self, path):
        """用 wav 文件回放测试, 不占用麦克风。"""
        with wave.open(path, "rb") as wf:
            if wf.getframerate() != SAMPLE_RATE:
                raise SystemExit(f"回放文件必须是 {SAMPLE_RATE}Hz, 当前 {wf.getframerate()}Hz")
            raw = wf.readframes(wf.getnframes())

        print("\n[*] 回放模式")
        for w, toks in self.tokens.items():
            print(f"[*] 唤醒词: {w}  (词表切分: {' + '.join(toks)})")
        print()

        step = BLOCK_SIZE * 2
        t0 = time.time()
        for i in range(0, len(raw), step):
            chunk = raw[i:i + step]
            ts = t0 + i / 2 / SAMPLE_RATE
            self.ring.append((ts, chunk))
            self.update_noise_floor(rms_db(chunk))
            self.process_block(chunk, ts)
            time.sleep(0.05)
        if self.state == "RECORDING":
            self.stop_recording("回放结束")


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="唤醒词 + 实时流式语音识别",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--wake", default="你好助手",
                   help="唤醒词, 多个用逗号分隔, 例如: --wake '你好助手,小助手'")
    p.add_argument("--model", default=None,
                   help="模型目录, 默认自动查找 ~/.cache/vosk 下的中文模型")
    p.add_argument("--device", default=None,
                   help="输入设备索引, 默认自动挑选原生 16kHz 麦克风")
    p.add_argument("--duration", type=float, default=30.0,
                   help="单次录音最长秒数, 默认 30")
    p.add_argument("--silence", type=float, default=1.2,
                   help="静音多少秒后结束录音, 默认 1.2")
    p.add_argument("--vad-threshold", type=float, default=-45.0,
                   help="语音活动检测最低电平(dB), 默认 -45")
    p.add_argument("--replay", default=None,
                   help="用 16kHz wav 文件回放测试, 不使用麦克风")
    p.add_argument("--no-save", dest="save", action="store_false",
                   help="不保存 WAV 文件")
    p.add_argument("--quiet", action="store_true", help="不显示电平条")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    app = WakeupASR(args)
    if args.replay:
        app.run_replay(args.replay)
    else:
        app.run_live()


if __name__ == "__main__":
    main()
