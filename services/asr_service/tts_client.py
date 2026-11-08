"""ChatTTS WebSocket 客户端 + 本地播放。

协议 (与 services/tts_service/examples/ws_server/protocol.py 一致):
- 客户端发 JSON 文本帧: {"type":"tts","text":"...","format":"pcm","stream":true}
- 服务端推二进制帧: 小端定长头 + payload
    magic(4s)=b"TTS1" type(1B) flags(1B) seq(4u) sample_rate(4u)
    n_samples(4u) req_id_len(2u) req_id(req_id_len bytes) payload
  type: 1=AUDIO_DATA, 2=AUDIO_END
  flags: 0x01=MP3, 0x02=WAV, 0x00=PCM16LE

收到的 PCM 边收边播, 不写任何文件。
"""

from __future__ import annotations

import json
import logging
import struct
import time
from dataclasses import dataclass
from typing import Optional

from config import TTSConfig

logger = logging.getLogger("tts_client")

MAGIC = b"TTS1"
FRAME_TYPE_AUDIO_DATA = 1
FRAME_TYPE_AUDIO_END = 2
HEADER_STRUCT = struct.Struct("<4sBBIIHH")
HEADER_SIZE = HEADER_STRUCT.size


class TTSError(RuntimeError):
    """TTS 合成或播放失败。"""


@dataclass
class TTSResult:
    """一次合成的统计信息。"""

    duration_ms: int = 0
    audio_bytes: int = 0
    frames: int = 0
    played: bool = False
    #: 分阶段耗时 (毫秒), 用来定位慢在哪一步
    connect_ms: int = 0      # 建立 WebSocket 连接
    first_frame_ms: int = 0  # 发出请求 -> 收到第一个音频帧
    synth_ms: int = 0        # 第一个音频帧 -> 收到 AUDIO_END (纯合成)
    play_ms: int = 0         # 播放/flush 收尾
    audio_ms: int = 0        # 音频本身的时长 (字节数推算)
    #: 服务端自报的分段耗时 (毫秒)
    server_total_ms: int = 0
    server_first_frame_ms: int = 0
    server_queued_ms: int = 0
    #: 是否已收到服务端的 done 事件
    done_received: bool = False


def unpack_frame(data: bytes):
    """解析二进制帧, 返回 (type, flags, seq, sample_rate, request_id, payload)。"""
    if len(data) < HEADER_SIZE:
        raise TTSError("帧太短")
    magic, ftype, flags, seq, sample_rate, n_samples, req_id_len = HEADER_STRUCT.unpack(
        data[:HEADER_SIZE]
    )
    if magic != MAGIC:
        raise TTSError("帧 magic 不匹配")
    body = data[HEADER_SIZE:]
    if len(body) < req_id_len:
        raise TTSError("帧被截断")
    request_id = body[:req_id_len].decode("utf-8", errors="replace")
    payload = body[req_id_len:]
    return ftype, flags, seq, sample_rate, request_id, payload


class TTSClient:
    """把文本发给 ChatTTS, 收 PCM 并直接播放。"""

    def __init__(self, cfg: TTSConfig) -> None:
        self.cfg = cfg
        self._speaker = None
        self._speaker_failed = False

    # ------------------------------------------------------------ 播放器

    def _open_speaker(self):
        """打开一个输出流。

        每次合成单独开/关, 避免复用已 stop 的流导致下一轮写入失败。
        没有可用输出设备时返回 None (只消费不播放), 并记住失败状态不再重试。
        """
        if self._speaker_failed:
            return None
        try:
            import sounddevice as sd

            stream = sd.RawOutputStream(
                samplerate=self.cfg.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=0,
            )
            stream.start()
            self._speaker = stream
            logger.info("[TTS] 扬声器已打开 (%dHz)", self.cfg.sample_rate)
            return stream
        except Exception as exc:
            self._speaker_failed = True
            logger.warning("[TTS] 无可用输出设备, 只合成不播放: %s", exc)
            return None

    def _close_speaker(self) -> None:
        if self._speaker is not None:
            try:
                self._speaker.stop()
                self._speaker.close()
            except Exception:
                pass
            self._speaker = None

    # ------------------------------------------------------------ 合成

    def speak(self, text: str, request_id: Optional[str] = None) -> TTSResult:
        """合成文本并直接播放。返回统计信息。"""
        if not text or not text.strip():
            raise TTSError("待合成文本为空")

        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            raise TTSError(f"缺少 websockets 依赖: {exc}") from exc

        request_id = request_id or f"r{int(time.time() * 1000)}"
        result = TTSResult()
        started = time.time()

        request = {
            "type": "tts",
            "request_id": request_id,
            "text": text,
            "voice": self.cfg.voice,
            "speed": self.cfg.speed,
            "format": "pcm",
            "stream": True,
            "play": False,
        }

        t_open = time.time()
        speaker = self._open_speaker()
        speaker_ms = int((time.time() - t_open) * 1000)

        t_conn = time.time()
        try:
            with connect(self.cfg.ws_url, max_size=None, open_timeout=10) as ws:
                result.connect_ms = int((time.time() - t_conn) * 1000)

                t_send = time.time()
                ws.send(json.dumps(request, ensure_ascii=False))
                send_ms = int((time.time() - t_send) * 1000)

                first_frame_at = None
                t_first = time.time()
                got_end = False
                while True:
                    message = ws.recv(timeout=self.cfg.timeout)
                    if isinstance(message, str):
                        self._handle_text_event(message, result)
                        # 服务端在 AUDIO_END 之后还会发一条 done 事件,
                        # 里面带着服务端的分段耗时。等它到了再收尾。
                        if result.done_received:
                            break
                        continue
                    ftype, flags, seq, sample_rate, req_id, payload = unpack_frame(message)
                    if ftype == FRAME_TYPE_AUDIO_END:
                        got_end = True
                        continue
                    if ftype != FRAME_TYPE_AUDIO_DATA:
                        continue
                    if first_frame_at is None:
                        first_frame_at = time.time()
                        result.first_frame_ms = int((first_frame_at - t_first) * 1000)
                        result.connect_ms += send_ms
                    result.frames += 1
                    result.audio_bytes += len(payload)
                    if speaker is not None and payload:
                        # 边收边播, 不落盘
                        speaker.write(payload)

                t_end = time.time()
                if first_frame_at is not None:
                    result.synth_ms = int((t_end - first_frame_at) * 1000)
                if got_end and not result.done_received:
                    # 服务端没发 done (异常路径), 也不能一直等下去
                    logger.warning("[TTS] 未收到服务端的 done 事件")
        except TTSError:
            self._close_speaker()
            raise
        except Exception as exc:
            self._close_speaker()
            raise TTSError(f"TTS 合成失败: {exc}") from exc
        finally:
            # stop() 会 flush 缓冲, 等这一轮播完; 随后关闭流
            t_close = time.time()
            self._close_speaker()
            result.play_ms = int((time.time() - t_close) * 1000)

        result.duration_ms = int((time.time() - started) * 1000)
        result.played = speaker is not None
        # 音频本身的时长: PCM16 单声道 -> 字节数 / 2 / 采样率
        if self.cfg.sample_rate:
            result.audio_ms = int(
                result.audio_bytes / 2 / self.cfg.sample_rate * 1000
            )
        logger.info(
            "[TTS] 总计 %dms | 连接 %dms | 首帧 %dms | 合成 %dms | 播放收尾 %dms"
            " | 音频 %dms | %d 帧 %d 字节 played=%s",
            result.duration_ms,
            result.connect_ms,
            result.first_frame_ms,
            result.synth_ms,
            result.play_ms,
            result.audio_ms,
            result.frames,
            result.audio_bytes,
            result.played,
        )
        if result.server_total_ms:
            logger.info(
                "[TTS] 服务端自报: 总计 %dms | 首帧 %dms | 排队 %dms",
                result.server_total_ms,
                result.server_first_frame_ms,
                result.server_queued_ms,
            )
        logger.info(
            "[TTS] 打开扬声器耗时 %dms", speaker_ms,
        )
        return result

    def _handle_text_event(self, raw: str, result: TTSResult) -> None:
        """处理服务端的 JSON 事件。"""
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return
        kind = obj.get("type")
        if kind == "error":
            raise TTSError(f"TTS 服务报错: {obj.get('message')}")
        if kind == "done":
            result.done_received = True
            result.duration_ms = int(obj.get("duration_ms", result.duration_ms))
            # 服务端自报的分段耗时, 与客户端侧对照能看出是排队还是推理慢
            result.server_total_ms = int(obj.get("duration_ms", 0))
            result.server_first_frame_ms = int(obj.get("first_frame_ms", -1))
            result.server_queued_ms = int(obj.get("queued_ms", 0))

    def close(self) -> None:
        self._close_speaker()
