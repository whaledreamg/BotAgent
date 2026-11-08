import numpy as np
import sounddevice as sd
import wave


def record_wav(filename="output.wav", fs=16000, channels=1):
    """
    录制语音，按 Ctrl+C 停止并保存为 WAV 文件。

    :param filename: 保存文件名
    :param fs:       采样率（16000 适合语音识别，44100 适合通用）
    :param channels: 声道数（1 = 单声道）
    """
    chunks = []

    def callback(indata, frames, time_info, status):
        if status:
            print(f"[状态] {status}")
        chunks.append(indata.copy())

    # dtype='int16' 直接录成 16-bit PCM，正好是 WAV 常用格式
    stream = sd.InputStream(
        samplerate=fs,
        channels=channels,
        dtype="int16",
        callback=callback,
    )

    print("🎙️  开始录音，按 Ctrl+C 停止...")
    stream.start()
    try:
        while True:
            sd.sleep(200)  # 主线程空转，实际采集在回调线程
    except KeyboardInterrupt:
        print("\n⏹️  停止录音")
    finally:
        stream.stop()
        stream.close()

    if not chunks:
        print("没有录到数据")
        return

    audio = np.concatenate(chunks, axis=0)  # shape: (总采样点数, channels)

    with wave.open(filename, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)        # 2 字节 = 16 bit
        wf.setframerate(fs)
        wf.writeframes(audio.tobytes())

    duration = audio.shape[0] / fs
    print(f"✅ 已保存 {filename}，时长 {duration:.2f} 秒")


if __name__ == "__main__":
    record_wav("output.wav", fs=16000)
