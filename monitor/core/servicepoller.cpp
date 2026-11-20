#include "servicepoller.h"

#include <QDateTime>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QTimer>
#include <QUrl>

ServicePoller::ServicePoller(QObject *parent)
    : QObject(parent)
    , m_nam(new QNetworkAccessManager(this))
    , m_timer(new QTimer(this))
{
    m_timer->setInterval(m_intervalMs);
    connect(m_timer, &QTimer::timeout, this, &ServicePoller::onTimeout);
}

ServicePoller::~ServicePoller()
{
    // 把还在飞的请求都断掉 —— 不然 replyFinished 会在对象析构后触发
    const auto replies = m_pending.keys();
    m_pending.clear();
    for (QNetworkReply *reply : replies) {
        reply->abort();
        reply->deleteLater();
    }
}

void ServicePoller::setServices(const QVector<ServiceConfig> &services)
{
    m_services = services;
}

void ServicePoller::setInterval(int intervalMs)
{
    m_intervalMs = qMax(200, intervalMs);
    m_timer->setInterval(m_intervalMs);
}

void ServicePoller::setRequestTimeout(int timeoutMs)
{
    m_timeoutMs = qMax(500, timeoutMs);
}

void ServicePoller::start()
{
    if (!m_timer->isActive()) {
        m_timer->start();
        // 立刻探一次, 别让用户等一个间隔才看到状态
        onTimeout();
    }
}

void ServicePoller::stop()
{
    m_timer->stop();
}

bool ServicePoller::isRunning() const
{
    return m_timer->isActive();
}

void ServicePoller::onTimeout()
{
    ++m_pollCount;
    emit pollCountChanged(m_pollCount);

    for (const ServiceConfig &svc : m_services)
        probe(svc);
}

void ServicePoller::probe(const ServiceConfig &svc)
{
    // ---- 健康检查 ----
    {
        QNetworkRequest req{QUrl(svc.healthUrl())};
        req.setRawHeader("Accept", "application/json");
        req.setTransferTimeout(m_timeoutMs);

        QNetworkReply *reply = m_nam->get(req);
        Request r;
        r.serviceName = svc.name;
        r.isHealth = true;
        r.startedMs = QDateTime::currentMSecsSinceEpoch();
        m_pending.insert(reply, r);
        connect(reply, &QNetworkReply::finished, this, &ServicePoller::onReplyFinished);
    }

    // ---- 详细状态 (可选) ----
    if (!svc.statusPath.isEmpty()) {
        QNetworkRequest req{QUrl(svc.statusUrl())};
        req.setRawHeader("Accept", "application/json");
        req.setTransferTimeout(m_timeoutMs);

        QNetworkReply *reply = m_nam->get(req);
        Request r;
        r.serviceName = svc.name;
        r.isHealth = false;
        r.startedMs = QDateTime::currentMSecsSinceEpoch();
        m_pending.insert(reply, r);
        connect(reply, &QNetworkReply::finished, this, &ServicePoller::onReplyFinished);
    }
}

int ServicePoller::interpretState(const QJsonObject &health, QString *detail)
{
    // 状态枚举: 0=未知 1=正常 2=降级 3=断开
    // (与 MonitorModel::ServiceState 一致)
    const QString status = health.value(QStringLiteral("status")).toString();

    // ---- 降级信号: 服务连上了, 但自报还没就绪 ----
    //
    // TTS 的 /health 在模型没加载完时返回 status="loading";
    // Vision 在相机没连上时 status 仍是 ok, 要看 camera_connected。
    if (status == QStringLiteral("loading")) {
        if (detail)
            *detail = QStringLiteral("模型加载中");
        return 2;
    }
    if (status == QStringLiteral("error") || status == QStringLiteral("failed")) {
        if (detail)
            *detail = health.value(QStringLiteral("detail")).toString(status);
        return 2;
    }
    if (health.contains(QStringLiteral("model_loaded"))
        && !health.value(QStringLiteral("model_loaded")).toBool()) {
        if (detail)
            *detail = QStringLiteral("模型未加载");
        return 2;
    }
    if (health.contains(QStringLiteral("camera_connected"))
        && !health.value(QStringLiteral("camera_connected")).toBool()) {
        if (detail)
            *detail = QStringLiteral("相机未连接");
        return 2;
    }
    if (health.contains(QStringLiteral("controller_connected"))
        && !health.value(QStringLiteral("controller_connected")).toBool()) {
        if (detail)
            *detail = QStringLiteral("控制器未连接");
        return 2;
    }

    if (detail) {
        // 优先展示几个最有信息量的字段
        const QStringList keys = {
            QStringLiteral("state"),        // ASR 的状态机
            QStringLiteral("model_loaded"),
            QStringLiteral("camera_connected"),
            QStringLiteral("controller_connected"),
            QStringLiteral("model"),
        };
        QStringList bits;
        for (const QString &key : keys) {
            if (!health.contains(key))
                continue;
            const QJsonValue v = health.value(key);
            if (v.isBool())
                bits << QStringLiteral("%1=%2").arg(key, v.toBool() ? QStringLiteral("true")
                                                                     : QStringLiteral("false"));
            else if (v.isString())
                bits << QStringLiteral("%1=%2").arg(key, v.toString());
        }
        *detail = bits.isEmpty() ? status : bits.join(QStringLiteral(" "));
    }
    return 1;
}

void ServicePoller::onReplyFinished()
{
    auto *reply = qobject_cast<QNetworkReply *>(sender());
    if (!reply)
        return;

    const Request req = m_pending.take(reply);
    reply->deleteLater();

    if (req.serviceName.isEmpty())
        return;

    const int latencyMs =
        static_cast<int>(QDateTime::currentMSecsSinceEpoch() - req.startedMs);

    // ---- 失败: 连不上 / 超时 / 非 2xx ----
    if (reply->error() != QNetworkReply::NoError) {
        const QString err = reply->errorString();
        if (req.isHealth) {
            emit serviceFailed(req.serviceName, err);
        } else {
            // 状态接口拉不到不影响健康判定, 只是没详细信息
            emit serviceUpdated(req.serviceName, -1, latencyMs,
                                QStringLiteral("状态接口不可用: %1").arg(err),
                                QString(), QJsonObject());
        }
        return;
    }

    const QByteArray body = reply->readAll();
    QJsonObject json;
    if (!body.isEmpty()) {
        const QJsonDocument doc = QJsonDocument::fromJson(body);
        if (doc.isObject())
            json = doc.object();
    }

    if (!req.isHealth) {
        // 状态接口 —— 只更新详细信息, 健康状态由健康接口决定
        emit serviceUpdated(req.serviceName, -1, latencyMs, QString(), QString(), json);
        return;
    }

    // ---- 健康接口 ----
    QString detail;
    const int state = interpretState(json, &detail);
    emit serviceUpdated(req.serviceName, state, latencyMs, detail,
                        QString::fromUtf8(body.left(400)), json);
}
