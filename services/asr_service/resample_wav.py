#!/usr/bin/env python3
"""
WAV 采样率转换工具。

默认后端是 ffmpeg（跨平台、质量高、不依赖 Python 音频库），
若系统没有 ffmpeg，会自动回退到 scipy + soundfile 的纯 Python 实现。

用法示例：
    # 单个文件，转 16k 单声道（ASRT 默认需求）
    python resample_wav.py input.wav

    # 指定输出
    python resample_wav.py input.wav -o out.wav

    # 保留立体声，转 44100
    python resample_wav.py input.wav -r 44100 -c 2

    # 批量转换目录（非递归）
    python resample_wav.py ./wavs -d ./wavs_16k

    # 递归子目录并保持目录结构
    python resample_wav.py ./wavs -d ./wavs_16k --recursive

    # 强制用 Python 后端
    python resample_wav.py input.wav --backend python
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

WAV_EXTS = {".wav", ".wave"}


# ---------------- ffmpeg 后端 ----------------
def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def resample_ffmpeg(src: Path, dst: Path, sr: int, channels: int) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-acodec", "pcm_s16le",   # ASRT / ASR 通用格式
        "-ar", str(sr),
        "-ac", str(channels),
        str(dst),
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except subprocess.CalledProcessError as e:
        tail = e.stderr.decode(errors="ignore").strip().splitlines()[-5:]
        print(f"\n❌ ffmpeg 转换失败: {src}\n" + "\n".join(tail))
        return False


# ---------------- Python 后端 ----------------
def resample_python(src: Path, dst: Path, sr: int, channels: int) -> bool:
    try:
        import numpy as np
        import soundfile as sf
        from scipy.signal import resample_poly
    except ImportError as e:
        print(f"\n❌ 缺少依赖: {e}。请 pip install numpy scipy soundfile")
        return False

    try:
        data, orig_sr = sf.read(str(src), always_2d=True)  # shape: (N, C)
    except Exception as e:
        print(f"\n❌ 读取失败: {src}: {e}")
        return False

    # 声道数处理
    if channels == 1 and data.shape[1] > 1:
        data = data.mean(axis=1, keepdims=True)
    elif channels > 1 and data.shape[1] == 1:
        data = np.repeat(data, channels, axis=1)
    elif data.shape[1] != channels:
        data = data[:, :channels]

    # 重采样（有理数倍，质量比 scipy.signal.resample 好很多）
    if sr != orig_sr:
        from math import gcd
        g = gcd(int(sr), int(orig_sr))
        up, down = sr // g, orig_sr // g
        data = resample_poly(data, up, down, axis=0)

    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), data, sr, subtype="PCM_16")
    return True


# ---------------- 通用 ----------------
def convert_one(src: Path, dst: Path, sr: int, channels: int, backend: str) -> bool:
    if backend == "ffmpeg":
        return resample_ffmpeg(src, dst, sr, channels)
    return resample_python(src, dst, sr, channels)


def collect_inputs(path: Path, recursive: bool) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        it = path.rglob("*") if recursive else path.iterdir()
        return sorted(p for p in it if p.is_file() and p.suffix.lower() in WAV_EXTS)
    return []


def main() -> None:
    ap = argparse.ArgumentParser(
        description="WAV 采样率转换（默认输出 16kHz / 单声道 / 16-bit PCM）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("input", help="输入 WAV 文件或目录")
    ap.add_argument("-o", "--output", help="输出文件（单文件模式）")
    ap.add_argument("-d", "--outdir", default="./resampled", help="输出目录（目录模式）")
    ap.add_argument("-r", "--rate", type=int, default=16000, help="目标采样率 Hz")
    ap.add_argument("-c", "--channels", type=int, default=1, help="目标声道数")
    ap.add_argument("--recursive", action="store_true", help="目录模式递归子目录")
    ap.add_argument(
        "--backend", choices=["auto", "ffmpeg", "python"], default="auto",
        help="转换后端；auto 优先 ffmpeg，无则回退 Python",
    )
    args = ap.parse_args()

    # 选择后端
    backend = args.backend
    if backend == "auto":
        backend = "ffmpeg" if have_ffmpeg() else "python"
    if backend == "ffmpeg" and not have_ffmpeg():
        sys.exit("❌ 指定 ffmpeg 后端但系统未安装 ffmpeg")
    print(f"🔧 使用后端: {backend} | 目标: {args.rate} Hz / {args.channels} 声道")

    in_path = Path(args.input)
    files = collect_inputs(in_path, args.recursive)
    if not files:
        sys.exit("没有找到 .wav 文件")

    # 单文件
    if in_path.is_file():
        out = Path(args.output) if args.output else in_path.with_name(
            f"{in_path.stem}_{args.rate}hz{in_path.suffix}"
        )
        ok = convert_one(in_path, out, args.rate, args.channels, backend)
        print(f"{'✅' if ok else '❌'} {in_path} -> {out}")
        return

    # 目录
    outdir = Path(args.outdir)
    total, success = len(files), 0
    print(f"开始转换 {total} 个文件 -> {outdir}")
    for i, src in enumerate(files, 1):
        rel = src.relative_to(in_path) if args.recursive else Path(src.name)
        dst = outdir / rel
        print(f"[{i}/{total}] {src.name} -> {dst}", end=" ... ", flush=True)
        ok = convert_one(src, dst, args.rate, args.channels, backend)
        print("✅" if ok else "❌")
        success += ok

    print(f"\n完成：{success}/{total} 成功")


if __name__ == "__main__":
    main()
