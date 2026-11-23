#include "visionclient.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QTimer>
#include <QUrl>

VisionClient::VisionClient(QObject *parent)
    : QObject(parent)
    , m_nam(new QNetworkAccessManager(this))
{
}

VisionClient::~VisionClient()
{
    // 还有请求在飞的话必须断掉 —— 否则 replyFinished 会在析构后触发
    if (m_reply) {
        m_reply->abort();
        m_reply->deleteLater();
        m_reply = nullptr;
    }
}

void VisionClient::setBaseUrl(const QString &baseUrl)
{
    QString url = baseUrl.trimmed();
    // 去掉尾部斜杠 —— 否则拼出来会是 http://host:8803//v1/detect
    while (url.endsWith(QLatin1Char('/')))
        url.chop(1);
    m_baseUrl = url;
}

void VisionClient::setTimeout(int ms)
{
    m_timeoutMs = qMax(1000, ms);
}

bool VisionClient::detect()
{
    if (m_baseUrl.isEmpty()) {
        emit failed(QStringLiteral("视觉服务地址未配置"));
        return false;
    }

    // ---- 上一次还没回来就 abort 掉 ----
    //
    // 连着点「识别一次」不该堆一堆请求 —— 相机只有一条流,
    // 并发采帧会互相踩。
    if (m_reply) {
        m_reply->abort();
        m_reply->deleteLater();
        m_reply = nullptr;
    }

    QNetworkRequest req{QUrl(m_baseUrl + QStringLiteral("/v1/detect"))};
    req.setHeader(QNetworkRequest::ContentTypeHeader,
                  QStringLiteral("application/json; charset=utf-8"));

    // 空 body = 用服务端的默认参数 (全部类别 / 默认阈值)。
    const QByteArray body = QJsonDocument(QJsonObject{}).toJson(QJsonDocument::Compact);

    m_reply = m_nam->post(req, body);
    connect(m_reply, &QNetworkReply::finished, this, &VisionClient::onReplyFinished);

    // 超时兜底 —— 相机没连上时服务可能卡在重连上
    QTimer::singleShot(m_timeoutMs, m_reply, [this] {
        if (m_reply && m_reply->isRunning())
            m_reply->abort();
    });

    return true;
}

void VisionClient::onReplyFinished()
{
    QNetworkReply *reply = m_reply;
    m_reply = nullptr;
    if (!reply)
        return;
    reply->deleteLater();

    // ---- 网络层错误 ----
    if (reply->error() != QNetworkReply::NoError) {
        const QString err = reply->errorString();
        if (reply->error() == QNetworkReply::OperationCanceledError)
            emit failed(QStringLiteral("识别超时 (%1 秒未响应)").arg(m_timeoutMs / 1000));
        else
            emit failed(err);
        return;
    }

    // ---- 解析响应 ----
    const QByteArray raw = reply->readAll();
    QJsonParseError perr{};
    const QJsonDocument doc = QJsonDocument::fromJson(raw, &perr);
    if (perr.error != QJsonParseError::NoError || !doc.isObject()) {
        emit failed(QStringLiteral("响应解析失败: %1").arg(perr.errorString()));
        return;
    }

    const QJsonObject obj = doc.object();

    // FastAPI 的校验错误会返回 {"detail": ...}
    if (obj.contains(QStringLiteral("detail"))) {
        const QJsonValue detail = obj.value(QStringLiteral("detail"));
        QString text = detail.isArray()
                           ? detail.toArray().first().toObject()
                                 .value(QStringLiteral("msg")).toString()
                           : detail.toString();
        if (text.isEmpty())
            text = QStringLiteral("请求被拒绝");
        emit failed(text);
        return;
    }

    const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    if (status < 200 || status >= 300) {
        QString msg = obj.value(QStringLiteral("error")).toString();
        if (msg.isEmpty())
            msg = QStringLiteral("HTTP %1").arg(status);
        emit failed(msg);
        return;
    }

    emit detected(obj);
}
