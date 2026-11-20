#pragma once

#include <QString>
#include <QVector>

/// 一个要监控的 HTTP 服务。
struct ServiceConfig {
    QString name;
    QString host;
    int port = 0;
    /// 健康检查路径 (必填)
    QString healthPath;
    /// 详细状态路径 (可选, 如 /v1/status); 拉不到不影响健康判定
    QString statusPath;

    QString baseUrl() const;
    QString healthUrl() const;
    QString statusUrl() const;

    /// 换成另一个主机 (返回副本) —— 界面上的「监控 IP」改了就靠它。
    ServiceConfig withHost(const QString &newHost) const;
};

/// 一个要订阅的 WebSocket 事件源。
struct WsConfig {
    QString name;
    QString url;

    /// 换成另一个主机 (返回副本) —— 只改 URL 里的 host 部分, 端口/路径不动。
    WsConfig withHost(const QString &newHost) const;
};

/// 监控 APP 的整体配置。
struct AppConfig {
    int pollIntervalMs = 2000;
    int requestTimeoutMs = 3000;
    /// 启动时是否立刻开始监控。
    ///
    /// 默认 **false** —— 打开窗口先停在「未监控」, 由用户点「开始监控」
    /// 才发起轮询与 WebSocket 订阅。
    bool autoStart = false;
    /// 界面「监控 IP」输入框的初始值。
    ///
    /// 填了就作为所有服务与 WebSocket 的目标主机; 开始监控时以输入框
    /// 里的实际内容为准 (IP 或主机名都行)。
    QString defaultHost = QStringLiteral("127.0.0.1");
    /// 手动指令用的 session id。
    ///
    /// 固定成独立的一个 (默认 monitor-ui), 这样手动输入的历史
    /// **不会混进语音链路** 的 robot-01 会话里。
    QString agentSessionId = QStringLiteral("monitor-ui");
    QVector<ServiceConfig> services;
    QVector<WsConfig> websockets;

    /// 从 JSON 文件读取; 读不到就用内置默认值。
    static AppConfig load(const QString &path = QString());
    /// 内置默认配置 (与 monitor/config.json 一致)。
    static AppConfig defaults();
};
