// 真实链路验证: 订阅 Agent 的事件流, 把事件喂给 ThinkingModel,
// 看树里到底渲染出了什么。
//
// 前提: Agent 服务在 8800, 且 llm.thinking.enabled=true (否则没有 phase=chat)。
//
// 用法:
//   cd monitor && QT_QPA_PLATFORM=offscreen ./build/e2e_thinking
#include <QCoreApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QTimer>
#include <QWebSocket>
#include <QDebug>

#include "models/thinkingmodel.h"

namespace {

void dumpTree(ThinkingModel &m, const QModelIndex &parent, int depth)
{
    const int rows = m.rowCount(parent);
    for (int r = 0; r < rows; ++r) {
        const QModelIndex idx = m.index(r, 0, parent);
        const QString title = m.data(idx, ThinkingModel::TitleRole).toString();
        const QString value = m.data(idx, ThinkingModel::ValueRole).toString();
        QString line = QString(depth * 2, QLatin1Char(' ')) + title;
        if (!value.isEmpty())
            line += QStringLiteral("  |  ") + value.left(90);
        qInfo().noquote() << line;
        dumpTree(m, idx, depth + 1);
    }
}

}  // namespace

int main(int argc, char *argv[])
{
    QCoreApplication app(argc, argv);

    ThinkingModel model;
    QWebSocket ws;
    QTimer timer;
    timer.setSingleShot(true);
    timer.setInterval(12000);

    QObject::connect(&ws, &QWebSocket::connected, [&] {
        qInfo() << "WS 已连接, 触发一次对话...";
        // 用 /v1/chat 触发纯 chat 分支 (会走 phase=chat)
        QNetworkAccessManager *mgr = new QNetworkAccessManager(&app);
        QNetworkRequest req(QUrl(QStringLiteral("http://127.0.0.1:8800/v1/chat")));
        req.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
        QJsonObject body{{"text", QStringLiteral("用两句话介绍一下量子计算")},
                         {"session_id", QStringLiteral("monitor-e2e")}};
        QNetworkReply *rep = mgr->post(req, QJsonDocument(body).toJson(QJsonDocument::Compact));
        QObject::connect(rep, &QNetworkReply::finished, [rep] {
            qInfo() << "对话完成";
            rep->deleteLater();
        });
    });

    QObject::connect(&ws, &QWebSocket::textMessageReceived, [&](const QString &msg) {
        const QJsonObject ev = QJsonDocument::fromJson(msg.toUtf8()).object();
        const QString type = ev.value(QStringLiteral("type")).toString();
        if (type == QStringLiteral("thinking")) {
            const QJsonObject p = ev.value(QStringLiteral("payload")).toObject();
            qInfo().noquote()
                << QStringLiteral("<< thinking phase=%1  reasoning长度=%2 tokens=%3")
                       .arg(p.value(QStringLiteral("phase")).toString())
                       .arg((p.value(QStringLiteral("reasoning")).toString().size()))
                       .arg(p.value(QStringLiteral("reasoning_tokens")).toInt());
        }
        model.handleEvent(ev);
    });

    QObject::connect(&timer, &QTimer::timeout, [&] {
        qInfo() << "\n========== 渲染出来的树 ==========";
        dumpTree(model, QModelIndex(), 0);
        app.quit();
    });

    ws.open(QUrl(QStringLiteral("ws://127.0.0.1:8800/v1/events/")));
    QTimer::singleShot(1500, &timer, [&timer]() { timer.start(); });
    return app.exec();}
