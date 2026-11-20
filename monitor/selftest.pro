QT       += core gui widgets

CONFIG   += c++17
CONFIG   += console
CONFIG   -= app_bundle

TARGET   = botagent_monitor_selftest
TEMPLATE = app

INCLUDEPATH += $$PWD

HEADERS += \
    $$PWD/core/appconfig.h \
    $$PWD/models/monitormodel.h \
    $$PWD/models/thinkingmodel.h \
    $$PWD/ui/flowcard.h \
    $$PWD/ui/flowpanel.h

SOURCES += \
    $$PWD/selftest.cpp \
    $$PWD/core/appconfig.cpp \
    $$PWD/models/monitormodel.cpp \
    $$PWD/models/thinkingmodel.cpp \
    $$PWD/ui/flowcard.cpp \
    $$PWD/ui/flowpanel.cpp
