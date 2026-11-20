#pragma once

#include <QObject>
#include <QTimer>
#include <QVector>

#include "appconfig.h"

class QWebSocket;

/// 一个 WebSocket 事件源 (Agent 的事件推送 / Arm 的播放事件 / Vision 的检测推送)。
///
/// 断线后自动重连, 退避 1s → 2s → 4s → 8s → 10s (封顶),
/// 连上后退避复位。
///
/// **start() / stop() 可反复切换** —— stop() 只是断开连接并停掉重连定时器,
/// socket 的信号始终保持连接, 所以再次 start() 后能正常收到事件。
class WsClient : public QObject {
    Q_OBJECT

public:
    explicit WsClient(QObject *parent = nullptr);
    ~WsClient() override;

    /// 设置要订阅的 WebSocket 列表。
    void setSources(const QVector<WsConfig> &sources);
    /// 开始/停止全部连接 (可反复调用)。
    void start();
    void stop();

    /// 当前是否处于「已启用」状态。
    bool isEnabled() const { return m_enabled; }

    /// 某个源当前是否已连接。
    bool isConnected(const QString &name) const;

signals:
    /// 收到一条事件 (JSON 已经解析好)。
    void eventReceived(const QString &source, const QJsonObject &event);
    /// 连接状态变化 (用于状态栏显示)。
    void connectionChanged(const QString &name, bool connected, const QString &error);

private slots:
    void onConnected();
    void onDisconnected();
    void onTextMessageReceived(const QString &message);
    void onErrorOccurred();

private:
    struct Source {
        WsConfig config;
        QWebSocket *socket = nullptr;
        QTimer *retryTimer = nullptr;
        int backoffMs = 1000;
        bool connected = false;
    };

    Source *findSource(const QString &name);
    void connectSource(Source &source);
    void scheduleRetry(Source &source);

    QVector<Source *> m_sources;

    /// 是否处于「已启用」状态。
    ///
    /// **不能用 disconnect() 来实现 stop()** —— 那会把 socket 的信号
    /// 永久断开, 之后再 start() 就收不到任何事件了。
    /// 这里用标志位: stop() 只关连接 + 停重连, 槽函数里看到
    /// m_enabled == false 就直接忽略。
    bool m_enabled = false;
};
