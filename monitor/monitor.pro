QT       += core gui network websockets widgets

CONFIG   += c++17
CONFIG   += thread

TARGET   = botagent_monitor
TEMPLATE = app

DEFINES += QT_DEPRECATED_WARNINGS

INCLUDEPATH += $$PWD

# 包含子模块配置
include(core/core.pri)
include(models/models.pri)
include(ui/ui.pri)

# 主窗口和入口
SOURCES += main.cpp \
           mainwindow.cpp

HEADERS += mainwindow.h

FORMS   += mainwindow.ui

# 资源文件 (QSS 样式 + 应用图标)
RESOURCES += resources/resources.qrc

# 应用图标: 桌面端由 main.cpp 的 setWindowIcon 从 qrc 读取,
# 这行让 Linux 打包时也能生成 .desktop 需要的图标文件。
unix:!android {
    icon.files = resources/icons/appicon.png
    icon.path = /usr/share/icons/hicolor/256x256/apps
    INSTALLS += icon
}

DISTFILES += \
    android/AndroidManifest.xml \
    android/build.gradle \
    android/gradle.properties \
    android/gradle/wrapper/gradle-wrapper.jar \
    android/gradle/wrapper/gradle-wrapper.properties \
    android/gradlew \
    android/gradlew.bat \
    android/res/values/libs.xml \
    android/res/values/styles.xml \
    android/res/xml/qtprovider_paths.xml \
    android/res/mipmap/appicon.png \
    config.json \
    README.md

# ---- Android ----
#
# Qt 的 Android 部署需要 OpenSSL (WebSocket 的 wss:// 与 TLS 后端依赖它)。
# openssl.pri 会把预编译的 libssl/libcrypto 塞进 ANDROID_EXTRA_LIBS。
#
# 路径不能写死 —— 开发机换路径后就找不到了。这里按优先级探测:
#   1. ANDROID_SDK_ROOT 环境变量 (build_android.sh 会导出)
#   2. 常见的 SDK 安装位置
ANDROID_OPENSSL_PRI = $$ANDROID_SDK_ROOT/android_openssl/openssl.pri
!exists($$ANDROID_OPENSSL_PRI) {
    ANDROID_OPENSSL_PRI = \
        /home/*/Android/Sdk/android_openssl/openssl.pri
}
exists($$ANDROID_OPENSSL_PRI) {
    android: include($$ANDROID_OPENSSL_PRI)
} else {
    android: warning("找不到 android_openssl/openssl.pri —— wss:// 将不可用")
}

contains(ANDROID_TARGET_ARCH,arm64-v8a) {
    ANDROID_PACKAGE_SOURCE_DIR = \
        $$PWD/android
}
