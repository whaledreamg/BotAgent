#!/usr/bin/env bash
# 一键构建 BotAgent 监控 APP (qmake)
#
#   ./build.sh          # 构建
#   ./build.sh --clean  # 先清理再构建
#
# 产物: monitor/build/botagent_monitor
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$HERE/build"

# Qt 6.8.3 安装位置 (可用 QT_PREFIX 环境变量覆盖)
QT_PREFIX="${QT_PREFIX:-/home/gxh/Qt/6.8.3/gcc_64}"

if [ ! -d "$QT_PREFIX/lib/cmake/Qt6" ]; then
    echo "[error] 找不到 Qt6: $QT_PREFIX" >&2
    echo "        请设置 QT_PREFIX 环境变量指向 Qt 安装目录" >&2
    exit 1
fi

# qmake 路径 (Qt 6 里叫 qmake6)
QMAKE="$QT_PREFIX/bin/qmake6"
if [ ! -x "$QMAKE" ]; then
    QMAKE="$QT_PREFIX/bin/qmake"
fi
if [ ! -x "$QMAKE" ]; then
    echo "[error] 找不到 qmake: $QT_PREFIX/bin/qmake6" >&2
    exit 1
fi

if [ "${1:-}" = "--clean" ]; then
    echo "=== 清理 ==="
    rm -rf "$BUILD_DIR"
fi

mkdir -p "$BUILD_DIR"

echo "=== qmake (Qt: $QT_PREFIX) ==="
# qmake 的工作目录决定 Makefile 的落点, 必须在 build/ 里跑
( cd "$BUILD_DIR" && "$QMAKE" "$HERE/monitor.pro" )

echo "=== make ==="
make -C "$BUILD_DIR" -j"$(nproc)"

# 自测程序 (独立的 qmake 工程)
echo "=== make selftest ==="
mkdir -p "$BUILD_DIR/selftest"
( cd "$BUILD_DIR/selftest" && "$QMAKE" "$HERE/selftest.pro" )
make -C "$BUILD_DIR/selftest" -j"$(nproc)"

echo
echo "构建完成: $BUILD_DIR/botagent_monitor"
echo "运行:     $BUILD_DIR/botagent_monitor"
echo "自测:     $BUILD_DIR/selftest/botagent_monitor_selftest"
