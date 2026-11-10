"""arm_service 侧的 TTS 播报。

安全门阀拒绝动作时, 必须让用户**听到**原因 —— 静默失败会让人以为
机器人没听见, 而���是出了问题。

这里直接连 ChatTTS 的 WebSocket, 收 PCM 交给 sounddevice 直接播放,
不写任何文件 —— 与 asr_service 的 TTSClient 同一套做法。

**为什么不复用 asr_service 的 TTSClient**: 那个类依赖 asr_service 的
config.TTSConfig, 会把两个服务耦合在一起 (还要跨目录 import)。
这里保持 arm_service 自包含, 只依赖 websockets + sounddevice (都可选)。
"""

from __future__ import annotations

import json
import logging
import struct
import time
from typing import Dict, Optional

logger = logging.getLogger("arm.tts")

MAGIC = b"TTS1"
FRAME_TYPE_AUDIO_DATA = 1
FRAME_TYPE_AUDIO_END = 2
HEADER_STRUCT = struct.Struct("<4sBBIIHH")
HEADER_SIZE = HEADER_STRUCT.size


class TTSError(RuntimeError):
    """TTS 合成或播放失败。"""


def _unpack_frame(data: bytes):
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
    return ftype, flags, seq, sample_rate, request_id, body[req_id_len:]


class ArmNotifier:
    """把提示文案用 TTS 念出来。

    全部失败都只记录日志 —— 播报是「锦上添花」, 不该因为扬声器没插好
    就让整个安全门阀失败 (门阀该拒还是拒)。

    **两种播报方式**:

    - ``server_play=True`` (默认): 在请求里带 ``play: true``, 让 TTS 服务端
      自己播。arm_service 所在的 conda 环境没有 sounddevice, 走这条路才
      能真的出声 —— 而且播报与 ASR 侧的播报走同一个 TTS 服务, 音色一致。
    - ``server_play=False``: 自己收 PCM 播。适合 arm_service 与 TTS 服务
      跑在同一台有声卡的机器、且本地有 sounddevice 的情况。
    """

    def __init__(
        self,
        ws_url: str = "ws://127.0.0.1:8000/ws",
        voice: str = "Default",
        speed: int = 5,
        sample_rate: int = 24000,
        timeout: float = 30.0,
        enabled: bool = True,
        server_play: bool = True,
    ) -> None:
        self.ws_url = ws_url
        self.voice = voice
        self.speed = speed
        self.sample_rate = sample_rate
        self.timeout = timeout
        self.enabled = enabled
        self.server_play = server_play
        self._speaker_failed = False

    # ------------------------------------------------------------ 播放器

    def _open_speaker(self):
        if self._speaker_failed:
            return None
        try:
            import sounddevice as sd

            stream = sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=0,
            )
            stream.start()
            return stream
        except Exception as exc:
            self._speaker_failed = True
            logger.warning("[播报] 无可用输出设备, 只合成不播放: %s", exc)
            return None

    # ------------------------------------------------------------ 播报

    def speak(self, text: str) -> bool:
        """播报一段文本。返回是否真的出声了。"""
        if not self.enabled:
            logger.info("[播报] 已关闭, 跳过: %r", text)
            return False
        if not text or not text.strip():
            return False

        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            logger.warning("[播报] 缺少 websockets 依赖: %s", exc)
            return False

        request_id = f"arm{int(time.time() * 1000)}"
        # 只发**显式配置过**的字段 —— 没配的让 TTS 服务端用自己的默认值
        # (configs/tts.yaml)。这样「换默认音色」只改服务端配置一处。
        request: Dict[str, object] = {
            "type": "tts",
            "request_id": request_id,
            "text": text,
            "format": "pcm",
            "stream": True,
            # 服务端播报: TTS 服务自己开扬声器放音, arm_service 这边只收帧不播
            "play": bool(self.server_play),
        }
        if self.voice:
            request["voice"] = self.voice
        if self.speed is not None:
            request["speed"] = self.speed

        # 服务端播报时本地不用开扬声器 (避免两个进程抢声卡)
        speaker = None if self.server_play else self._open_speaker()
        played = False
        try:
            with connect(self.ws_url, max_size=None, open_timeout=10) as ws:
                ws.send(json.dumps(request, ensure_ascii=False))
                while True:
                    message = ws.recv(timeout=self.timeout)
                    if isinstance(message, str):
                        try:
                            obj = json.loads(message)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("type") == "error":
                            logger.warning(
                                "[播报] TTS 服务报错: %s", obj.get("message")
                            )
                            break
                        if obj.get("type") == "done":
                            break
                        continue

                    ftype, flags, seq, sample_rate, req_id, payload = _unpack_frame(message)
                    if ftype == FRAME_TYPE_AUDIO_END:
                        continue
                    if ftype != FRAME_TYPE_AUDIO_DATA:
                        continue
                    if speaker is not None and payload:
                        speaker.write(payload)
                        played = True
        except Exception as exc:
            logger.warning("[播报] 失败: %s", exc)
            return False
        finally:
            if speaker is not None:
                try:
                    speaker.stop()
                    speaker.close()
                except Exception:
                    pass

        # 服务端播报时, 无法从客户端侧确认是否出声 —— 以「请求送达」为准
        if self.server_play:
            played = True
        logger.info("[播报] %r (played=%s, server_play=%s)", text, played, self.server_play)
        return played
