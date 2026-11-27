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
import re
import sys
import time
import wave
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

# 保证能从任意目录启动 (audio_filter 在本目录)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import audio_filter

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


def _split_syllable(syllable: str) -> tuple:
    """把一个拼音音节拆成 (声母, 韵母)。

    例: ``"zai"`` -> ``("z", "ai")``, ``"jian"`` -> ``("j", "ian")``。

    拆不出来 (比如整音节 ``"a"``) 时声母为空串, 韵母就是整个音节。
    """
    s = (syllable or "").strip().lower()
    if not s:
        return ("", "")
    # 声母表 —— 按长度降序匹配 (``zh``/``ch``/``sh`` 要先于 ``z``/``c``/``s``)
    for initial in ("zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n",
                    "l", "g", "k", "h", "j", "q", "x", "r", "z", "c", "s",
                    "y", "w"):
        if s.startswith(initial) and len(s) > len(initial):
            return (initial, s[len(initial):])
    return ("", s)


def _syllable_similarity(a: str, b: str) -> float:
    """两个拼音音节的相似度 (0~1)。

    分三档:

    - **完全相同** -> 1.0
    - **声母相同 + 韵母完全相同** -> 0.95
      (读音确实一样, 只是写法不同 —— 如「白」bai / 「拜」bai)
    - **声母相同 + 韵母相近** -> 0.9
      (韵母长且只差一处 —— 如「ian」/「iao」)
    - 其余 -> 0.0 (差太多, 不算命中)

    **短韵母必须完全一致** —— 这是防误伤的关键。「几」(ji) 与
    「见」(jian) 声母相同, 但读音差得远, 不该算相近。所以
    韵母长度 <= 2 时不做任何模糊比较。

    用「韵母是对方的子串」来判断相近是不行的: 「i」是「ian」的子串,
    按那条规则「几」会被判成和「见」一样 —— 而这正是「现在几点」
    被误判成「再见」的根源。
    """
    if a == b:
        return 1.0
    ini_a, fin_a = _split_syllable(a)
    ini_b, fin_b = _split_syllable(b)
    if not ini_a or not ini_b or ini_a != ini_b:
        return 0.0
    if fin_a == fin_b:
        return 0.95
    # 短韵母必须完全一致 —— 不做模糊比较
    if len(fin_a) <= 2 or len(fin_b) <= 2:
        return 0.0
    # 长韵母允许一处差异 (「ian」/「iao」这类)
    ratio = SequenceMatcher(None, fin_a, fin_b).ratio()
    return 0.9 if ratio >= 0.7 else 0.0


def _pinyin_syllable_similarity(win_sylls, target_sylls) -> float:
    """两个**音节序列**的相似度 (0~1) —— 逐音节比对后取平均。

    **每个音节都必须相近** (分数 >= 0.9) 才算命中 —— 这是防误伤的
    第二道闸。逐字符比对会让「在几」和「再见」拿到 0.86 (前缀字符
    碰巧相同), 而逐音节比对里「几」和「见」的韵母差得太远, 分数
    会被拉下来。

    要求「每个音节都相近」而不是「平均分够高」, 是为了防住另一种
    情况: 三音节的结束词里两个音节完全对上、第三个差很远, 平均分
    仍然可能超过阈值。

    Args:
        win_sylls: 识别文本窗口的音节列表。
        target_sylls: 结束词的音节列表。

    Returns:
        0~1 的相似度; 有任何音节不相近时返回 0.0。
    """
    if not win_sylls or not target_sylls:
        return 0.0
    if len(win_sylls) != len(target_sylls):
        return 0.0
    scores = [
        _syllable_similarity(w, t)
        for w, t in zip(win_sylls, target_sylls)
    ]
    # 任一音节不相近 -> 整体不成立
    if any(s < 0.9 for s in scores):
        return 0.0
    return sum(scores) / len(scores)


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

        # 结束词 -> 触发词集合
        #
        # **不再依赖 vosk 词表切分** —— 这是「结束词识别不上」的根因:
        # 切分失败的那个结束词会被直接忽略, 于是它永远匹配不上。
        # 而且即使切分成功, 要求「整句的结尾正好等于切分结果」也太严:
        # 「再见」被听成「在见」就废了。
        #
        # 现在改成**纯文本子串匹配**: 句子里**含有**结束词就结束。
        # 结束词分三类, 任意一类命中都算:
        #
        #   1. end_words      —— 主结束词 (如「再见」)
        #   2. end_aliases    —— 同义/近形的告别词 (如「拜拜」「回头见」)
        #   3. 拼音模糊兜底   —— 读音相近就算 (如「白白」「百拜」)
        self.end_words = [
            w.strip() for w in str(getattr(args, "end", "") or "").split(",") if w.strip()
        ]
        self.end_aliases = [
            w.strip()
            for w in str(getattr(args, "end_aliases", "") or "").split(",")
            if w.strip()
        ]
        #: 结束词的**完整触发词集合** —— 主结束词 + 别名
        self.end_triggers: list = list(self.end_words) + list(self.end_aliases)
        #: 结束词是否启用 (有触发词就行)
        self.end_enabled = bool(self.end_triggers)
        #: 拼音模糊兜底是否启用
        self.end_match_fuzzy = bool(getattr(args, "end_match_fuzzy", True))
        #: 拼音模糊兜底的相似度阈值
        self.end_match_threshold = float(
            getattr(args, "end_match_threshold", 0.6) or 0.0
        )

        if self.end_enabled:
            print(f"[结束词] {self.end_triggers}")
            if self.end_match_fuzzy:
                print(f"[结束词] 拼音兜底已启用 (阈值 {self.end_match_threshold:.2f})")
        else:
            print("[结束词] 没有可用的结束词, 功能未启用")

        # 兼容旧字段: 结束词的词表切分 (只用于日志, 匹配不再依赖它)
        self.end_tokens: dict = {}
        for w in self.end_triggers:
            toks = segment_wake_word(self.model, w)
            if toks:
                self.end_tokens[w] = toks
        if self.end_words and self.end_tokens:
            failed_end = [w for w in self.end_triggers if w not in self.end_tokens]
            if failed_end:
                print(f"[结束词] 这些词不在模型词表里 (仍可用, 走文本匹配): {failed_end}")

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
        #: **录音状态变化**的回调 (可选)。
        #:
        #: 调用时机: 开始录音 / 停止录音。回调收到 ``"RECORDING"`` 或
        #: ``"WAITING"`` —— 监控端靠它区分「还没触发唤醒词」和
        #: 「正在识别」, 画三态动画。
        #:
        #: 回调异常会被吞掉 —— 它只是个通知, 不该影响识别主流程。
        self.on_recording_state_change = None
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
        #: 词级识别结果 (词 + 置信度), 供热词纠错打分用
        self.final_words = []
        #: 本轮是否因为命中**结束词**而结束。None = 不是被结束词结束的。
        #:
        #: 子类 (server.VoiceASR) 靠它知道「要关闭连续对话窗口,
        #: 需要重新唤醒」。
        self.ended_by_word: Optional[str] = None

        #: **回声屏蔽** 相关的状态 (子类用)。
        #:
        #: TTS 播报期间, 麦克风会把机器人自己的声音录进去。播报结束
        #: 后声音还在空气里飘 (声波传播 + 房间混响 + ALSA 缓冲),
        #: 这段「余音」如果被当成用户开口, 就会把播报内容当成用户的话
        #: 再识别一遍 —— 表现为机器人自己跟自己对话。
        #:
        #: 三个字段配合起来用:
        #: - ``echo_guard_until``: 绝对时间戳。在此之前收到的音频一律
        #:   **丢弃** (播报后的静默沉降期)。
        #: - ``echo_reject_until``: 绝对时间戳。「等安静」的硬上限 ——
        #:   到点还没安静下来就强制恢复 (防止环境一直很吵导致永远不听)。
        #: - ``echo_quiet_at``: 绝对时间戳。确认「余音已消散」的时刻,
        #:   0 = 还没确认。
        self.echo_guard_until: float = 0.0
        self.echo_reject_until: float = 0.0
        self.echo_quiet_at: float = 0.0

        #: 麦克风输入的**高通滤波**。
        #:
        #: 为什么要滤: 空调隆隆声、电源 50Hz 哼声这类低频噪声会把静默时的
        #: RMS 电平拉高, 让 VAD 门限跟着抬高, 说话声更容易被判成噪声。
        #: 4 阶 80Hz 高通把 50Hz 压掉 16dB, 而 200Hz 以上几乎无损。
        #:
        #: 滤波器在**入队前**应用, 所以 VAD 与识别看到的是同一份信号。
        self.filter = audio_filter.build_filter(
            cutoff_hz=float(getattr(args, "highpass_hz", 0.0) or 0.0),
            sample_rate=SAMPLE_RATE,
            order=int(getattr(args, "highpass_order", 4) or 4),
            channels=1,
        )
        print(f"[滤波] {self.filter.description}")

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
            # **入队前滤波** —— 这样 VAD 的电平判断、唤醒词识别、正式识别
            # 看到的都是同一份滤波后的信号。滤掉的是低频噪声 (空调隆隆声、
            # 电源哼声), 人声内容基本无损。
            self.audio_q.put_nowait(self.filter.process(bytes(indata)))
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

    def _notify_recording_state(self, state: str) -> None:
        """通知外部「录音状态变了」。

        Args:
            state: ``"RECORDING"`` (正在录音) 或 ``"WAITING"`` (等待唤醒词)。
        """
        cb = getattr(self, "on_recording_state_change", None)
        if cb is None:
            return
        try:
            cb(state)
        except Exception as exc:  # pragma: no cover - 回调不该影响识别
            print(f"[录音状态] 回调异常: {exc}")

    def start_recording(self, ts, wake):
        self.state = "RECORDING"
        self._notify_recording_state("RECORDING")
        self.rec_chunks = []
        self.rec_start_ts = ts
        self.last_voice_ts = ts
        self.final_texts = []
        self.partial_text = ""
        self.asr_rec = KaldiRecognizer(self.model, SAMPLE_RATE)
        # 开启词级结果 —— 热词纠错要用逐词置信度打分。
        #
        # 置信度**不是**纠错的硬门槛 (Vosk 会对「自信的错误」给高置信度,
        # 比如把「埃斯顿」听成「埃弗顿」时 conf=1.000), 但它是有用的
        # 辅助权重 —— 低置信度的词更容易被误纠。
        self.asr_rec.SetWords(True)

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
                self._collect_words(res.get("result"))
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

    def _collect_words(self, result) -> None:
        """收集词级识别结果 (词 + 置信度)。

        ``SetWords(True)`` 之后, ``Result()`` 里会多一个 ``result`` 数组,
        每项形如 ``{"word": "埃斯顿", "conf": 0.994, "start": .., "end": ..}``。

        这是热词纠错的置信度来源。没有它也能纠 (只是少了辅助权重)。
        """
        if not isinstance(result, list):
            return
        for item in result:
            if not isinstance(item, dict):
                continue
            word = item.get("word")
            if not word:
                continue
            entry = {"word": str(word)}
            conf = item.get("conf")
            if conf is not None:
                try:
                    entry["conf"] = float(conf)
                except (TypeError, ValueError):
                    pass
            self.final_words.append(entry)

    # ------------------------------------------------------------ 结束词

    def _match_end(self, text: Optional[str] = None) -> Optional[str]:
        """在已识别的文本里找结束词。

        结束词要匹配**累积文本**而不是只匹配最后一块 —— 用户说
        「今天天气怎么样, 再见」时, 「再见」和前半句通常在不同的
        识别片段里, 只看最后一块会漏掉。

        匹配用的是**累积的识别结果** (已确认的 final 片段 + 当前的
        partial 片段)。partial 也要看 —— vosk 往往在静音前不会把
        最后一段定稿, 只等 final 的话要白等到静音超时才收尾, 显得
        反应很慢。

        ## 匹配规则: **含有即命中**

        只要句子里**含有**结束词就结束 —— 不要求它在末尾。

        为什么放宽: 结束词在识别文本里的位置很不稳定。vosk 可能把
        「再见」切成两块 (「再」+「见」), 也可能后面还跟着语气词
        (「再见吧」)。要求「结尾正好等于结束词」会把这些全都漏掉,
        表现为「说了再见但机器人不反应」。

        ## 三层匹配

        1. **精确子串** —— 主结束词 + 别名, 归一化后直接找
        2. **拼音模糊兜底** —— 读音相近就算 (如「白白」「百拜」)

        Args:
            text: 要检查的文本。None = 用累积的识别结果。

        Returns:
            命中的结束词; 没命中返回 None。
        """
        if not getattr(self, "end_enabled", False):
            return None

        if text is None:
            # 已定稿的片段 + 当前未定稿的片段
            text = norm("".join(self.final_texts)) + norm(self.partial_text or "")
        else:
            text = norm(text)
        if not text:
            return None

        # ---- 1. 精确子串 ----
        for w in self.end_triggers:
            key = norm(w)
            if key and key in text:
                return w

        # ---- 2. 拼音模糊兜底 ----
        if self.end_match_fuzzy:
            hit = self._match_end_by_pinyin(text)
            if hit:
                return hit

        return None

    def _match_end_by_pinyin(self, text: str) -> Optional[str]:
        """用**拼音相似度**找结束词。

        解决「结束词识别不上」: Vosk 常把「拜拜」听成「白白」「百拜」,
        把「再见」听成「在见」。纯文本子串匹配对这种情况无能为力,
        但读音是接近的 —— 按拼音比对就能兜住。

        ## 比对的是**音节**, 不是拼音字符串

        早期版本用 ``SequenceMatcher`` 直接比拼音**字符串**,
        误伤极其严重:

        | 识别文本 | 命中窗口 | 结束词 | 相似度 |
        | --- | --- | --- | --- |
        | 现在几点 | 在几 | 再见 | 0.86 |
        | 查一下天气 | 下天 | 再见 | 0.62 |
        | 音量调大一点 | 一点 | 再见 | 0.67 |

        原因: ``SequenceMatcher`` 比的是**字符**, 而「在几」(zai ji) 与
        「再见」(zai jian) 的字符前 5 个完全一样 —— 前缀碰巧相同就被
        判成 0.86 的相似度。这**不是读音相近**, 只是巧合。

        现在改成**逐音节**比对: 两个音节算「相近」的条件是

        1. **完全相同** —— 直接算 1.0 分
        2. **声母相同且韵母相近** —— 如「拜」(bai) vs「白」(bai)、
           「再」(zai) vs「在」(zai) —— 算 0.9 分
        3. **整体编辑距离相近** —— 兜住「jiàn」vs「jiǎn」这类声调差异
           (pypinyin 默认不带声调, 所以这条覆盖了大部分情况)

        只有**每个音节都相近**才算命中。

        Args:
            text: 已归一化的文本。

        Returns:
            命中的结束词; 没命中返回 None。
        """
        try:
            from hotwords import to_pinyin
        except Exception:  # pragma: no cover - 缺 pypinyin 就跳过
            return None

        threshold = float(self.end_match_threshold or 0.0)
        if threshold <= 0:
            return None

        for w in self.end_triggers:
            key = norm(w)
            if not key:
                continue
            target_sylls = to_pinyin(key).split()
            if not target_sylls:
                continue

            # 窗口长度 = 音节数 (中文 1 汉字 ≈ 1 音节)
            n_syll = len(target_sylls)
            for start in range(0, max(0, len(text) - n_syll + 1)):
                window = text[start : start + n_syll]
                if not window:
                    break
                win_sylls = to_pinyin(window).split()
                if len(win_sylls) != n_syll:
                    continue
                ratio = _pinyin_syllable_similarity(win_sylls, target_sylls)
                if ratio >= threshold:
                    print(
                        f"[结束词] 拼音兜底命中: {window!r} ~ {key!r} ({ratio:.2f})"
                    )
                    return w
        return None

    def _strip_end_word(self, text: str, end_word: str) -> str:
        """把结束词从文本里去掉。

        结束词不是用户的问题, 不该交给 Agent —— 去掉之后如果还剩下
        正文 (「查一下天气, 再见」-> 「查一下天气」), 那部分照常处理。

        结束词可能在句子的**任意位置** (包含即匹配), 所以这里不是只
        剥结尾, 而是把**所有**出现的结束词都摘掉。
        """
        stripped = norm(text)
        if not stripped:
            return stripped

        # 摘掉所有触发词 (长的优先 —— 「再见」要在「见」之前摘)
        triggers = sorted(
            (norm(w) for w in self.end_triggers if norm(w)),
            key=len,
            reverse=True,
        )
        for key in triggers:
            if key and key in stripped:
                stripped = stripped.replace(key, "")

        # 摘掉后可能留下多余的标点/空格
        stripped = re.sub(r"[\s,，。!！?？、]+", "", stripped)
        return stripped

    def stop_recording(self, reason):
        if self.state != "RECORDING":
            return
        self.state = "IDLE"
        # 录音结束 —— 回到「等待唤醒词」。
        # (识别完成的标记由 Pipeline 那边负责, 这里只管录音状态)
        self._notify_recording_state("WAITING")
        try:
            res = json.loads(self.asr_rec.FinalResult())
            text = res.get("text", "").strip()
            if text:
                self.final_texts.append(text)
            self._collect_words(res.get("result"))
        except Exception:
            pass
        self.asr_rec = None
        # partial 先留一份 —— 结束词的判定要用它 (见下)。
        # 万一 vosk 把结束词留在 partial 里没定稿, 也不能漏判。
        last_partial = self.partial_text
        self.partial_text = ""
        self._print_line("", newline=True)

        full = norm("".join(self.final_texts))
        # 去掉开头重复的唤醒词
        for w in self.tokens:
            if full.startswith(norm(w)):
                full = full[len(norm(w)):]
                break

        # 结束词: 命中就把会话结束掉, 并把结束词从正文里摘掉
        # (结束词不是用户的问题, 不该交给 Agent)
        self.ended_by_word = None
        end_word = self._match_end(full) or self._match_end(
            norm("".join(self.final_texts)) + norm(last_partial)
        )
        if end_word:
            self.ended_by_word = end_word
            full = self._strip_end_word(full, end_word)
            print(f"\n[结束词] 命中 [{end_word}] —— 会话结束, 需要重新唤醒")

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
        self.on_recording_complete(full, wav_path, self.final_words)

    def on_recording_complete(self, text, wav_path=None, words=None):
        """录音结束回调, 默认什么都不做。子类可覆盖以接入 LLM。

        Args:
            text: 识别出的完整文本。
            wav_path: 录音文件路径 (未落盘时为 None)。
            words: 词级结果 ``[{"word":..., "conf":...}]``, 供热词纠错用。
        """
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

        # 结束词: 命中就**立刻**结束录音, 不等静音超时。
        #
        # 「再见」说出口之后还继续录着, 用户会以为机器人没听见, 或者
        # 把后面的环境噪声也录进去 —— 所以必须第一时间收尾。
        if getattr(self, "end_tokens", None) and self._match_end():
            self.stop_recording("命中结束词")
            return False

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
            # 回放不走麦克风回调, 所以要在这里手动滤波 ——
            # 保证回放测试测的是**滤波后**的链路。
            chunk = self.filter.process(raw[i:i + step])
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
    p.add_argument("--end", default="",
                   help="结束词, 多个用逗号分隔, 例如: --end '再见,拜拜'。"
                        "说完结束词后立即结束对话, 需要重新唤醒。留空 = 不启用")
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
    p.add_argument("--highpass-hz", type=float, default=80.0,
                   help="高通滤波截止频率(Hz), 0=关闭, 默认 80")
    p.add_argument("--highpass-order", type=int, default=4,
                   help="高通滤波阶数, 默认 4")
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
