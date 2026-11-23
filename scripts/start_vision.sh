#!/usr/bin/env bash
# 启动视觉服务 (conda 环境 yolo)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="$ROOT/services/vision_service"
CONDA_ENV="${VISION_CONDA_ENV:-yolo}"
LOG_DIR="$ROOT/logs"

export VISION_SERVER_HOST="${VISION_SERVER_HOST:-0.0.0.0}"
export VISION_SERVER_PORT="${VISION_SERVER_PORT:-8803}"

# ---- Orbbec SDK 的 libusb 后端 ----
#
# **pip 装的 pyorbbecsdk 自带的 libOrbbecSDK.so 是没有 USB PAL 的构建** ——
# 用它枚举设备会得到 0 个, 日志里报:
#   "Usb pal is not exist, please check the build config that you have
#    enabled BUILD_USB_PAL"
#
# 必须用源码编译的那份 (OB_BUILD_USB_PAL=ON)。查找逻辑放在
# services/vision_service/paths.py 里 (跟服务本身用的是同一套),
# 这里只负责把找到的目录塞进 LD_LIBRARY_PATH。
#
# 优先级:
#   1. $ORBBEC_LIB (显式指定)
#   2. $VISION_SDK_LIB_DIR / $VISION_SDK_DIR (环境变量)
#   3. 仓库内 sdk/orbbec/lib
#   4. 仓库外的常见位置
#
# 详见 sdk/README.md 和 docs/vision-service.md。
ORBBEC_LIB="${ORBBEC_LIB:-}"

if [ -z "$ORBBEC_LIB" ]; then
    for cand in \
        "${VISION_SDK_LIB_DIR:-}" \
        "$ROOT/sdk/orbbec/lib" \
        "$ROOT/../OrbbecSDK_v2/build/linux_x86_64/lib" \
        "$ROOT/../../OrbbecSDK_v2/build/linux_x86_64/lib" \
        "$HOME/workspace/project/OrbbecSDK_v2/build/linux_x86_64/lib" \
        "/opt/OrbbecSDK_v2/build/linux_x86_64/lib"
    do
        [ -z "$cand" ] && continue
        if [ -f "$cand/libOrbbecSDK.so.2" ]; then
            ORBBEC_LIB="$cand"
            break
        fi
    done
fi

if [ -f "$ORBBEC_LIB/libOrbbecSDK.so.2" ]; then
    export LD_LIBRARY_PATH="$ORBBEC_LIB:${LD_LIBRARY_PATH:-}"
    export VISION_SDK_LIB_DIR="$ORBBEC_LIB"
    echo "[vision] Orbbec SDK: $ORBBEC_LIB"
elif [ -z "$ORBBEC_LIB" ]; then
    echo "[vision] 警告: 未找到 libOrbbecSDK.so.2 —— pip 装的 pyorbbecsdk2 没有 USB PAL,"
    echo "         相机枚举会得到 0 个设备。"
    echo "         编译 OrbbecSDK (OB_BUILD_USB_PAL=ON) 并把它链到 sdk/orbbec/lib,"
    echo "         或设 VISION_SDK_LIB_DIR —— 详见 sdk/README.md"
fi

# ---- 模型权重 / 数据集的查找目录 ----
#
# services/vision_service/paths.py 会在这些目录里找配置里写的文件名,
# 所以大文件放仓库外也没问题 (仓库里只留目录结构)。
export VISION_MODEL_DIR="${VISION_MODEL_DIR:-$ROOT/models}"
export VISION_DATA_DIR="${VISION_DATA_DIR:-$ROOT/datasets}"

mkdir -p "$LOG_DIR"
cd "$SERVICE_DIR"

echo "[vision] 启动视觉服务 (env=$CONDA_ENV, port=$VISION_SERVER_PORT) ..."
echo "[vision] 模型目录: $VISION_MODEL_DIR"
echo "[vision] 数据目录: $VISION_DATA_DIR"
conda run --no-capture-output -n "$CONDA_ENV" \
    python main.py 2>&1 | tee -a "$LOG_DIR/vision.log"
