QT       += core gui widgets network

CONFIG   += c++17
CONFIG   += console
CONFIG   -= app_bundle

TARGET   = botagent_monitor_selftest
TEMPLATE = app

INCLUDEPATH += $$PWD

# 样式资源 —— 自测要能加载全局样式, 才能验证弹窗在真实主题下的排版。
RESOURCES += $$PWD/resources/resources.qrc

HEADERS += \
    $$PWD/core/agentclient.h \
    $$PWD/core/appconfig.h \
    $$PWD/core/qssmanager.h \
    $$PWD/models/monitormodel.h \
    $$PWD/models/thinkingmodel.h \
    $$PWD/ui/asrstatusindicator.h \
    $$PWD/ui/flowcard.h \
    $$PWD/ui/flowpanel.h \
    $$PWD/ui/servicedetaildialog.h

SOURCES += \
    $$PWD/selftest.cpp \
    $$PWD/core/agentclient.cpp \
    $$PWD/core/appconfig.cpp \
    $$PWD/core/qssmanager.cpp \
    $$PWD/models/monitormodel.cpp \
    $$PWD/models/thinkingmodel.cpp \
    $$PWD/ui/asrstatusindicator.cpp \
    $$PWD/ui/flowcard.cpp \
    $$PWD/ui/flowpanel.cpp \
    $$PWD/ui/servicedetaildialog.cpp
