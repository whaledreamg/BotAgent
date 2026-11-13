#!/usr/bin/env bash
# 启动视觉服务 (conda 环境 yolo)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="$ROOT/services/vision_service"
CONDA_ENV="${VISION_CONDA_ENV:-yolo}"
LOG_DIR="$ROOT/logs"

export VISION_SERVER_HOST="${VISION_SERVER_HOST:-0.0.0.0}"
export VISION_SERVER_PORT="${VISION_SERVER_PORT:-8803}"

# Orbbec SDK 的 libusb 后端。
#
# **pip 装的 pyorbbecsdk 是没有 USB 后端的构建** —— 用它枚举设备会得到 0 个,
# 日志里报 "Usb pal is not exist, please check the build config that you have
# enabled BUILD_USB_PAL"。
#
# 所以这里指向源码编译出来的库 (OB_BUILD_USB_PAL=ON), 它带 USB PAL。
# 编译方法见 docs/vision-service.md。
ORBBEC_LIB="${ORBBEC_LIB:-}"
if [ -z "$ORBBEC_LIB" ]; then
    # 依次探测常见位置, 避免写死某台机器的绝对路径
    for cand in \
        "$ROOT/../OrbbecSDK_v2/build/linux_x86_64/lib" \
        "$ROOT/../../OrbbecSDK_v2/build/linux_x86_64/lib" \
        "/home/gxh/workspace/project/OrbbecSDK_v2/build/linux_x86_64/lib" \
        "/opt/OrbbecSDK_v2/build/linux_x86_64/lib"
    do
        if [ -f "$cand/libOrbbecSDK.so.2" ]; then
            ORBBEC_LIB="$cand"
            break
        fi
    done
fi
if [ -f "$ORBBEC_LIB/libOrbbecSDK.so.2" ]; then
    export LD_LIBRARY_PATH="$ORBBEC_LIB:${LD_LIBRARY_PATH:-}"
elif [ -z "$ORBBEC_LIB" ]; then
    echo "[vision] 警告: 未找到 libOrbbecSDK.so.2 —— pip 装的 pyorbbecsdk2 没有 USB PAL,"
    echo "         相机枚举会得到 0 个设备。请编译 OrbbecSDK 并设 ORBBEC_LIB, 见 docs/vision-service.md"
fi

mkdir -p "$LOG_DIR"
cd "$SERVICE_DIR"

echo "[vision] 启动视觉服务 (env=$CONDA_ENV, port=$VISION_SERVER_PORT) ..."
if [ -n "$LD_LIBRARY_PATH" ]; then
    echo "[vision] LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
fi
conda run --no-capture-output -n "$CONDA_ENV" \
    python main.py 2>&1 | tee -a "$LOG_DIR/vision.log"
