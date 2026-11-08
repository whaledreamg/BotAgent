#!/usr/bin/env python3
"""
将 MP4（或任意音视频文件）转换为 WAV。

用法示例：
    # 单个文件
    python mp4_to_wav.py input.mp4

    # 指定输出名、采样率、声道
    python mp4_to_wav.py input.mp4 -o out.wav -r 16000 -c 1

    # 批量转换整个目录（非递归）
    python mp4_to_wav.py ./videos -d ./wavs

    # 保留原始采样率、立体声
    python mp4_to_wav.py input.mp4 -r 0 -c 2
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".m4v", ".ts", ".wmv"}


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        sys.exit(
            "❌ 未找到 ffmpeg。请先安装：\n"
            "  Ubuntu/Debian: sudo apt install ffmpeg\n"
            "  macOS:         brew install ffmpeg\n"
            "  Windows:       下载后把 bin 目录加入 PATH"
        )


def convert_one(
    src: Path,
    dst: Path,
    sample_rate: int = 16000,
    channels: int = 1,
) -> bool:
    """
    用 ffmpeg 把单个文件转成 WAV。

    :param src:         输入文件
    :param dst:         输出 wav 路径
    :param sample_rate: 采样率，传 0 表示保持原采样率
    :param channels:    声道数（1=单声道，2=立体声）
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",               # 覆盖已存在文件
        "-i", str(src),     # 输入
        "-vn",              # 丢弃视频流
        "-acodec", "pcm_s16le",  # 16-bit PCM
        "-ac", str(channels),
    ]
    if sample_rate > 0:
        cmd += ["-ar", str(sample_rate)]
    cmd += [str(dst)]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        msg = e.stderr.decode(errors="ignore").strip().splitlines()
        tail = "\n".join(msg[-5:]) if msg else "(无输出)"
        print(f"❌ 转换失败: {src}\n{tail}")
        return False


def collect_inputs(path: Path, recursive: bool = False) -> list[Path]:
    """把输入路径展开成待转换的文件列表。"""
    if path.is_file():
        return [path]
    if path.is_dir():
        it = path.rglob("*") if recursive else path.iterdir()
        return sorted(
            p for p in it if p.is_file() and p.suffix.lower() in VIDEO_EXTS
        )
    print(f"⚠️  路径不存在: {path}")
    return []


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MP4/视频文件转 WAV（基于 ffmpeg）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", help="输入文件或目录")
    parser.add_argument("-o", "--output", help="输出文件（单文件模式）")
    parser.add_argument("-d", "--outdir", default=".", help="输出目录（目录模式）")
    parser.add_argument(
        "-r", "--rate", type=int, default=16000,
        help="采样率，0 表示保持原采样率",
    )
    parser.add_argument(
        "-c", "--channels", type=int, default=1,
        help="声道数（1=单声道，2=立体声）",
    )
    parser.add_argument("--recursive", action="store_true", help="目录模式递归子目录")
    args = parser.parse_args()

    check_ffmpeg()

    in_path = Path(args.input)
    files = collect_inputs(in_path, args.recursive)
    if not files:
        sys.exit("没有找到可转换的文件。")

    # ---- 单文件模式 ----
    if in_path.is_file():
        out = Path(args.output) if args.output else in_path.with_suffix(".wav")
        ok = convert_one(in_path, out, args.rate, args.channels)
        print(f"{'✅' if ok else '❌'} {in_path} -> {out}")
        return

    # ---- 目录模式 ----
    outdir = Path(args.outdir)
    total = len(files)
    success = 0
    print(f"开始转换 {total} 个文件 -> {outdir}")
    for i, src in enumerate(files, 1):
        # 保持相对目录结构
        rel = src.relative_to(in_path) if args.recursive else Path(src.name)
        dst = outdir / rel.with_suffix(".wav")
        print(f"[{i}/{total}] {src} -> {dst}", end=" ... ", flush=True)
        ok = convert_one(src, dst, args.rate, args.channels)
        print("✅" if ok else "❌")
        success += ok

    print(f"\n完成：{success}/{total} 成功")


if __name__ == "__main__":
    main()
