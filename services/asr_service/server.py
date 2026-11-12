"""ASR 服务入口。

ASR 是语音链路的**第一环**: 麦克风 -> 唤醒词 -> 识别文本。
识别文本交给 Agent 服务做语义识别与分流:
- 分流结果 speak=True  -> 走 TTS 合成并直接播放 (不落盘)
- 分流结果 speak=False -> 工具类分支已直接执行动作, 不走 TTS

对外接口 (标准库 http.server, 不需要额外依赖):
- GET /health  健康检查与当前状态
- GET /state   最近一轮的识别/分流/执行详情

用法:
    conda run -n asrt python server.py --wake "你好助手"
    conda run -n asrt python server.py --replay test.wav --wake "你好助手"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wakeup_asr
from config import load_config
from pipeline import Pipeline
from wakeup_asr import rms_db

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="[%(asctime)s][%(levelname)s][%(name)s] %(message)s",
)
logger = logging.getLogger("asr")

#: 连续对话模式下的「唤醒词」占位标记。
#: 用来区分「用户说了唤醒词」和「在连续对话窗口内直接说话」两种录音入口,
#: 好让终端提示语不一样。
CONTINUOUS_WAKE_LABEL = "(连续对话)"

#: 连续对话模式下的语音起始判定: 需要连续这么多块检测到人声才算「用户开口」。
#: 每块约 256ms (16kHz / 4096 样本), 2 块约 512ms。
#: 设太小会被瞬时撞击声误触发, 设太大会让响应显得迟钝。
VOICE_RUN_BLOCKS = 2

#: 回声拒绝期里, 需要连续这么多块**安静**才确认余音已消散。
#:
#: 余音是断断续续的 —— 声波在房间里反射叠加, 能量会有起伏, 声音
#: 之间有间隙。只看一块安静就放开, 会在余音的间隙里误触发, 把
#: 机器人自己的声音当成用户开口。
#:
#: 每块约 256ms, 3 块约 768ms。
ECHO_QUIET_BLOCKS = 3


class VoiceASR(wakeup_asr.WakeupASR):
    """带 Agent 分流与 TTS 朗读的语音助手。

    支持**连续对话**: 一轮结束后, 在 `asr.continuous_timeout` 秒内(默认 30s)
    用户再次说话不需要唤醒词, 直接进入录音; 超过这个时间则回到等待唤醒状态。

    窗口的计时从「本轮播报结束」开始, 播放语音期间不计入 ——
    机器人自己说话的时间不该算在用户的 30 秒里。
    """

    def __init__(self, args, pipeline: Pipeline) -> None:
        self.pipeline = pipeline
        self.skip_wake = bool(getattr(args, "skip_wake", False))
        #: 连续对话窗口长度(秒); 0 表示关闭
        self.continuous_timeout = float(
            getattr(args, "continuous_timeout", 0.0) or 0.0
        )
        #: 连续对话窗口的到期时刻; None 表示当前不在连续对话中
        self._session_until: Optional[float] = None
        #: 连续对话窗口的起点; 用于过滤掉 TTS 播放期间录到的音频
        self._session_started_ts: float = 0.0
        #: 是否正在「等用户开口」 (连续对话模式)
        self._waiting_speech = False
        #: 连续有声块计数 (VAD 去抖)
        self._voice_run = 0
        #: 回声拒绝期里连续安静的块数 (确认余音消散用)
        self._quiet_run = 0
        #: **回声抑制**: 播报结束后的静默沉降期 (秒)。
        #:
        #: TTS 播完之后, 扬声器的余音还在空气里传播 (声波 + 房间混响 +
        #: ALSA 缓冲)。这段时间内麦克风录到的是机器人自己的声音 ——
        #: 如果当成人声触发录音, 下一轮就会把播报内容当成用户的话。
        self.echo_settle_sec = float(
            getattr(args, "echo_settle", 0.0) or 0.0
        )
        #: 等安静的最长等待 (秒) —— 沉降期之后最多再等这么久确认房间安静
        self.echo_quiet_timeout = float(
            getattr(args, "echo_quiet_timeout", 2.0) or 0.0
        )
        super().__init__(args)

    # 不落盘: 禁止保存识别音频
    def save_wav(self):
        return None

    # ------------------------------------------------------------ 连续对话

    @property
    def in_continuous_session(self) -> bool:
        """当前是否处于连续对话窗口内。"""
        if not self.continuous_timeout or self._session_until is None:
            return False
        return time.time() < self._session_until

    def _session_remaining(self) -> float:
        """连续对话窗口还剩多少秒。"""
        if not self.continuous_timeout or self._session_until is None:
            return 0.0
        return max(0.0, self._session_until - time.time())

    def open_session(self) -> None:
        """开启连续对话窗口 (新一轮开始时调用)。"""
        if not self.continuous_timeout:
            return
        self._session_until = time.time() + self.continuous_timeout
        # 记录窗口起点: 环形缓冲里比这个时刻更早的音频是机器人自己
        # 说话时录到的, 不能进下一轮的前滚, 否则会把 TTS 的声音
        # 当成用户的话识别出来。
        self._session_started_ts = time.time()
        # 进入「等用户开口」状态: 窗口内不需要唤醒词, 但也不能一进窗口
        # 就录音 (否则环境噪声会被当成用户开口)
        self._waiting_speech = True
        self._voice_run = 0
        logger.info(
            "[连续对话] 已开启, %.0fs 内再次说话无需唤醒词",
            self.continuous_timeout,
        )

    def arm_echo_guard(self, settle_sec: Optional[float] = None) -> None:
        """**在播报开始前**调用, 声明「接下来这段时间别听我的声音」。

        这是回声抑制的关键 —— 必须在 TTS 开口**之前**就把守卫架起来,
        不能等播完了再补:

        - 播报**期间**录到的音频: 直接丢弃 (时间戳早于守卫结束时刻)
        - 播报**之后**的余音: 要求能量回落到噪声底附近才认作用户开口

        之前的问题是守卫在播完之后才架, 结果播报最后那几帧的余音
        时间戳反而比守卫还新, 过滤不掉, 直接被当成用户说话。

        Args:
            settle_sec: 沉降期秒数。None = 用配置值。
                这是「播报结束 -> 可以开始听用户说话」的最短等待时间,
                要覆盖声波传播 + 混响 + ALSA 缓冲排空的时间。
        """
        if settle_sec is None:
            settle_sec = self.echo_settle_sec
        settle = float(settle_sec or 0.0)

        # 守卫覆盖「从现在开始直到播报结束 + 沉降期」
        #
        # 这里先用一个保守的下界占位 —— 真正的结束时刻在播报结束后
        # 由 finalize_echo_guard() 修正 (那时才知道播了多久)。
        now = time.time()
        self.echo_guard_until = now + settle
        self.echo_reject_until = now + settle
        self.echo_quiet_at = 0.0
        self._quiet_run = 0
        logger.info("[回声守卫] 已架起 (沉降期 %.2fs)", settle)

    def finalize_echo_guard(self, settle_sec: Optional[float] = None) -> None:
        """播报结束后调用, 把守卫延伸到「播报结束 + 沉降期」。

        ``arm_echo_guard`` 只知道「现在要播了」, 不知道播了多久。
        播完之后才知道真正的余音窗口在哪。

        **两个时间窗口不是一回事**:

        - ``echo_guard_until`` = 播完 + 沉降期。这段时间音频**整个丢弃**,
          根本不看电平。
        - ``echo_reject_until`` = 播完 + 沉降期 + 等安静的上限。这段时间
          音频照收, 但 VAD 不认作人声, 直到房间真的安静下来。

        两者必须分开 —— 如果用同一个时间戳, 守卫期把音频全丢了,
        等守卫结束时拒绝期也正好到期, 「等安静」的逻辑就永远不会触发
        (余音还在就已经放行了)。
        """
        if settle_sec is None:
            settle_sec = self.echo_settle_sec
        settle = float(settle_sec or 0.0)
        now = time.time()

        # 硬丢弃期: 播完 + 沉降期
        self.echo_guard_until = now + settle
        # 等安静期: 在硬丢弃期之后, 再给一段「确认房间安静」的时间
        self.echo_reject_until = now + settle + self.echo_quiet_timeout
        self.echo_quiet_at = 0.0
        self._quiet_run = 0
        logger.info(
            "[回声守卫] 沉降期 %.2fs, 等安静上限 %.2fs (到 %.2f)",
            settle,
            self.echo_quiet_timeout,
            self.echo_reject_until,
        )

    def close_session(self) -> None:
        """关闭连续对话窗口。"""
        if self._session_until is not None:
            self._session_until = None
            self._waiting_speech = False
            self._voice_run = 0
            logger.info("[连续对话] 已关闭, 需要重新唤醒")

    def start_recording(self, ts, wake):
        """开始录音。连续对话模式下的提示语与前滚策略不同。"""
        if wake == CONTINUOUS_WAKE_LABEL:
            remaining = self._session_remaining()
            print(f"\n[连续对话] 窗口内 (剩 {remaining:.0f}s), 请继续说话...")
            self._start_continuous_recording(ts)
            return
        super().start_recording(ts, wake)

    def _start_continuous_recording(self, ts) -> None:
        """连续对话模式下的录音启动。

        与基类的区别只有一处: 前滚音频只取「窗口开启之后」的片段。

        基类的前滚会拿环形缓冲里最近 0.5 秒的音频, 用来避免吞掉
        紧跟唤醒词的第一句话。但连续对话里, 环形缓冲可能还留着
        TTS 播放时录到的机器人自己的声音 —— 那部分必须排除掉,
        否则会被识别成用户说的话。
        """
        self.state = "RECORDING"
        self.rec_chunks = []
        self.rec_start_ts = ts
        self.last_voice_ts = ts
        self.final_texts = []
        self.partial_text = ""
        self.asr_rec = wakeup_asr.KaldiRecognizer(self.model, wakeup_asr.SAMPLE_RATE)

        # 重置唤醒检测器: 连续对话窗口过期后要重新靠唤醒词进入,
        # 检测器里残留的旧文本会让下一次唤醒判定出错。
        self.kw_rec = (
            wakeup_asr.KaldiRecognizer(self.model, wakeup_asr.SAMPLE_RATE, self.grammar)
            if self.grammar_supported
            else None
        )
        self.gen_rec = wakeup_asr.KaldiRecognizer(self.model, wakeup_asr.SAMPLE_RATE)
        self.kw_text = ""
        self.gen_text = ""

        # 前滚: 只带「安全时刻」之后的音频
        #
        # 安全时刻 = max(窗口开启时刻, 回声守卫结束时刻)。
        #
        # **为什么要取 max**: 回声守卫结束时刻比窗口开启时刻晚 (播报
        # 之后还有沉降期)。只按窗口时刻过滤的话, 播报尾声的余音
        # (时间戳在两者之间) 会被带进前滚, 直接变成「用户说的话」。
        preroll = []
        safe_from = max(self._session_started_ts, self.echo_guard_until)
        for rts, chunk in list(self.ring):
            if rts < safe_from:
                continue
            if ts - rts <= self.preroll_sec:
                preroll.append(chunk)
        for chunk in preroll:
            self.rec_chunks.append(chunk)
            self.asr_rec.AcceptWaveform(chunk)

    def process_block(self, data, ts):
        """等待 Agent / 播放语音期间不做识别, 避免把自己的声音再识别一遍。"""
        if self.pipeline.busy:
            return False

        # **回声守卫期内**: 收到的音频一律丢弃, 不进环形缓冲也不参与 VAD。
        #
        # 这段时间是「播报刚结束」的沉降期 —— 麦克风里全是机器人自己的
        # 余音。放进来只会有两种坏结果:
        #   1. 被 VAD 当成用户开口 -> 立刻开始录音 -> 识别出自己的播报
        #   2. 进了环形缓冲 -> 下一轮的前滚把它带进识别
        if ts < self.echo_guard_until:
            return False

        if self.state == "IDLE":
            # 连续对话窗口内: 等用户开口, 不需要唤醒词
            if self.in_continuous_session:
                if self._waiting_speech:
                    if self._voice_detected(data, ts):
                        self._waiting_speech = False
                        remaining = self._session_remaining()
                        logger.info(
                            "[连续对话] 检测到语音 (窗口剩 %.1fs), 开始录音",
                            remaining,
                        )
                        self.start_recording(ts, CONTINUOUS_WAKE_LABEL)
                        return True
                    # 还没人说话, 继续等
                    return False

                # 已经不在等语音的阶段, 正常往下走

            # 窗口刚过期: 明确提示用户需要重新唤醒
            if self._session_until is not None:
                self.close_session()

            if self.skip_wake:
                # --no-wake: 跳过唤醒词判定直接进入录音。
                # 仅用于链路自测(回放模式), 不改变正常的唤醒词识别逻辑。
                wake = next(iter(self.tokens), "") if self.tokens else ""
                self.start_recording(ts, wake)
                return True

        return super().process_block(data, ts)

    def _voice_detected(self, data, ts: Optional[float] = None) -> bool:
        """判断这一块音频里有没有人声 (VAD)。

        连续对话时不能一进窗口就开始录音 —— 那会把环境噪声当成用户开口,
        录 1.2 秒静音后以「没听清」收场, 白白等一个静音超时。

        需要**连续** VOICE_RUN_BLOCKS 块都有人声才认定为「用户开口」,
        中间只要断一下就重新计数, 避免瞬时撞击声误触发。

        **回声拒绝**: 刚播报完时, 即使电平超阈值也不算人声 —— 那还是
        机器人自己的余音。要等声音真的落到噪声底附近, 确认是「新的」
        人声, 才开始录音。
        """
        db = rms_db(data)
        thresh = max(self.noise_floor + 10.0, self.args.vad_threshold)
        now = ts if ts is not None else time.time()

        # ---- 回声拒绝期 ----
        #
        # 余音可能比预想的久 (大房间混响)。所以不能简单地「等固定
        # 时间就恢复」—— 要**等到真的安静下来**。
        #
        # 但也不能无限等: 房间本来就吵的话会永远等下去。所以有个
        # 硬上限 (echo_reject_until), 到点就强制恢复。
        if self.echo_reject_until and now < self.echo_reject_until:
            if db <= thresh:
                # 一块安静不够 —— 余音是断断续续的, 声音之间有间隙,
                # 间隙里电平会掉下去。只看一块就放开, 会在余音的
                # 间隙里误触发。要**连续**安静 ECHO_QUIET_BLOCKS 块
                # 才认账。
                self._quiet_run += 1
                if self._quiet_run >= ECHO_QUIET_BLOCKS:
                    self.echo_quiet_at = now
                    self.echo_reject_until = 0.0
                    self._quiet_run = 0
                    logger.info("[回声守卫] 余音已消散, 恢复接受人声")
            else:
                # 又吵起来了 —— 余音还在, 重新计数
                self._quiet_run = 0
            return False

        # 硬上限到了, 但一直没安静下来 —— 强制放行, 免得机器人变聋
        if self.echo_reject_until and now >= self.echo_reject_until:
            logger.info("[回声守卫] 超过等待上限, 强制恢复接受人声")
            self.echo_reject_until = 0.0
            self.echo_quiet_at = now

        if db <= thresh:
            self._voice_run = 0
            return False
        self._voice_run += 1
        return self._voice_run >= VOICE_RUN_BLOCKS

    def run_replay(self, path):
        """回放测试。

        与基类的区别: 节奏按**真实时间**走 (基类是加速 5 倍回放)。

        连续对话的验证必须用真实节奏 —— 加速回放时, 第二段语音会在
        第一轮的 Agent/TTS 还没跑完时就到了, 会被 busy 状态挡掉,
        测不出「窗口内无需唤醒词」这个行为。

        ``--replay-speed`` 可以调快慢, 1.0 = 真实时间。
        """
        import wave as _wave

        with _wave.open(path, "rb") as wf:
            if wf.getframerate() != wakeup_asr.SAMPLE_RATE:
                raise SystemExit(
                    f"回放文件必须是 {wakeup_asr.SAMPLE_RATE}Hz, "
                    f"当前 {wf.getframerate()}Hz"
                )
            raw = wf.readframes(wf.getnframes())

        print("\n[*] 回放模式")
        for w, toks in self.tokens.items():
            print(f"[*] 唤醒词: {w}  (词表切分: {' + '.join(toks)})")
        print()

        step = wakeup_asr.BLOCK_SIZE * 2
        speed = float(getattr(self.args, "replay_speed", 1.0) or 1.0)
        block_sec = step / 2 / wakeup_asr.SAMPLE_RATE
        t0 = time.time()
        for i in range(0, len(raw), step):
            chunk = raw[i:i + step]
            ts = t0 + i / 2 / wakeup_asr.SAMPLE_RATE
            self.ring.append((ts, chunk))
            self.update_noise_floor(rms_db(chunk))
            self.process_block(chunk, ts)
            # 按真实节奏等待 (speed=1.0 时与音频时长一致)
            target = t0 + (i + step) / 2 / wakeup_asr.SAMPLE_RATE / speed
            delay = target - time.time()
            if delay > 0:
                time.sleep(delay)
        if self.state == "RECORDING":
            self.stop_recording("回放结束")

    def on_recording_complete(self, text, wav_path=None):
        """识别结束后交给编排器: Agent 分流 -> (必要时) TTS 播放。"""
        # 录音阶段耗时: 从开始录音到识别结束 (不含用户说话本身的时间)
        record_ms = 0
        if getattr(self, "rec_start_ts", None):
            record_ms = int((time.time() - self.rec_start_ts) * 1000)
        logger.info("[计时] ASR 录音+识别阶段 %dms, 识别文本长度 %d", record_ms, len(text or ""))
        self.pipeline.on_recording_complete(text, record_ms=record_ms)


class HealthHandler(BaseHTTPRequestHandler):
    """提供 /health 与 /state 两个只读接口。"""

    pipeline: Pipeline = None  # type: ignore[assignment]

    def _send(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/health", "/", "/state"):
            self._send(self.pipeline.snapshot())
        else:
            self._send({"error": "not found", "path": path}, 404)

    def log_message(self, fmt, *args):  # 静音默认访问日志
        logger.debug("[http] " + fmt, *args)


def start_http_server(host: str, port: int, pipeline: Pipeline) -> ThreadingHTTPServer:
    handler = type("BoundHealthHandler", (HealthHandler,), {"pipeline": pipeline})
    httpd = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    logger.info("[HTTP] 健康检查: http://%s:%d/health", host, port)
    return httpd


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="ASR -> Agent -> TTS 语音服务")
    p.add_argument("--wake", default=None, help="唤醒词, 多个用逗号分隔")
    p.add_argument("--model", default=None, help="识别模型目录")
    p.add_argument("--device", default=None, help="麦克风设备索引")
    p.add_argument("--duration", type=float, default=None, help="单次录音最长秒数")
    p.add_argument("--silence", type=float, default=None, help="静音结束阈值(秒)")
    p.add_argument("--vad-threshold", type=float, default=None, help="VAD 阈值(dB)")
    p.add_argument("--replay", default=None, help="用 wav 文件回放测试, 不占麦克风")
    p.add_argument("--replay-speed", type=float, default=1.0,
                   help="回放速度倍率: 1.0 = 真实时间, 2.0 = 两倍速 "
                        "(默认 1.0, 连续对话验证需要真实节奏)")
    p.add_argument("--no-wake", dest="skip_wake", action="store_true",
                   help="跳过唤醒词判定直接识别 (仅链路自测用)")
    p.add_argument("--no-save", dest="save", action="store_false", help="不保存 WAV")
    p.add_argument("--quiet", action="store_true", help="不显示电平条")
    p.add_argument("--no-http", dest="http", action="store_false", help="不启动健康检查 HTTP 服务")
    p.add_argument("--keep-alive", type=float, default=0.0,
                   help="回放结束后继续保留 HTTP 服务多少秒 (便于外部读取 /state)")
    p.add_argument("--continuous-timeout", type=float, default=None,
                   help="连续对话窗口秒数: 一轮结束后多久内说话无需唤醒词, "
                        "0 表示关闭 (默认取 configs/pipeline.yaml)")
    p.add_argument("--echo-settle", type=float, default=None,
                   help="回声沉降期秒数: TTS 播完后等多久才接受用户语音 "
                        "(要覆盖声波传播+混响+音频缓冲, 默认取 configs/pipeline.yaml)")
    p.add_argument("--echo-quiet-timeout", type=float, default=None,
                   help="等安静的最长等待秒数: 沉降期后最多再等这么久确认"
                        "房间安静, 超时强制恢复 (默认取 configs/pipeline.yaml)")
    p.add_argument("--host", default=None, help="健康检查监听地址")
    p.add_argument("--port", type=int, default=None, help="健康检查监听端口")
    return p.parse_args(argv)


def build_wakeup_args(args, cfg):
    """把配置值与命令行合成 wakeup_asr 需要的命名空间。"""
    # 唤醒词: 命令行 > 配置(字符串或列表) > 内置默认值
    if args.wake:
        wake = args.wake
    else:
        words = cfg.asr.wake_word_list
        wake = ",".join(words) if words else "你好助手"

    ns = argparse.Namespace(
        wake=wake,
        model=args.model or cfg.asr.model_path or None,
        device=args.device or (cfg.asr.device or None),
        duration=args.duration if args.duration is not None else cfg.asr.max_duration,
        silence=(
            args.silence if args.silence is not None else cfg.asr.silence_timeout
        ),
        vad_threshold=(
            args.vad_threshold
            if args.vad_threshold is not None
            else cfg.asr.vad_threshold
        ),
        replay=args.replay,
        replay_speed=args.replay_speed,
        skip_wake=args.skip_wake,
        save=False,   # 永不落盘
        quiet=args.quiet,
        # 连续对话窗口(秒)
        continuous_timeout=(
            args.continuous_timeout
            if args.continuous_timeout is not None
            else cfg.asr.continuous_timeout
        ),
        # 回声沉降期(秒): 播报结束后等多久才允许接受用户语音
        echo_settle=(
            args.echo_settle
            if args.echo_settle is not None
            else cfg.asr.echo_settle
        ),
        # 等安静的最长等待(秒)
        echo_quiet_timeout=(
            args.echo_quiet_timeout
            if args.echo_quiet_timeout is not None
            else cfg.asr.echo_quiet_timeout
        ),
    )
    return ns


def main(argv=None):
    cfg = load_config()
    args = parse_args(argv)
    wakeup_args = build_wakeup_args(args, cfg)

    pipeline = Pipeline(cfg)

    httpd = None
    if args.http:
        host = args.host or cfg.server.host
        port = args.port or cfg.server.port
        try:
            httpd = start_http_server(host, port, pipeline)
        except OSError as exc:
            # 端口被占用意味着外部读到的 /state 会是别的进程的, 必须显式报错
            logger.error("[HTTP] 端口 %s 启动失败: %s", port, exc)
            raise SystemExit(
                f"健康检查端口 {port} 被占用, 请先停止占用进程再重试"
            )

    app = VoiceASR(wakeup_args, pipeline)
    # 一轮彻底结束后开启连续对话窗口 (播报完成才计时, 不占用用户的窗口)
    pipeline.on_turn_complete = app.open_session
    # 播报**之前**架回声守卫 (挡住自己的声音被麦克风收进去)
    pipeline.on_before_speak = app.arm_echo_guard
    # 播报**之后**把守卫延伸到沉降期 (余音还没散)
    pipeline.on_after_speak = app.finalize_echo_guard

    if app.continuous_timeout:
        print(
            f"[*] 连续对话已启用: 一轮结束后 {app.continuous_timeout:.0f}s 内"
            f"再次说话无需唤醒词"
        )
    else:
        print("[*] 连续对话未启用: 每轮都需要唤醒词")

    try:
        if wakeup_args.replay:
            app.run_replay(wakeup_args.replay)
            # 回放结束后等这一轮 (Agent + TTS + 播放) 跑完再退出,
            # 否则 HTTP 服务会随进程一起消失, 外部看不到最终状态。
            if pipeline.turn_count:
                print("[*] 等待当前这一轮处理完成 ...")
                pipeline.wait_idle(timeout=300, turn=pipeline.turn_count)
                time.sleep(1)
            if args.keep_alive > 0:
                print(f"[*] 保持 HTTP 服务 {args.keep_alive:.0f}s ...")
                time.sleep(args.keep_alive)
        else:
            app.run_live()
    except KeyboardInterrupt:
        print()
    finally:
        pipeline.close()
        if httpd is not None:
            httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
