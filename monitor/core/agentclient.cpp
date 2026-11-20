#include "agentclient.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QTimer>
#include <QUrl>

AgentClient::AgentClient(QObject *parent)
    : QObject(parent)
    , m_nam(new QNetworkAccessManager(this))
{
}

AgentClient::~AgentClient()
{
    // 还有请求在飞的话必须断掉 —— 否则 replyFinished 会在析构后触发
    if (m_reply) {
        m_reply->abort();
        m_reply->deleteLater();
        m_reply = nullptr;
    }
}

void AgentClient::setBaseUrl(const QString &baseUrl)
{
    QString url = baseUrl.trimmed();
    // 去掉尾部斜杠 —— 否则拼出来会是 http://host:8800//v1/dispatch
    while (url.endsWith(QLatin1Char('/')))
        url.chop(1);
    m_baseUrl = url;
}

void AgentClient::setTimeout(int ms)
{
    m_timeoutMs = qMax(1000, ms);
}

bool AgentClient::dispatch(const QString &text, const QString &sessionId)
{
    const QString body = text.trimmed();
    if (body.isEmpty())
        return false;
    if (m_baseUrl.isEmpty()) {
        emit failed(QStringLiteral("Agent 地址未配置"));
        return false;
    }

    // ---- 上一个还没回来就 abort 掉 ----
    //
    // 避免快速连点时一堆请求堆积、回复乱序。
    // (WS 事件不受影响 —— 思考过程照样能收全)
    if (m_reply) {
        m_reply->abort();
        m_reply->deleteLater();
        m_reply = nullptr;
    }

    m_sessionId = sessionId.isEmpty() ? QStringLiteral("monitor-ui") : sessionId;

    QNetworkRequest req{QUrl(m_baseUrl + QStringLiteral("/v1/dispatch"))};
    req.setHeader(QNetworkRequest::ContentTypeHeader,
                  QStringLiteral("application/json; charset=utf-8"));

    const QJsonObject payload{
        {QStringLiteral("session_id"), m_sessionId},
        {QStringLiteral("text"), body},
        // 手动指令之间保留上下文 —— 连着问几件相关的事能接上话
        {QStringLiteral("use_history"), true},
    };

    m_reply = m_nam->post(req, QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(m_reply, &QNetworkReply::finished, this, &AgentClient::onReplyFinished);

    // 超时兜底 —— Agent 要走 LLM, 可能比默认超时久
    QTimer::singleShot(m_timeoutMs, m_reply, [this] {
        if (m_reply && m_reply->isRunning()) {
            m_reply->abort();
        }
    });

    return true;
}

void AgentClient::onReplyFinished()
{
    QNetworkReply *reply = m_reply;
    m_reply = nullptr;
    if (!reply)
        return;
    reply->deleteLater();

    // ---- 网络层错误 ----
    if (reply->error() != QNetworkReply::NoError) {
        const QString err = reply->errorString();
        // abort 是我们自己超时触发的 —— 报「超时」而不是笼统的网络错误
        if (reply->error() == QNetworkReply::OperationCanceledError)
            emit failed(QStringLiteral("请求超时 (%1 秒未响应)").arg(m_timeoutMs / 1000));
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

    // FastAPI 的校验错误会返回 {"detail": [...]}
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

    emit dispatched(obj);
}
