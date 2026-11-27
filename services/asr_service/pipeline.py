"""语音链路编排状态机。

状态流转:
    IDLE  --唤醒-->  REC  --识别完成--> THINK --speak=true--> SAY --播完--> IDLE
                                       \\--speak=false--------------> IDLE

职责边界:
- ASR 永远是第一环: 麦克风 -> 唤醒 -> 识别
- Agent 决定是否朗读 (speak 字段)
- 只有 speak=true 时才连 TTS, PCM 边收边播, 不落盘
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from agent_client import AgentClient, AgentError
from config import Config
from hotwords import HotwordCorrector, load_corrector
from tts_client import TTSClient, TTSError

logger = logging.getLogger("pipeline")


class State(str, Enum):
    IDLE = "IDLE"
    REC = "REC"
    THINK = "THINK"
    SAY = "SAY"


class RecState(str, Enum):
    """**录音**阶段的状态 (跟 Pipeline 的 State 分开)。

    ``Pipeline.State`` 描述的是「这一轮处理到哪一步了」, 从识别完成
    才开始变动 —— 在它眼里, 等待唤醒词和正在录音都是 IDLE, 没法区分。

    监控端要画「语音识别三态动画」(等唤醒 / 识别中 / 识别完成),
    靠的就是这个枚举。
    """

    #: 等待唤醒词 (没在录音)
    WAITING = "WAITING"
    #: 正在录音 (已唤醒, 用户在说话)
    RECORDING = "RECORDING"


@dataclass
class LastTurn:
    """最近一轮的完整记录, 供 /state 查询。"""

    text: str = ""
    intent: str = ""
    speak: bool = False
    reply: str = ""
    action: Optional[Dict[str, Any]] = None
    trace: List[Dict[str, Any]] = field(default_factory=list)
    at: float = 0.0
    #: 分阶段耗时 (毫秒)
    timings: Dict[str, int] = field(default_factory=dict)
    #: 最近一次热词纠错的明细 (before/after/hits); 没纠错过就是空的
    hotword_fix: Dict[str, Any] = field(default_factory=dict)


class Pipeline:
    """ASR -> Agent -> (TTS -> 播放) 的编排器。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.agent = AgentClient(cfg.agent, session_id=cfg.asr.session_id)
        self.tts = TTSClient(cfg.tts)
        self.state = State.IDLE
        #: **录音**阶段的状态 (等待唤醒词 / 正在录音)。
        #:
        #: 由 ASR 侧 (VoiceASR) 在 start_recording / stop_recording 时
        #: 通过 ``set_rec_state()`` 推进 —— 监控端靠它画三态动画。
        self.rec_state = RecState.WAITING
        #: 最近一次「识别完成」的标记: ``{"text": ..., "at": ...}``。
        #:
        #: 识别完成的**瞬间**就写进来 (不等 Agent 跑完), 监控端能
        #: 第一时间知道「这一轮识别完了」。新一轮开始时清空。
        self.rec_result: Optional[Dict[str, Any]] = None
        self.last = LastTurn()
        self.errors: List[str] = []
        self.turn_count = 0
        self._lock = threading.Lock()
        #: **会话已被结束词关闭**的标记。
        #:
        #: 用户说了「再见」之后, 这一轮结束后**不能**再开启连续对话窗口
        #: —— 否则「再见」就白说了, 窗口内还能直接说话。
        #:
        #: ASR 侧命中结束词时调 :meth:`end_session` 置位; 下次用唤醒词
        #: 开启新会话时, ASR 侧调 :meth:`resume_session` 清掉。
        self.session_ended = False
        #: 一轮完全结束 (Agent + TTS 播完) 时的回调。
        #: ASR 侧用它来开启连续对话窗口。
        self.on_turn_complete: Optional[Callable[[], None]] = None
        #: **播报开始前**的回调。
        #:
        #: ASR 侧用它来架「回声守卫」—— 必须在 TTS 开口**之前**就
        #: 声明「接下来别听我的声音」。等播完了再补就晚了: 播报最后
        #: 几帧的余音时间戳比守卫还新, 过滤不掉, 会被当成用户说话。
        self.on_before_speak: Optional[Callable[[], None]] = None
        #: **播报结束后**的回调。
        #:
        #: ASR 侧用它把回声守卫延伸到「播报结束 + 沉降期」——
        #: 播之前只知道「要播了」, 播完才知道余音窗口到底在哪。
        self.on_after_speak: Optional[Callable[[], None]] = None
        #: **热词纠错器** (专业名词本地拼音纠错)。
        #:
        #: 现在的 ASR 是 Vosk 大模型, 只有静态 HCLG.fst, **不支持运行时
        #: 语法图** —— 没法在解码时把「公司名」塞进词表加权。所以改在
        #: **识别之后、交给 Agent 之前** 做纠错: 纯本地, 零网络延迟。
        #:
        #: Agent 那边有 LLM 兜底, 但语音场景对延迟极敏感 (一次 LLM 往返
        #: 1~3 秒), 所以热词纠错必须放在**不花网络往返**的位置。
        self.hotwords = load_corrector(cfg.hotwords)

    # ------------------------------------------------------------ 状态

    @property
    def busy(self) -> bool:
        """是否正在等待 Agent 或播放语音 (期间不做识别, 避免自识别)。"""
        return self.state in (State.THINK, State.SAY)

    def _set_state(self, state: State) -> None:
        with self._lock:
            self.state = state
        logger.info("[状态] -> %s", state.value)

    def set_rec_state(self, state) -> None:
        """更新**录音**阶段的状态 (等待唤醒词 / 正在录音)。

        由 ASR 侧在真正开始/停止录音时调用 —— 监控端靠它区分
        「还没触发唤醒词」和「正在识别」。

        Args:
            state: ``RecState`` 成员或它的字符串值。
        """
        try:
            new_state = RecState(state)
        except ValueError:
            logger.warning("[录音状态] 未知取值, 忽略: %r", state)
            return

        with self._lock:
            if self.rec_state == new_state:
                return
            self.rec_state = new_state
        logger.info("[录音状态] -> %s", new_state.value)

    def mark_rec_result(self, text: str) -> None:
        """标记「这一轮识别完成了」。

        识别完成的**瞬间**就记下来 —— 不等 Agent 分流、不等 TTS 播完。
        监控端据此显示「识别完成」动画。

        同时把录音状态退回「等待唤醒词」—— 识别已经结束了。
        """
        with self._lock:
            self.rec_result = {
                "text": text,
                "at": time.time(),
                "turn": self.turn_count,
            }
            self.rec_state = RecState.WAITING

    def end_session(self, reason: str = "") -> None:
        """**结束会话** —— 用户说了结束词, 这一轮之后必须重新唤醒。

        置位 ``session_ended`` 之后, ``on_turn_complete`` 回调会被
        跳过 —— 那个回调是用来开启连续对话窗口的, 结束词之后不能开。

        Args:
            reason: 结束原因 (写进日志, 便于排查)。
        """
        with self._lock:
            already = self.session_ended
            self.session_ended = True
        if not already:
            logger.info("[会话] 已结束 (%s), 需要重新唤醒", reason or "结束词")

    def resume_session(self) -> None:
        """**恢复会话** —— 用户重新说了唤醒词, 连续对话窗口可以再开了。

        上一轮是被结束词关掉的, 这一轮是新的唤醒, 所以把
        ``session_ended`` 清掉。
        """
        with self._lock:
            if not self.session_ended:
                return
            self.session_ended = False
        logger.info("[会话] 已重新唤醒, 恢复连续对话")

    def _notify_turn_complete(self) -> None:
        """通知外部「这一轮彻底结束了」。

        放在锁外调用, 避免回调里再取锁造成死锁。

        **会话已被结束词关闭时跳过** —— 这个回调用来开启连续对话窗口,
        结束词之后不能再开, 否则「再见」就白说了。
        """
        with self._lock:
            if self.session_ended:
                return
        cb = self.on_turn_complete
        if cb is None:
            return
        try:
            cb()
        except Exception as exc:  # pragma: no cover - 回调异常不该影响主流程
            logger.warning("turn_complete 回调异常: %s", exc)

    def _notify_before_speak(self) -> None:
        """通知外部「马上要播报了」。

        ASR 侧在这里架回声守卫 —— 必须在 TTS 开口之前, 否则挡不住
        播报本身的回声。
        """
        cb = self.on_before_speak
        if cb is None:
            return
        try:
            cb()
        except Exception as exc:  # pragma: no cover
            logger.warning("before_speak 回调异常: %s", exc)

    def _notify_after_speak(self) -> None:
        """通知外部「播报结束了」。

        ASR 侧在这里把回声守卫延伸到「播完 + 沉降期」。
        """
        cb = self.on_after_speak
        if cb is None:
            return
        try:
            cb()
        except Exception as exc:  # pragma: no cover
            logger.warning("after_speak 回调异常: %s", exc)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            state = self.state.value
            last = self.last
            errors = list(self.errors)
            turns = self.turn_count
            rec_state = self.rec_state.value
            rec_result = dict(self.rec_result) if self.rec_result else None
        return {
            "status": "ok",
            "state": state,
            "turns": turns,
            # ---- 录音阶段 (监控端画三态动画用) ----
            #:
            #: "WAITING" = 还没触发唤醒词; "RECORDING" = 正在识别。
            "rec_state": rec_state,
            #: 最近一次「识别完成」的标记: {"text", "at", "turn"}。
            #: 识别完成的那一瞬间就写进来了 —— 不等 Agent/TTS。
            "rec_result": rec_result,
            "wake_words": self.cfg.asr.wake_words,
            #: 结束词 (会话结束语)。为空 = 未启用结束词。
            "end_words": self.cfg.asr.end_word_list,
            #: 结束词的别名。命中任意一个都算结束。
            "end_aliases": self.cfg.asr.end_alias_list,
            #: 是否启用结束词的拼音模糊兜底匹配。
            "end_match_fuzzy": self.cfg.asr.end_match_fuzzy,
            #: 拼音模糊匹配的相似度阈值 (0~1)。
            "end_match_threshold": self.cfg.asr.end_match_threshold,
            #: 命中结束词时的收尾播报文案。留空 = 不播报。
            "end_prompt": self.cfg.asr.end_prompt or "",
            "asr_model": self.cfg.asr.model_path or "(自动查找)",
            "agent_url": self.cfg.agent.dispatch_url,
            "tts_url": self.cfg.tts.ws_url,
            "last_text": last.text,
            "last_intent": last.intent,
            "last_speak": last.speak,
            "last_reply": last.reply,
            "last_action": last.action,
            "timings": last.timings,
            "hotword_fix": last.hotword_fix,
            "hotwords_enabled": self.hotwords is not None,
            "hotword_count": (
                len(self.hotwords.known_words()) if self.hotwords else 0
            ),
            "errors": errors[-10:],
        }

    def _record_error(self, message: str) -> None:
        with self._lock:
            self.errors.append(message)
        logger.error("[错误] %s", message)

    # ------------------------------------------------------------ 主流程

    def submit(
        self,
        text: str,
        record_ms: int = 0,
        words: Optional[List[Dict[str, Any]]] = None,
        after_prompt: str = "",
    ) -> None:
        """提交一轮识别结果, 在后台线程里跑完整条链路。

        立刻置 THINK 状态, 主循环随即停止识别 (避免把自己说的话再识别一遍),
        同时避免主线程被 TTS 播放阻塞。

        Args:
            text: 识别出的文本。
            record_ms: 录音+识别阶段耗时 (毫秒)。
            words: 词级识别结果 (``[{"word":..., "conf":...}]``),
                用于热词纠错的置信度打分。
            after_prompt: **正文播完之后**再播的一段话 (如「好的，我走了」)。

                用于结束词场景: 用户说「查下天气再见」, 先播天气,
                再播这句告别 —— 顺序不能反, 反了就显得机器人在敷衍。

                留空 = 不播。
        """
        text = (text or "").strip()
        if not text:
            # 没识别到内容: 走 TTS 播报提示语, 让用户知道要再说一次
            self._record_error(self._empty_result_message())
            self._prompt_empty_result()
            return

        # ---- 热词纠错 (本地, 零延迟) ----
        # 必须在交给 Agent **之前**做: Agent 的规则匹配和 tool calling 都靠
        # 文本里的专业名词才能匹配上 (公司名、产品名往往就是命令里的关键词)。
        text = self._correct_hotwords(text, words)

        self._set_state(State.THINK)
        with self._lock:
            self.turn_count += 1
            # **先把识别文本记下来** —— 这一步在等 Agent 之前,
            # 监控端才能实时看到「用户说了什么」。等 _run_turn 跑完再记的话,
            # 中间那几秒的 THINK 阶段监控端是空的。
            self.last.text = text
            self.last.at = time.time()
        # 「识别完成」这一刻 —— 立刻标记, 不等 Agent 分流。
        # (新的一轮会把 rec_result 覆盖掉, 上一轮的完成动画不会重播)
        self.mark_rec_result(text)
        worker = threading.Thread(
            target=self._run_turn,
            args=(text, record_ms, after_prompt),
            name="pipeline-turn",
            daemon=True,
        )
        worker.start()

    def _correct_hotwords(
        self, text: str, words: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """热词纠错: 把识别文本里的专业名词换成规范写法。

        纠错失败**不能影响主流程** —— 抛异常就原样放行。
        """
        if self.hotwords is None:
            return text
        try:
            result = self.hotwords.correct(text, words)
        except Exception as exc:  # pragma: no cover - 兜底
            logger.warning("[热词纠错] 异常, 原样放行: %s", exc)
            return text
        if result.changed:
            with self._lock:
                self.last.hotword_fix = result.as_dict()
        return result.after

    def _empty_result_message(self) -> str:
        """没听清时的提示文案。

        错误记录里存的话和 TTS 播报的话必须一致 —— 否则会出现
        「日志说一套、机器人说另一套」的情况, 排查时容易误判。
        """
        prompt = (self.cfg.asr.empty_result_prompt or "").strip()
        return prompt or "没听清内容，请再说一遍"

    def _prompt_empty_result(self) -> None:
        """没听清时用 TTS 播报一句提示 (如 "没听清内容，请再说一遍")。"""
        prompt = (self.cfg.asr.empty_result_prompt or "").strip()
        if not prompt:
            self._set_state(State.IDLE)
            return

        self._set_state(State.SAY)
        # **播报前**架回声守卫 —— 挡住「没听清」这句提示被自己听到
        self._notify_before_speak()
        try:
            result = self.tts.speak(prompt)
            logger.info(
                "[提示] 已播报 %r (%dms, %d 帧)",
                prompt,
                result.duration_ms,
                result.frames,
            )
        except TTSError as exc:
            self._record_error(f"TTS 提示播报失败: {exc}")
        except Exception as exc:  # pragma: no cover - 兜底
            self._record_error(f"TTS 提示播报异常: {exc}")
        finally:
            # 播完了 (不管成功还是失败) —— 把回声守卫延伸到沉降期
            self._notify_after_speak()

        self._set_state(State.IDLE)
        # 没听清也算一轮结束, 连续对话窗口继续开着 (用户可以马上再说一次)
        self._notify_turn_complete()

    def speak_direct(self, text: str) -> None:
        """直接播一段文字 (不经过 Agent 分流)。

        用于**结束词**的收尾话 —— 用户说「再见」之后回一句「再见」,
        这不是一次问答, 不该走 Agent (那会多一次 LLM 往返的延迟)。

        播完**不会**开启连续对话窗口 —— 结束词意味着会话已经结束,
        需要重新唤醒。
        """
        text = (text or "").strip()
        if not text:
            return

        self._set_state(State.SAY)
        # **播报前**架回声守卫 —— 挡住收尾话被自己听到
        self._notify_before_speak()
        try:
            result = self.tts.speak(text)
            logger.info(
                "[播报] 已播报 %r (%dms, %d 帧)",
                text,
                result.duration_ms,
                result.frames,
            )
        except TTSError as exc:
            self._record_error(f"TTS 播报失败: {exc}")
        except Exception as exc:  # pragma: no cover - 兜底
            self._record_error(f"TTS 播报异常: {exc}")
        finally:
            # 播完了 (不管成功还是失败) —— 把回声守卫延伸到沉降期
            self._notify_after_speak()

        self._set_state(State.IDLE)
        # 故意**不**调 _notify_turn_complete(): 结束词之后不该重开
        # 连续对话窗口, 否则「再见」就白说了。

    def _run_turn(self, text: str, record_ms: int = 0, after_prompt: str = "") -> None:
        """后台线程: Agent 分流 -> (必要时) TTS 播放。

        Args:
            text: 识别出的文本。
            record_ms: 录音+识别阶段耗时 (毫秒)。
            after_prompt: 正文播完之后**再播**的一段话 (如「好的，我走了」)。
                留空 = 不播。
        """
        logger.info("[识别] %s", text)

        turn_start = time.time()

        # 1) 交给 Agent 做语义识别与分流
        t_agent = time.time()
        try:
            reply = self.agent.dispatch(text)
        except AgentError as exc:
            self._record_error(f"Agent 调用失败: {exc}")
            self._set_state(State.IDLE)
            # Agent 挂了也结束这一轮, 让用户可以重新说
            self._notify_turn_complete()
            # 收尾话还是要说的 —— 用户说了「再见」, 机器人一声不吭很突兀
            self._speak_after_prompt(after_prompt)
            return
        agent_ms = int((time.time() - t_agent) * 1000)

        with self._lock:
            self.last.text = text
            self.last.intent = reply.intent
            self.last.speak = reply.speak
            self.last.reply = reply.reply
            self.last.action = reply.action
            self.last.trace = reply.trace
            self.last.at = time.time()

        # 2) 按分流结果决定下游: 工具类分支不进 TTS
        if not reply.speak:
            logger.info(
                "[分流] intent=%s (工具类分支, 不朗读) action=%s",
                reply.intent,
                reply.action,
            )
            with self._lock:
                self.last.timings = {
                    "asr_record_ms": record_ms,
                    "agent_total_ms": agent_ms,
                    "agent_server_ms": reply.server_ms,
                    "tts_total_ms": 0,
                    "turn_total_ms": agent_ms,
                    "end_to_end_ms": record_ms + agent_ms,
                }
            self._set_state(State.IDLE)
            # 工具分支不朗读, 这一轮立即结束
            self._notify_turn_complete()
            # 收尾话还是要说的 —— 用户说了「再见」, 机器人一声不吭很突兀
            self._speak_after_prompt(after_prompt)
            return

        if not reply.reply.strip():
            logger.info("[分流] intent=%s 但回复为空, 跳过朗读", reply.intent)
            self._set_state(State.IDLE)
            self._notify_turn_complete()
            self._speak_after_prompt(after_prompt)
            return

        # 3) 需要朗读: 进 TTS 合成并直接播放 (不落盘)
        self._set_state(State.SAY)
        # **播报前**架回声守卫 —— 挡住机器人自己的声音被麦克风收进去
        self._notify_before_speak()
        t_tts = time.time()
        try:
            result = self.tts.speak(reply.reply)
            tts_ms = int((time.time() - t_tts) * 1000)
            logger.info(
                "[朗读] 总计 %dms | 连接 %dms | 首帧 %dms | 合成 %dms | 播放收尾 %dms"
                " | 音频时长 %dms | %d 帧",
                tts_ms,
                result.connect_ms,
                result.first_frame_ms,
                result.synth_ms,
                result.play_ms,
                result.audio_ms,
                result.frames,
            )
        except TTSError as exc:
            tts_ms = int((time.time() - t_tts) * 1000)
            self._record_error(f"TTS 合成失败: {exc}")
        except Exception as exc:  # pragma: no cover - 兜底
            tts_ms = int((time.time() - t_tts) * 1000)
            self._record_error(f"TTS 异常: {exc}")
        finally:
            # 播完了 (不管成功还是失败) —— 把回声守卫延伸到沉降期
            self._notify_after_speak()

        total_ms = int((time.time() - turn_start) * 1000)
        timings = {
            "asr_record_ms": record_ms,
            "agent_total_ms": agent_ms,
            "agent_server_ms": reply.server_ms,
            "tts_total_ms": tts_ms,
            "turn_total_ms": total_ms,
            "end_to_end_ms": record_ms + total_ms,
        }
        with self._lock:
            self.last.timings = timings

        # 汇总: 一眼看出慢在哪
        logger.info(
            "[全链路计时] 语音->文字 %dms | Agent %dms (服务端 %dms) | TTS %dms"
            " | 文字->语音 %dms | 端到端 %dms",
            record_ms,
            agent_ms,
            reply.server_ms,
            tts_ms,
            total_ms,
            record_ms + total_ms,
        )

        self._set_state(State.IDLE)
        # 播报完成, 这一轮才算真正结束 —— 此时才开启连续对话窗口,
        # 让机器人自己说话的时间不占用用户的 30 秒。
        self._notify_turn_complete()

        # ---- 收尾话: 正文播完之后才说 ----
        #
        # 「查下天气再见」-> 先播天气, 再播「好的，我走了」。
        # 顺序不能反 —— 反了就显得机器人在敷衍。
        self._speak_after_prompt(after_prompt)

    def _speak_after_prompt(self, prompt: str) -> None:
        """正文播完之后再播一段收尾话 (如「好的，我走了」)。

        走 :meth:`speak_direct` 而不是普通分支 —— 它**不会**重开连续
        对话窗口。结束词意味着会话已经结束, 说完了告别就该回到「等待
        唤醒词」, 不能再听下一句。

        播失败也不该把整轮拖垮 —— 收尾话是锦上添花, 正文才是正事。
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return
        try:
            self.speak_direct(prompt)
        except Exception as exc:  # pragma: no cover - 兜底
            self._record_error(f"收尾话播报异常: {exc}")

    def on_recording_complete(
        self,
        text: str,
        record_ms: int = 0,
        words: Optional[List[Dict[str, Any]]] = None,
        after_prompt: str = "",
    ) -> None:
        """识别完成后的编排入口。由 WakeupASR 子类调用。

        Args:
            text: 识别出的文本。
            record_ms: 录音+识别阶段耗时 (毫秒)。
            words: 词级识别结果, 供热词纠错打分。
            after_prompt: 正文播完之后**再播**的一段话 (如「好的，我走了」)。
        """
        self.submit(
            text, record_ms=record_ms, words=words, after_prompt=after_prompt
        )

    def wait_idle(self, timeout: float = 300.0, turn: int = 0) -> bool:
        """等待指定的那一轮 (Agent + TTS) 跑完。

        turn 是要等待的轮次序号 (由 turn_count 递增)。超时返回 False。
        """
        deadline = time.time() + timeout
        # 先等这一轮被提交
        while time.time() < deadline and self.turn_count < turn:
            time.sleep(0.05)
        if self.turn_count < turn:
            return False
        # 再等它跑完
        while time.time() < deadline and self.busy:
            time.sleep(0.1)
        return not self.busy

    def close(self) -> None:
        try:
            self.tts.close()
        except Exception:
            pass
