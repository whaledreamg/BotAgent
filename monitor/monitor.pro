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

# 资源文件 (QSS 样式)
RESOURCES += resources/resources.qrc

DISTFILES += \
    config.json \
    README.md
