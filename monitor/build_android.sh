#!/usr/bin/env bash
# 一键打包 BotAgent 监控 APP 的 Android 版 (APK)
#
#   ./build_android.sh                 # 打 debug APK (arm64-v8a)
#   ./build_android.sh --release       # 打 release APK (需要签名配置)
#   ./build_android.sh --arch armeabi-v7a
#   ./build_android.sh --install       # 打完后装到已连接的设备
#   ./build_android.sh --clean         # 先清理再构建
#
# 产物: monitor/build-android/build/outputs/apk/*/*.apk
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$HERE/build-android"

# ---- 工具链路径 (可用环境变量覆盖) ----
#
# 默认值写死的是原始开发机 (/home/gxh/...)。换机器后如果不存在,
# 就自动探测 —— 否则每次都得手动 export 一堆变量。
QT_PREFIX="${QT_PREFIX:-/home/gxh/Qt/6.8.3}"
if [ ! -d "$QT_PREFIX/android_arm64_v8a" ] && [ ! -d "$QT_PREFIX/gcc_64" ]; then
    for _cand in /home/*/Qt/6.* /opt/Qt/6.* /usr/local/Qt/6.*; do
        if [ -d "$_cand/android_arm64_v8a" ] || [ -d "$_cand/gcc_64" ]; then
            QT_PREFIX="$_cand"
            break
        fi
    done
fi

ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-/home/gxh/Android/Sdk}"
if [ ! -d "$ANDROID_SDK_ROOT" ]; then
    for _cand in /home/*/Android/Sdk /opt/android-sdk /usr/local/android-sdk; do
        if [ -d "$_cand" ]; then
            ANDROID_SDK_ROOT="$_cand"
            break
        fi
    done
fi

ANDROID_NDK_ROOT="${ANDROID_NDK_ROOT:-$ANDROID_SDK_ROOT/ndk/26.1.10909125}"
JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-21-openjdk-amd64}"

# 目标架构 —— 决定用哪套 Qt Android 库。
ARCH="arm64-v8a"
case "$ARCH" in
    arm64-v8a)   QT_ANDROID_DIR="$QT_PREFIX/android_arm64_v8a" ;;
    armeabi-v7a) QT_ANDROID_DIR="$QT_PREFIX/android_armv7" ;;
    x86_64)      QT_ANDROID_DIR="$QT_PREFIX/android_x86_64" ;;
    x86)         QT_ANDROID_DIR="$QT_PREFIX/android_x86" ;;
    *) echo "[error] 不支持的架构: $ARCH" >&2; exit 1 ;;
esac

DEPLOY_QT="$QT_PREFIX/gcc_64/bin/androiddeployqt"
QMAKE="$QT_ANDROID_DIR/bin/qmake6"

# ---- 参数 ----
BUILD_TYPE="debug"
DO_INSTALL=0
DO_CLEAN=0
for arg in "$@"; do
    case "$arg" in
        --release) BUILD_TYPE="release" ;;
        --debug)   BUILD_TYPE="debug" ;;
        --install) DO_INSTALL=1 ;;
        --clean)   DO_CLEAN=1 ;;
        --arch=*)  ARCH="${arg#--arch=}" ;;
        *) echo "[error] 未知参数: $arg" >&2; exit 1 ;;
    esac
done

# ---- 前置检查 ----
[ -x "$QMAKE" ]      || { echo "[error] 找不到 qmake: $QMAKE" >&2; exit 1; }
[ -x "$DEPLOY_QT" ]  || { echo "[error] 找不到 androiddeployqt: $DEPLOY_QT" >&2; exit 1; }
[ -d "$ANDROID_SDK_ROOT" ] || { echo "[error] 找不到 Android SDK: $ANDROID_SDK_ROOT" >&2; exit 1; }

export ANDROID_SDK_ROOT
export ANDROID_HOME="$ANDROID_SDK_ROOT"
export ANDROID_NDK_ROOT
export JAVA_HOME
export QT_ANDROID_TARGET_ARCH="$ARCH"

if [ "$DO_CLEAN" = "1" ]; then
    echo "=== 清理 ==="
    rm -rf "$BUILD_DIR"
fi

mkdir -p "$BUILD_DIR"

echo "=== 1/3 qmake (Qt: $QT_ANDROID_DIR, 架构: $ARCH) ==="
( cd "$BUILD_DIR" && "$QMAKE" "$HERE/monitor.pro" )

echo "=== 2/3 make (编译 native 库) ==="
make -C "$BUILD_DIR" -j"$(nproc)"

# androiddeployqt 期望 native 库放在 build 目录的 libs/<abi>/ 下。
LIB_NAME="libbotagent_monitor_${ARCH}.so"
mkdir -p "$BUILD_DIR/libs/$ARCH"
cp -f "$BUILD_DIR/$LIB_NAME" "$BUILD_DIR/libs/$ARCH/$LIB_NAME"

echo "=== 3/3 androiddeployqt (打 APK) ==="
(
    cd "$BUILD_DIR"
    if [ "$BUILD_TYPE" = "release" ]; then
        "$DEPLOY_QT" --output . --input "android-botagent_monitor-deployment-settings.json" --release
    else
        "$DEPLOY_QT" --output . --input "android-botagent_monitor-deployment-settings.json"
    fi
)

# androiddeployqt 里调 gradlew 时会继承环境变量, 但它自己起的是 wrapper,
# 这里再显式跑一遍 gradle, 保证用上我们导出的 SDK/NDK 变量。
echo "=== gradle assemble ==="
(
    cd "$BUILD_DIR"
    if [ "$BUILD_TYPE" = "release" ]; then
        ./gradlew --no-daemon assembleRelease
    else
        ./gradlew --no-daemon assembleDebug
    fi
)

APK=$(find "$BUILD_DIR/build/outputs/apk" -name "*.apk" | head -1)
if [ -z "$APK" ]; then
    echo "[error] 没有找到 APK" >&2
    exit 1
fi

echo
echo "构建完成: $APK"
echo "大小: $(du -h "$APK" | cut -f1)"

if [ "$DO_INSTALL" = "1" ]; then
    echo "=== 安装到设备 ==="
    "$ANDROID_SDK_ROOT/platform-tools/adb" install -r "$APK"
fi
