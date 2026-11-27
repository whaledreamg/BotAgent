#!/usr/bin/env python3
"""验证: 结束词 (会话结束语) 的行为。

覆盖:
1. 结束词能被登记 (含别名)
2. 说结束词 -> 立即结束录音 (不等静音超时)
3. 结束词从正文里摘掉, 不交给 Agent
4. 命中结束词 -> 连续对话窗口关闭, 必须重新唤醒
5. **包含即停止** —— 结束词在句子中间也触发
   (「跟他说再见之后我就走了」-> 结束)
6. 没配置结束词时功能完全关闭
7. 新的唤醒词能恢复会话 (窗口重新开启)
8. 结束词只出现在 partial 里也能命中
9. **结束词别名** —— 「拜拜」也算结束
10. **拼音模糊兜底** —— 「白白」「百拜」这种同音误识别也能命中
11. **收尾话** —— 正文先播, 播完再说「好的，我走了」
12. 结束词从正文里摘掉时**保留其余正文**

用法:
    conda run -n asrt python tools/check_end_word.py
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
import agent_client as agent_client_mod  # noqa: E402
from config import load_config  # noqa: E402
from pipeline import Pipeline  # noqa: E402

#: 测试用的结束词 (「再见」在 vosk 中文词表里是完整的一个词)
END_WORD = "再见"

#: 命中结束词后的收尾话 (与 configs/pipeline.yaml 的 end_prompt 一致)
END_PROMPT = "好的，我走了"


class FakeTTS:
    """假 TTS —— 只记录播了什么, 不真的发声。

    用来验证**播报顺序**: 「先播正文, 再播收尾话」。
    """

    def __init__(self, sink=None):
        self.sink = sink if sink is not None else []
        self.spoken = self.sink

    def speak(self, text):
        self.sink.append(text)
        return types.SimpleNamespace(
            connect_ms=0,
            first_frame_ms=0,
            synth_ms=0,
            play_ms=0,
            audio_ms=0,
            frames=0,
            duration_ms=0,
        )


class FakeAgent:
    """假 Agent —— 不发 HTTP, 直接返回一个固定的回复。

    用来验证「正文先播, 收尾话后播」的顺序 —— 真 Agent 需要跑起来,
    测试里等它太慢也太脆。
    """

    def __init__(self, reply: str = "今天晴, 二十六度"):
        self.reply = reply
        self.calls = []

    def dispatch(self, text, session_id=None):
        self.calls.append(text)
        return agent_client_mod.AgentReply(
            intent="chat",
            speak=True,
            reply=self.reply,
            server_ms=1,
        )


def make_silence(ms: int = 512) -> bytes:
    """静音块 (16kHz / 16bit)。"""
    n = int(16000 * ms / 1000)
    return (np.zeros(n, dtype=np.int16)).tobytes()


def make_speech(ms: int = 512, freq: float = 220.0) -> bytes:
    """有声音的块 (用正弦波模拟人声)。"""
    n = int(16000 * ms / 1000)
    t = np.arange(n) / 16000.0
    wave = 0.25 * np.sin(2 * np.pi * freq * t)
    return (wave * 32767).astype(np.int16).tobytes()


class ScriptedRecognizer:
    """按脚本吐出识别结果的识别器替身。

    ``script`` 是一个字符串列表 —— 每次 ``AcceptWaveform`` 返回 True
    就吐出下一个, 用来精确控制「什么时候识别出了什么」。
    """

    def __init__(self, script):
        self.script = list(script)
        self.index = 0
        self.words = []

    def AcceptWaveform(self, data):  # noqa: N802
        if self.index < len(self.script):
            self.index += 1
            return True
        return False

    def Result(self):  # noqa: N802
        if self.index == 0:
            return json.dumps({"text": "", "result": []})
        text = self.script[self.index - 1]
        return json.dumps(
            {"text": text, "result": [{"word": text, "conf": 0.99}]}
        )

    def PartialResult(self):  # noqa: N802
        return json.dumps({"partial": ""})

    def FinalResult(self):  # noqa: N802
        # 最后一段在 FinalResult 里才吐出来 (模拟 vosk 的收尾行为)
        if self.index < len(self.script):
            text = self.script[self.index]
            self.index += 1
            return json.dumps(
                {"text": text, "result": [{"word": text, "conf": 0.99}]}
            )
        return json.dumps({"text": "", "result": []})

    def SetWords(self, flag):  # noqa: N802
        return None


class PartialOnlyRecognizer(ScriptedRecognizer):
    """结束词只出现在 **partial** 里的识别器替身。

    模拟 vosk 在静音前不把最后一段定稿的情况 —— 结束词此时只在
    ``PartialResult`` 里, 不在 ``Result()`` / ``FinalResult()`` 里。

    ``script`` 里的每一项都在 ``AcceptWaveform()`` 返回 False 时
    作为 partial 吐出 (即「还没定稿」的状态)。
    """

    def __init__(self, script, partial=""):
        super().__init__([])
        self.partials = list(script)
        self.partial = partial

    def AcceptWaveform(self, data):  # noqa: N802
        # 一直不定稿 —— 模拟「vosk 还在等静音」
        return False

    def PartialResult(self):  # noqa: N802
        if self.partials:
            self.partial = self.partials.pop(0)
        return json.dumps({"partial": self.partial})

    def FinalResult(self):  # noqa: N802
        # 收尾时也不吐结束词 (它一直留在 partial 里)
        return json.dumps({"text": "", "result": []})


@contextlib.contextmanager
def patched_vosk(script=None):
    """把 vosk 的模型加载与识别器创建都替换成替身。"""
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
        self.echo_settle_sec = float(getattr(a, "echo_settle", 0.0) or 0.0)
        self.echo_quiet_timeout = float(getattr(a, "echo_quiet_timeout", 2.0) or 0.0)
        self.echo_guard_until = 0.0
        self.echo_reject_until = 0.0
        self.echo_quiet_at = 0.0
        self._quiet_run = 0
        self.end_prompt = str(getattr(a, "end_prompt", "") or "")
        self.model = None

        # 基类 WakeupASR.__init__ 里做的事, 这里手工做一遍 (不加载模型)
        self.args = a
        self.wake_words = ["小助手"]
        self.tokens = {"小助手": ["小", "助手"]}
        self.grammar = json.dumps(["小", "助手"])
        self.grammar_supported = False
        # 结束词: 走和基类一样的登记流程, 但不真的查词表
        #
        # 匹配已经是**纯文本子串**了, 不再依赖词表切分 —— 所以这里
        # 直接把主结束词 + 别名都登记成触发词。
        self.end_words = [
            w.strip() for w in str(getattr(a, "end", "") or "").split(",") if w.strip()
        ]
        self.end_aliases = [
            w.strip()
            for w in str(getattr(a, "end_aliases", "") or "").split(",")
            if w.strip()
        ]
        self.end_triggers = list(self.end_words) + list(self.end_aliases)
        self.end_enabled = bool(self.end_triggers)
        self.end_match_fuzzy = bool(getattr(a, "end_match_fuzzy", True))
        self.end_match_threshold = float(
            getattr(a, "end_match_threshold", 0.6) or 0.0
        )
        self.end_tokens = {w: [w] for w in self.end_triggers}
        self.state = "IDLE"
        self.kw_rec = None
        self.gen_rec = types.SimpleNamespace(
            AcceptWaveform=lambda d: False,
            Result=lambda: "{}",
            PartialResult=lambda: "{}",
        )
        self.kw_text = ""
        self.gen_text = ""
        self.ring = []
        self.noise_floor = -55.0
        self.preroll_sec = 0.5
        self.min_record_sec = 1.0
        self.rec_chunks = []
        self.rec_start_ts = 0.0
        self.last_voice_ts = 0.0
        self.final_texts = []
        self.partial_text = ""
        self.final_words = []
        self.ended_by_word = None
        self.asr_rec = None
        self.filter = types.SimpleNamespace(
            process=lambda d: d,
            description="(无滤波)",
        )

    def patched_rec(model, rate, grammar=None):
        if script is None:
            return ScriptedRecognizer([])
        if isinstance(script, tuple):
            # (定稿序列, partial 文本) —— 结束词只留在 partial 里
            return PartialOnlyRecognizer(script[0], script[1])
        return ScriptedRecognizer(script)

    server_mod.VoiceASR.__init__ = patched_init
    server_mod.wakeup_asr.KaldiRecognizer = patched_rec
    try:
        yield
    finally:
        server_mod.VoiceASR.__init__ = original_init
        server_mod.wakeup_asr.KaldiRecognizer = original_rec


def build_voice_asr(cfg, timeout: float = 30.0, end: str = END_WORD,
                    script=None, end_prompt: str = "",
                    end_aliases: str = "", end_match_fuzzy: bool = True,
                    end_match_threshold: float = 0.6):
    """构造一个不加载模型的 VoiceASR 实例 (需在 patched_vosk 上下文内)。"""
    args = types.SimpleNamespace(
        wake="小助手",
        end=end,
        end_aliases=end_aliases,
        end_match_fuzzy=end_match_fuzzy,
        end_match_threshold=end_match_threshold,
        end_prompt=end_prompt,
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
        echo_settle=cfg.asr.echo_settle,
        echo_quiet_timeout=cfg.asr.echo_quiet_timeout,
    )
    pipeline = Pipeline(cfg)
    app = server_mod.VoiceASR(args, pipeline)
    if script is not None:
        app._script = script
    return app


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def main() -> int:
    cfg = load_config()
    results = []

    # ------------------------------------------------------------ 1. 词表切分
    print("\n1) 结束词登记")
    with patched_vosk():
        app = build_voice_asr(cfg, end=END_WORD)
        results.append(check("结束词已登记", END_WORD in app.end_tokens,
                              f"end_tokens={list(app.end_tokens)}"))

    # ------------------------------------------------------------ 2. 立即结束录音
    print("\n2) 说结束词立即结束录音 (不等静音超时)")
    with patched_vosk(script=["今天天气怎么样", END_WORD]):
        app = build_voice_asr(cfg, end=END_WORD, script=["今天天气怎么样", END_WORD])
        ts = time.time()
        app.start_recording(ts, "小助手")
        # 喂两块: 第一块出「今天天气怎么样」, 第二块出「再见」
        app.process_block(make_speech(), ts)
        app.process_block(make_speech(), ts + 0.3)
        stopped = app.state == "IDLE"
        results.append(check("命中结束词后停止录音", stopped, f"state={app.state}"))
        results.append(check("记录了结束词", app.ended_by_word == END_WORD,
                              f"ended_by_word={app.ended_by_word}"))

    # ------------------------------------------------------------ 3. 摘掉结束词
    print("\n3) 结束词从正文里摘掉")
    with patched_vosk(script=["今天天气怎么样", END_WORD]):
        app = build_voice_asr(cfg, end=END_WORD, script=["今天天气怎么样", END_WORD])
        captured = {}
        app.on_recording_complete = (
            lambda text, wav_path=None, words=None: captured.update(text=text)
        )
        ts = time.time()
        app.start_recording(ts, "小助手")
        app.process_block(make_speech(), ts)
        app.process_block(make_speech(), ts + 0.3)
        text = captured.get("text", "")
        results.append(check("正文保留", "今天天气怎么样" in text, f"text={text!r}"))
        results.append(check("结束词已摘除", END_WORD not in text, f"text={text!r}"))

    # ------------------------------------------------------------ 4. 关闭连续对话
    print("\n4) 命中结束词后关闭连续对话窗口")
    with patched_vosk(script=["今天天气怎么样", END_WORD]):
        app = build_voice_asr(cfg, end=END_WORD, script=["今天天气怎么样", END_WORD])
        app.open_session()
        in_session_before = app.in_continuous_session
        ts = time.time()
        app.start_recording(ts, "小助手")
        app.process_block(make_speech(), ts)
        app.process_block(make_speech(), ts + 0.3)
        results.append(check("开始前在连续对话中", in_session_before))
        results.append(check("命中后窗口已关闭", app._session_until is None,
                              f"session_until={app._session_until}"))
        results.append(check("命中后不在连续会话中", not app.in_continuous_session))
        results.append(check("会话被标记为已结束", app.pipeline.session_ended))

    # ------------------------------------------------------------ 5. 包含即停止
    print("\n5) 结束词出现在句子中间也触发 (包含即停止)")
    with patched_vosk(script=["跟他说再见之后我就走了"]):
        app = build_voice_asr(cfg, end=END_WORD,
                              script=["跟他说再见之后我就走了"])
        ts = time.time()
        app.start_recording(ts, "小助手")
        app.process_block(make_speech(), ts)
        stopped = app.state == "IDLE"
        results.append(check("中间出现也结束录音", stopped,
                              f"state={app.state}"))
        results.append(check("记录了结束词", app.ended_by_word == END_WORD,
                              f"ended_by_word={app.ended_by_word}"))

    # ------------------------------------------------------------ 5b. 摘词保留正文
    print("\n5b) 摘掉中间结束词后保留其余正文")
    with patched_vosk(script=["跟他说再见之后我就走了"]):
        app = build_voice_asr(cfg, end=END_WORD,
                              script=["跟他说再见之后我就走了"])
        captured = {}
        app.on_recording_complete = (
            lambda text, wav_path=None, words=None: captured.update(text=text)
        )
        ts = time.time()
        app.start_recording(ts, "小助手")
        app.process_block(make_speech(), ts)
        text = captured.get("text", "")
        results.append(check("结束词已摘除", END_WORD not in text, f"text={text!r}"))
        results.append(check("其余正文保留",
                              "跟他说" in text and "之后我就走了" in text,
                              f"text={text!r}"))

    # ------------------------------------------------------------ 6. 未启用
    print("\n6) 没配置结束词时功能关闭")
    with patched_vosk(script=["今天天气怎么样"]):
        app = build_voice_asr(cfg, end="", script=["今天天气怎么样"])
        results.append(check("没有登记任何结束词", not app.end_tokens,
                              f"end_tokens={list(app.end_tokens)}"))
        ts = time.time()
        app.start_recording(ts, "小助手")
        app.process_block(make_speech(), ts)
        still_recording = app.state == "RECORDING"
        results.append(check("不会误结束录音", still_recording, f"state={app.state}"))

    # ------------------------------------------------------------ 7. 重新唤醒恢复会话
    print("\n7) 重新唤醒后恢复连续对话")
    with patched_vosk(script=["今天天气怎么样", END_WORD]):
        app = build_voice_asr(cfg, end=END_WORD, script=["今天天气怎么样", END_WORD])
        ts = time.time()
        app.start_recording(ts, "小助手")
        app.process_block(make_speech(), ts)
        app.process_block(make_speech(), ts + 0.3)
        ended = app.pipeline.session_ended
        # 新的唤醒 -> 清掉「已结束」标记
        app.start_recording(time.time() + 1, "小助手")
        resumed = not app.pipeline.session_ended
        results.append(check("结束词后标记为已结束", ended))
        results.append(check("新唤醒后恢复会话", resumed))

    # ------------------------------------------------------------ 8. 结束词只在 partial 里
    print("\n8) 结束词只在 partial 里也能命中")
    with patched_vosk(script=(["今天天气怎么样", END_WORD], "")):
        app = build_voice_asr(cfg, end=END_WORD,
                              script=(["今天天气怎么样", END_WORD], ""))
        ts = time.time()
        app.start_recording(ts, "小助手")
        # 第一块: partial = 「今天天气怎么样」
        app.process_block(make_speech(), ts)
        # 第二块: partial = 「再见」-> 应该立刻结束
        app.process_block(make_speech(), ts + 0.3)
        stopped = app.state == "IDLE"
        results.append(check("partial 里的结束词也触发结束", stopped,
                              f"state={app.state}"))
        results.append(check("记录了结束词", app.ended_by_word == END_WORD,
                              f"ended_by_word={app.ended_by_word}"))

    # ------------------------------------------------------------ 9. 结束词别名
    print("\n9) 结束词别名也算结束")
    aliases = "拜拜,白白,百拜,回头见"
    with patched_vosk(script=["拜拜"]):
        app = build_voice_asr(cfg, end=END_WORD, end_aliases=aliases,
                              script=["拜拜"])
        ts = time.time()
        app.start_recording(ts, "小助手")
        app.process_block(make_speech(), ts)
        results.append(check("「拜拜」触发结束", app.state == "IDLE",
                              f"state={app.state}"))
        results.append(check("记录了别名", app.ended_by_word == "拜拜",
                              f"ended_by_word={app.ended_by_word}"))

    with patched_vosk(script=["回头见"]):
        app = build_voice_asr(cfg, end=END_WORD, end_aliases=aliases,
                              script=["回头见"])
        ts = time.time()
        app.start_recording(ts, "小助手")
        app.process_block(make_speech(), ts)
        results.append(check("「回头见」触发结束", app.state == "IDLE",
                              f"state={app.state}"))

    # 别名也要从正文里摘掉
    with patched_vosk(script=["查一下天气拜拜"]):
        app = build_voice_asr(cfg, end=END_WORD, end_aliases=aliases,
                              script=["查一下天气拜拜"])
        captured = {}
        app.on_recording_complete = (
            lambda text, wav_path=None, words=None: captured.update(text=text)
        )
        ts = time.time()
        app.start_recording(ts, "小助手")
        app.process_block(make_speech(), ts)
        text = captured.get("text", "")
        results.append(check("别名从正文里摘掉", "拜拜" not in text, f"text={text!r}"))
        results.append(check("正文保留", "查一下天气" in text, f"text={text!r}"))

    # ------------------------------------------------------------ 10. 拼音兜底
    print("\n10) 拼音模糊兜底 (同音误识别)")
    #
    # 「拜拜」被听成「白白」「百拜」是常见误识别 —— 纯文本子串匹配对
    # 这类完全无能为力, 靠拼音相似度兜住。
    for wrong in ("白白", "百拜"):
        with patched_vosk(script=[wrong]):
            app = build_voice_asr(cfg, end="拜拜", script=[wrong])
            ts = time.time()
            app.start_recording(ts, "小助手")
            app.process_block(make_speech(), ts)
            results.append(check(f"「{wrong}」也能命中「拜拜」",
                                  app.state == "IDLE", f"state={app.state}"))

    # 关掉拼音兜底 -> 就不该命中了
    with patched_vosk(script=["白白"]):
        app = build_voice_asr(cfg, end="拜拜", script=["白白"],
                              end_match_fuzzy=False)
        ts = time.time()
        app.start_recording(ts, "小助手")
        app.process_block(make_speech(), ts)
        results.append(check("关掉兜底后不命中",
                              app.state == "RECORDING", f"state={app.state}"))

    # 兜底不该误伤普通句子
    with patched_vosk(script=["今天天气怎么样"]):
        app = build_voice_asr(cfg, end="拜拜", script=["今天天气怎么样"])
        ts = time.time()
        app.start_recording(ts, "小助手")
        app.process_block(make_speech(), ts)
        results.append(check("普通句子不被兜底误伤",
                              app.state == "RECORDING", f"state={app.state}"))

    # ------------------------------------------------------------ 10b. 防误伤
    print("\n10b) 拼音兜底**不误伤**普通句子 (回归)")
    #
    # 早期版本用 SequenceMatcher 直接比拼音**字符串**, 误伤极其严重:
    #
    #   「现在几点」    -> 命中「在几」~「再见」  (0.86)
    #   「查一下天气」  -> 命中「下天」~「再见」  (0.62)
    #   「音量调大一点」-> 命中「一点」~「再见」  (0.67)
    #
    # 原因: SequenceMatcher 比的是**字符**, 而「在几」(zai ji) 与
    # 「再见」(zai jian) 的字符前 5 个完全一样 —— 前缀碰巧相同就被
    # 判成高相似度。这不是读音相近, 只是巧合。
    #
    # 现在改成**逐音节**比对 (声母必须相同 + 韵母相近), 并且要求
    # **每个音节都相近**。下面这组就是防回归的锚点。
    false_positives = [
        "现在几点", "现在几号", "现在时间",
        "查一下天气", "今天会下雨吗",
        "音量调大一点",
        "请告诉我你在哪个城市",
        "今天天气怎么样", "讲个笑话", "打开客厅的灯", "打开空调",
        "帮我拿个苹果", "你是谁", "谢谢你", "知道了",
        "给我一瓶水",
    ]
    for text in false_positives:
        with patched_vosk(script=[text]):
            app = build_voice_asr(cfg, end=END_WORD, script=[text])
            ts = time.time()
            app.start_recording(ts, "小助手")
            app.process_block(make_speech(), ts)
            results.append(check(f"「{text}」不误触发结束",
                                  app.state == "RECORDING",
                                  f"state={app.state} ended={app.ended_by_word}"))

    # 真的告别语必须仍然能命中
    #
    # 「拜拜」「回头见」「白白了」靠的是**别名 + 拼音兜底**, 所以这里
    # 要把别名配上 —— 只配主结束词「再见」的话它们当然匹配不上。
    aliases = "拜拜,回头见,白白"
    for text in ("再见", "拜拜", "回头见", "再见吧", "白白了"):
        with patched_vosk(script=[text]):
            app = build_voice_asr(cfg, end=END_WORD, end_aliases=aliases,
                                  script=[text])
            ts = time.time()
            app.start_recording(ts, "小助手")
            app.process_block(make_speech(), ts)
            results.append(check(f"「{text}」仍然触发结束",
                                  app.state == "IDLE", f"state={app.state}"))

    # ------------------------------------------------------------ 11. 收尾话
    print("\n11) 结束后的收尾话 «好的，我走了»")
    #
    # 用户问「查下天气再见」-> 先播天气, **播完**再说这句告别。
    # 顺序不能反 —— 反了就显得机器人在敷衍。
    spoken = []
    with patched_vosk(script=["今天天气怎么样", END_WORD]):
        app = build_voice_asr(cfg, end=END_WORD,
                              script=["今天天气怎么样", END_WORD],
                              end_prompt=END_PROMPT)
        app.pipeline.tts = FakeTTS(spoken)
        app.pipeline.agent = FakeAgent("今天晴, 二十六度")
        ts = time.time()
        app.start_recording(ts, "小助手")
        app.process_block(make_speech(), ts)
        app.process_block(make_speech(), ts + 0.3)
        # 等后台线程把这一轮跑完
        deadline = time.time() + 10
        while time.time() < deadline and len(spoken) < 2:
            time.sleep(0.05)
        time.sleep(0.2)

    results.append(check("正文先播", any("今天晴" in s for s in spoken),
                          f"spoken={spoken}"))
    results.append(check("收尾话在正文之后播",
                          bool(spoken) and spoken[-1] == END_PROMPT,
                          f"spoken={spoken}"))
    results.append(check("收尾话是「好的，我走了」",
                          END_PROMPT in spoken, f"spoken={spoken}"))

    print()
    if all(results):
        print(f"结果: 全部通过 ({len(results)} 项)")
        return 0
    failed = len(results) - sum(results)
    print(f"结果: {failed} 项失败")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
