#pragma once

#include <QHash>
#include <QJsonObject>
#include <QObject>
#include <QVector>

#include "appconfig.h"

class QNetworkAccessManager;
class QNetworkReply;
class QTimer;

/// 定时轮询各服务的健康/状态接口。
///
/// 每个服务两个请求:
///   - healthPath —— 必需。请求成功即视为「连上了」。
///   - statusPath —— 可选。拉不到不影响健康判定, 只是少了详细信息。
///
/// 所有请求都是异步的, 互不阻塞; 任何一个服务卡住都不会影响其它服务。
class ServicePoller : public QObject {
    Q_OBJECT

public:
    explicit ServicePoller(QObject *parent = nullptr);
    ~ServicePoller() override;

    /// 设置要监控的服务列表。
    void setServices(const QVector<ServiceConfig> &services);
    /// 轮询间隔 (毫秒)。
    void setInterval(int intervalMs);
    /// 单个请求超时 (毫秒)。
    void setRequestTimeout(int timeoutMs);

    /// 开始/暂停轮询。
    void start();
    void stop();
    bool isRunning() const;

    /// 已完成的轮询次数。
    int pollCount() const { return m_pollCount; }

signals:
    /// 某个服务探测完成。
    ///
    /// ``state`` 是 MonitorModel::ServiceState 的取值:
    ///   0=未知 1=正常 2=降级 3=断开
    void serviceUpdated(const QString &name, int state, int latencyMs,
                        const QString &detail, const QString &rawStatus,
                        const QJsonObject &statusJson);
    /// 某个服务探测失败 (连不上/超时/非 200)。
    void serviceFailed(const QString &name, const QString &error);
    /// 轮询次数变化 (用于状态栏显示)。
    void pollCountChanged(int count);

private slots:
    void onTimeout();
    void onReplyFinished();

private:
    /// 一次请求的上下文 —— 用来把回复对应回「哪个服务、哪个接口」。
    struct Request {
        QString serviceName;
        bool isHealth = true;
        qint64 startedMs = 0;
        /// 健康接口返回的 JSON (状态接口的回复到达时用来合并)
        QJsonObject healthJson;
    };

    /// 发起一次探测。
    void probe(const ServiceConfig &svc);
    /// 从健康接口的 JSON 里判断服务状态与说明文字。
    static int interpretState(const QJsonObject &health, QString *detail);

    QHash<QNetworkReply *, Request> m_pending;
    QNetworkAccessManager *m_nam = nullptr;
    QTimer *m_timer = nullptr;
    QVector<ServiceConfig> m_services;
    int m_intervalMs = 2000;
    int m_timeoutMs = 3000;
    int m_pollCount = 0;
};
