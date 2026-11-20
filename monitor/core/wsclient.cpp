#include "wsclient.h"

#include <QDateTime>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QWebSocket>

namespace {
/// 重连退避上限 (毫秒)
constexpr int kMaxBackoffMs = 10000;
}  // namespace

WsClient::WsClient(QObject *parent)
    : QObject(parent)
{
}

WsClient::~WsClient()
{
    // **必须先断开 socket 的信号再关连接。**
    //
    // close() 会同步发出 disconnected 信号, 槽函数里会去碰上层对象
    // (MainWindow 的 model)。而 MainWindow 的析构是按子对象创建顺序来的 ——
    // model 排在 WsClient 前面, 已经被销毁, 于是 use-after-free,
    // 表现为退出时 std::bad_alloc / 段错误。
    //
    // 这里是**析构**场景 —— 对象马上要没了, 永久断信号是安全的。
    // (运行期的 stop() 不能这么做, 见 m_enabled 的注释)
    m_enabled = false;
    for (Source *source : m_sources) {
        source->retryTimer->stop();
        if (source->socket) {
            QObject::disconnect(source->socket, nullptr, this, nullptr);
            source->socket->close();
            source->socket = nullptr;
        }
        delete source->retryTimer;
        delete source;
    }
    m_sources.clear();
}

void WsClient::setSources(const QVector<WsConfig> &sources)
{
    for (Source *source : m_sources) {
        source->retryTimer->stop();
        if (source->socket) {
            // socket 的 parent 是 this, 会自动 delete —— 这里只断开
            source->socket->close();
            source->socket = nullptr;
        }
        delete source->retryTimer;
        delete source;
    }
    m_sources.clear();

    for (const WsConfig &cfg : sources) {
        auto *source = new Source;
        source->config = cfg;

        source->socket = new QWebSocket;
        source->socket->setParent(this);
        connect(source->socket, &QWebSocket::connected,
                this, &WsClient::onConnected);
        connect(source->socket, &QWebSocket::disconnected,
                this, &WsClient::onDisconnected);
        connect(source->socket, &QWebSocket::textMessageReceived,
                this, &WsClient::onTextMessageReceived);
        connect(source->socket, &QWebSocket::errorOccurred,
                this, &WsClient::onErrorOccurred);

        source->retryTimer = new QTimer(this);
        source->retryTimer->setSingleShot(true);
        connect(source->retryTimer, &QTimer::timeout, this, [this, source]() {
            connectSource(*source);
        });

        m_sources.append(source);
    }

    // 已经在监控状态下换源 —— 立刻把新 socket 连上。
    //
    // (start() 在已启用时会直接返回, 所以这里要自己补一次连接)
    if (m_enabled) {
        for (Source *source : m_sources)
            connectSource(*source);
    }
}

void WsClient::start()
{
    if (m_enabled)
        return;
    m_enabled = true;
    for (Source *source : m_sources)
        connectSource(*source);
}

void WsClient::stop()
{
    if (!m_enabled)
        return;
    m_enabled = false;

    for (Source *source : m_sources) {
        source->retryTimer->stop();
        if (source->socket) {
            // **不能在这里 disconnect()** ——
            // 那会把 socket 的信号永久断开, 之后再 start() 就再也收不到
            // 事件了 (思考树 / 流程卡片会一直是空的)。
            // 这里只关连接; 槽函数里会用 m_enabled 判断是否处理。
            source->socket->close();
        }
        if (source->connected) {
            source->connected = false;
            emit connectionChanged(source->config.name, false, QString());
        }
    }
}

bool WsClient::isConnected(const QString &name) const
{
    for (Source *source : m_sources) {
        if (source->config.name == name)
            return source->connected;
    }
    return false;
}

WsClient::Source *WsClient::findSource(const QString &name)
{
    for (Source *source : m_sources) {
        if (source->config.name == name)
            return source;
    }
    return nullptr;
}

void WsClient::connectSource(Source &source)
{
    if (!m_enabled || !source.socket)
        return;
    source.socket->abort();
    source.socket->open(QUrl(source.config.url));
}

void WsClient::scheduleRetry(Source &source)
{
    if (!m_enabled)
        return;
    if (source.retryTimer->isActive())
        return;

    const int delay = source.backoffMs;
    source.retryTimer->start(delay);

    // 退避: 1s -> 2s -> 4s -> 8s -> 10s (封顶)
    source.backoffMs = qMin(source.backoffMs * 2, kMaxBackoffMs);
}

void WsClient::onConnected()
{
    auto *socket = qobject_cast<QWebSocket *>(sender());
    if (!socket)
        return;

    for (Source *source : m_sources) {
        if (source->socket != socket)
            continue;
        source->connected = true;
        source->backoffMs = 1000;   // 连上了就复位退避
        emit connectionChanged(source->config.name, true, QString());
        return;
    }
}

void WsClient::onDisconnected()
{
    auto *socket = qobject_cast<QWebSocket *>(sender());
    if (!socket)
        return;

    for (Source *source : m_sources) {
        if (source->socket != socket)
            continue;
        if (source->connected) {
            source->connected = false;
            // 没启用时的断开是**我们自己停的**, 不是故障 ——
            // 别往流程面板里塞「连接断开」的系统事件。
            if (m_enabled)
                emit connectionChanged(source->config.name, false,
                                       QStringLiteral("连接断开"));
        }
        // 没启用就别重连了 (scheduleRetry 里会检查 m_enabled)
        scheduleRetry(*source);
        return;
    }
}

void WsClient::onErrorOccurred()
{
    auto *socket = qobject_cast<QWebSocket *>(sender());
    if (!socket)
        return;

    for (Source *source : m_sources) {
        if (source->socket != socket)
            continue;
        const QString err = socket->errorString();
        if (source->connected) {
            source->connected = false;
            if (m_enabled)
                emit connectionChanged(source->config.name, false, err);
        }
        // 出错后 disconnected 也会来, 那里会安排重连;
        // 但有些错误不触发 disconnected, 这里兜个底。
        scheduleRetry(*source);
        return;
    }
}

void WsClient::onTextMessageReceived(const QString &message)
{
    auto *socket = qobject_cast<QWebSocket *>(sender());
    if (!socket)
        return;

    // 没启用时收到的消息直接丢弃 (通常是 close() 之后残留的一帧)
    if (!m_enabled)
        return;

    QString sourceName;
    for (Source *source : m_sources) {
        if (source->socket == socket) {
            sourceName = source->config.name;
            break;
        }
    }
    if (sourceName.isEmpty())
        return;

    QJsonParseError err{};
    const QJsonDocument doc = QJsonDocument::fromJson(message.toUtf8(), &err);
    if (err.error != QJsonParseError::NoError || !doc.isObject()) {
        // 不是 JSON —— 当成纯文本事件报出去, 别静默丢掉
        QJsonObject obj;
        obj.insert(QStringLiteral("type"), QStringLiteral("text"));
        obj.insert(QStringLiteral("ts"), QDateTime::currentDateTime().toString(Qt::ISODateWithMs));
        QJsonObject payload;
        payload.insert(QStringLiteral("message"), message.left(500));
        obj.insert(QStringLiteral("payload"), payload);
        emit eventReceived(sourceName, obj);
        return;
    }

    emit eventReceived(sourceName, doc.object());
}
