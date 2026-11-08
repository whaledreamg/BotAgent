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
from tts_client import TTSClient, TTSError

logger = logging.getLogger("pipeline")


class State(str, Enum):
    IDLE = "IDLE"
    REC = "REC"
    THINK = "THINK"
    SAY = "SAY"


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


class Pipeline:
    """ASR -> Agent -> (TTS -> 播放) 的编排器。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.agent = AgentClient(cfg.agent, session_id=cfg.asr.session_id)
        self.tts = TTSClient(cfg.tts)
        self.state = State.IDLE
        self.last = LastTurn()
        self.errors: List[str] = []
        self.turn_count = 0
        self._lock = threading.Lock()
        #: 一轮完全结束 (Agent + TTS 播完) 时的回调。
        #: ASR 侧用它来开启连续对话窗口。
        self.on_turn_complete: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------ 状态

    @property
    def busy(self) -> bool:
        """是否正在等待 Agent 或播放语音 (期间不做识别, 避免自识别)。"""
        return self.state in (State.THINK, State.SAY)

    def _set_state(self, state: State) -> None:
        with self._lock:
            self.state = state
        logger.info("[状态] -> %s", state.value)

    def _notify_turn_complete(self) -> None:
        """通知外部「这一轮彻底结束了」。

        放在锁外调用, 避免回调里再取锁造成死锁。
        """
        cb = self.on_turn_complete
        if cb is None:
            return
        try:
            cb()
        except Exception as exc:  # pragma: no cover - 回调异常不该影响主流程
            logger.warning("turn_complete 回调异常: %s", exc)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            state = self.state.value
            last = self.last
            errors = list(self.errors)
            turns = self.turn_count
        return {
            "status": "ok",
            "state": state,
            "turns": turns,
            "wake_words": self.cfg.asr.wake_words,
            "asr_model": self.cfg.asr.model_path or "(自动查找)",
            "agent_url": self.cfg.agent.dispatch_url,
            "tts_url": self.cfg.tts.ws_url,
            "last_text": last.text,
            "last_intent": last.intent,
            "last_speak": last.speak,
            "last_reply": last.reply,
            "last_action": last.action,
            "timings": last.timings,
            "errors": errors[-10:],
        }

    def _record_error(self, message: str) -> None:
        with self._lock:
            self.errors.append(message)
        logger.error("[错误] %s", message)

    # ------------------------------------------------------------ 主流程

    def submit(self, text: str, record_ms: int = 0) -> None:
        """提交一轮识别结果, 在后台线程里跑完整条链路。

        立刻置 THINK 状态, 主循环随即停止识别 (避免把自己说的话再识别一遍),
        同时避免主线程被 TTS 播放阻塞。
        """
        text = (text or "").strip()
        if not text:
            # 没识别到内容: 走 TTS 播报提示语, 让用户知道要再说一次
            self._record_error(self._empty_result_message())
            self._prompt_empty_result()
            return

        self._set_state(State.THINK)
        with self._lock:
            self.turn_count += 1
        worker = threading.Thread(
            target=self._run_turn,
            args=(text, record_ms),
            name="pipeline-turn",
            daemon=True,
        )
        worker.start()

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

        self._set_state(State.IDLE)
        # 没听清也算一轮结束, 连续对话窗口继续开着 (用户可以马上再说一次)
        self._notify_turn_complete()

    def _run_turn(self, text: str, record_ms: int = 0) -> None:
        """后台线程: Agent 分流 -> (必要时) TTS 播放。"""
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
            return

        if not reply.reply.strip():
            logger.info("[分流] intent=%s 但回复为空, 跳过朗读", reply.intent)
            self._set_state(State.IDLE)
            self._notify_turn_complete()
            return

        # 3) 需要朗读: 进 TTS 合成并直接播放 (不落盘)
        self._set_state(State.SAY)
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

    def on_recording_complete(self, text: str, record_ms: int = 0) -> None:
        """识别完成后的编排入口。由 WakeupASR 子类调用。"""
        self.submit(text, record_ms=record_ms)

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
