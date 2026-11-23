"""麦克风输入的高通滤波。

背景
----
语音识别的特征提取 (MFCC) 覆盖 40~7800Hz, 但**人声的基频和主要能量**
在 200Hz 以上。而麦克风采到的环境噪声里有一大块是**低频**:

- 空调 / 风扇的隆隆声 (几十到一两百 Hz)
- 桌面 / 机箱的振动
- 电源哼声 (50Hz 及其谐波)
- 移动时的手部抖动

这些低频噪声有两个害处:

1. **抬高 RMS** —— 能量全在低频, 但 RMS 把所有频段算在一起, 于是
   「静���时的电平」被拉高, VAD 门限跟着抬高, 说话声更容易被判成噪声。
2. **挤占动态范围** —— 低频占了大部分幅度, 人声的高频细节 (辅音、
   齿音) 相对就小了, 信噪比变差。

一个 4 阶 Butterworth 高通 (80Hz) 就能把 50Hz 压掉 16dB, 30Hz 压掉
34dB, 同时 200Hz 以上几乎不损失 (<0.01dB) —— 人声内容基本无损。

为什么放在这里
--------------
**在音频进入队列之前**滤波。这样 VAD 的电平判断、唤醒词识别、正式识别
看到的都是**同一份**滤波后的信号 —— 如果只在一处滤波, 会出现「门限按
未滤波的电平算, 识别按滤波后的信号做」的不一致, 反而更难调。

实现
----
用 ``scipy.signal`` 的二阶节 (SOS) 形式:

- **SOS 而非直接型**: 高阶 IIR 用直接型实现会有严重的数值稳定性问题
  (高 Q 值的 biquad 在 float64 下也可能溢出)。SOS 形式把每个二阶节
  独立计算, 数值上稳定得多。
- **零初始状态**: 滤波器从「静止」开始, 起始有一小段建立过程
  (几毫秒), 对连续流可以忽略。
- **跨块保持状态**: 每一块 (256ms) 的滤波继承上一块的 ``zi``,
  否则块边界会有不连续, 产生咔哒声。

没有 scipy 时**降级为直通** (只记一次警告) —— 不能因为缺个滤波依赖
就让整个 ASR 服务起不来。
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger("asr.audio_filter")


class HighPassFilter:
    """流式高通滤波器 (int16 PCM -> int16 PCM)。

    Args:
        cutoff_hz: 截止频率。低于这个频率的成分被衰减。
        sample_rate: 采样率 (Hz)。
        order: 滤波器阶数 (2 的倍数, 每个二阶节算 2 阶)。默认 4。
        channels: 声道数 (当前链路是单声道)。
    """

    def __init__(
        self,
        cutoff_hz: float,
        sample_rate: int,
        order: int = 4,
        channels: int = 1,
    ) -> None:
        self.cutoff_hz = float(cutoff_hz)
        self.sample_rate = int(sample_rate)
        self.order = int(order)
        self.channels = int(channels)

        self._sos: Optional[np.ndarray] = None
        self._zi: Optional[np.ndarray] = None
        self._enabled = False

        if self.cutoff_hz <= 0:
            # 0 或负数 = 关闭滤波
            return

        # 截止频率必须低于奈奎斯特频率, 否则滤波器无意义
        nyquist = self.sample_rate / 2.0
        if self.cutoff_hz >= nyquist * 0.95:
            logger.warning(
                "[滤波] 截止频率 %.0fHz 过高 (奈奎斯特 %.0fHz), 关闭高通",
                self.cutoff_hz,
                nyquist,
            )
            return

        try:
            from scipy import signal
        except ImportError:
            logger.warning("[滤波] 没装 scipy, 关闭高通滤波 (音频原样处理)")
            return

        try:
            self._sos = signal.butter(
                self.order,
                self.cutoff_hz,
                btype="highpass",
                fs=self.sample_rate,
                output="sos",
            )
        except Exception as exc:
            logger.warning("[滤波] 构造高通滤波器失败, 关闭: %s", exc)
            return

        # 滤波状态 (zi) 的形状: sosfilt 在 axis=0、输入 (帧数, 声道) 时,
        # 要求 zi 的形状是 (节数, 2, 声道数)。
        self._zi = np.zeros((self._sos.shape[0], 2, self.channels))
        self._enabled = True
        logger.info(
            "[滤波] 高通已启用: %.0fHz, %d 阶, %dHz, %d 声道",
            self.cutoff_hz,
            self.order,
            self.sample_rate,
            self.channels,
        )

    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """滤波是否真的在工作 (没装 scipy / 截止频率非法时会是 False)。"""
        return self._enabled

    @property
    def description(self) -> str:
        """人类可读的状态描述, 用来打日志。"""
        if not self._enabled:
            return "off"
        return f"highpass {self.cutoff_hz:.0f}Hz order={self.order}"

    def reset(self) -> None:
        """清空滤波器状态 (回放开始 / 重新开始录音时用)。"""
        if self._zi is not None and self._sos is not None:
            self._zi = np.zeros((self._sos.shape[0], 2, self.channels))

    def process(self, data: bytes) -> bytes:
        """滤波一块 PCM 数据。

        Args:
            data: 小端 int16 PCM 字节 (交错多声道)。

        Returns:
            滤波后的同格式字节。滤波不可用时**原样返回**。
        """
        if not self._enabled or not data:
            return data

        try:
            from scipy import signal

            samples = np.frombuffer(data, dtype="<i2")
            if samples.size == 0:
                return data

            # 拆成 (帧数, 声道)
            frames = samples.size // self.channels
            if frames == 0:
                return data
            shaped = samples[: frames * self.channels].reshape(frames, self.channels)

            # 转 float64 —— int16 直接进 IIR 会有量化噪声累积
            sig = shaped.astype(np.float64)

            filtered, self._zi = signal.sosfilt(self._sos, sig, axis=0, zi=self._zi)

            # 限幅回 int16。高通理论上不会溢出, 但浮点误差可能越界一点点。
            np.clip(filtered, -32768, 32767, out=filtered)
            out = filtered.astype("<i2").reshape(-1)
            return out.tobytes()
        except Exception as exc:
            # 滤波失败不能拖垮识别 —— 关掉滤波, 原样放行
            logger.warning("[滤波] 处理失败, 本次之后关闭滤波: %s", exc)
            self._enabled = False
            return data


def build_filter(
    cutoff_hz: float,
    sample_rate: int,
    order: int = 4,
    channels: int = 1,
) -> HighPassFilter:
    """构造滤波器。``cutoff_hz <= 0`` 时返回一个「直通」的实例。"""
    return HighPassFilter(cutoff_hz, sample_rate, order=order, channels=channels)


def measure_response(
    cutoff_hz: float,
    sample_rate: int,
    order: int = 4,
    freqs: Optional[Tuple[float, ...]] = None,
) -> Tuple[Tuple[float, float], ...]:
    """算一下滤波器的频响, 方便调参时看效果。

    Returns:
        ((频率Hz, 增益dB), ...) 的元组。
    """
    if cutoff_hz <= 0:
        return ()
    try:
        from scipy import signal
    except ImportError:
        return ()

    sos = signal.butter(
        order, cutoff_hz, btype="highpass", fs=sample_rate, output="sos"
    )
    if freqs is None:
        freqs = (0, 30, 50, 80, 100, 150, 200, 400, 1000, 4000, 7800)

    out = []
    for f in freqs:
        w, h = signal.sosfreqz(sos, worN=[2 * np.pi * f / sample_rate])
        gain = 20 * np.log10(abs(h[0]) + 1e-12)
        out.append((float(f), float(gain)))
    return tuple(out)
