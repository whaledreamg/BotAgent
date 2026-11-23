QT       += core gui widgets network websockets

CONFIG   += c++17
CONFIG   += console
CONFIG   -= app_bundle

TARGET   = e2e_thinking
TEMPLATE = app

INCLUDEPATH += $$PWD

HEADERS += \
    $$PWD/models/thinkingmodel.h

SOURCES += \
    $$PWD/e2e_thinking.cpp \
    $$PWD/models/thinkingmodel.cpp
