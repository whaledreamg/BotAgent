#pragma once

#include <QByteArray>
#include <QString>
#include <QVector>

/// 一个要监控的 HTTP 服务。
struct ServiceConfig {    QString name;
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

    /// **把配置写回 JSON 文件**。
    ///
    /// 只写 `defaultHost` 一个字段 —— 文件里其余内容 (services /
    /// websockets / pollIntervalMs ...) **原样保留**。
    ///
    /// 为什么要保留而不是整体重写: 用户可能手改过 config.json 里的
    /// 端口、路径。整体重写会把那些改动抹掉。
    ///
    /// 写回的位置是**上次 load() 实际读的那个文件** (见 loadedPath());
    /// 没读到过就写 `config.json` 到当前工作目录。
    ///
    /// Returns: 写成功返回 true; 失败 (只读介质/权限不足) 返回 false。
    static bool save(const AppConfig &cfg, const QString &path = QString());

    /// 上次 load() **实际读到**的配置文件路径。
    ///
    /// 没读到过 (用了内置默认配置) 返回空串。
    static QString loadedPath();

    /// 配置文件的**候选路径** —— 按优先级从高到低。
    ///
    /// 暴露出来是为了让 save() 和 load() 用同一套候选。
    static QStringList candidatePaths(const QString &path = QString());

private:
    /// 在 JSON 文本里**就地**替换 defaultHost 的值, 保持其余排版不变。
    ///
    /// 找不到该键 / 值不是字符串时返回空 QByteArray ——
    /// 调用方会退回「整体重写」。
    static QByteArray replaceHostInJson(const QByteArray &json, const QString &host);
};
