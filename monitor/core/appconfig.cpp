#include "appconfig.h"

#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLoggingCategory>
#include <QStringList>
#include <QUrl>

QString ServiceConfig::baseUrl() const
{
    return QStringLiteral("http://%1:%2").arg(host).arg(port);
}

QString ServiceConfig::healthUrl() const
{
    return baseUrl() + healthPath;
}

QString ServiceConfig::statusUrl() const
{
    return statusPath.isEmpty() ? QString() : baseUrl() + statusPath;
}

ServiceConfig ServiceConfig::withHost(const QString &newHost) const
{
    ServiceConfig s = *this;
    if (!newHost.trimmed().isEmpty())
        s.host = newHost.trimmed();
    return s;
}

WsConfig WsConfig::withHost(const QString &newHost) const
{
    WsConfig w = *this;
    const QString host = newHost.trimmed();
    if (host.isEmpty())
        return w;

    // 只换 URL 里的 host 部分, 端口与路径保持原样。
    //
    // ws://127.0.0.1:8800/v1/events/  ->  ws://192.168.1.10:8800/v1/events/
    const QUrl u(url);
    if (!u.isValid() || u.host().isEmpty()) {
        // URL 不合法 —— 退回字符串替换 (把原来的 host 换掉)
        const QString oldHost = u.host();
        if (!oldHost.isEmpty()) {
            w.url = QString(url).replace(oldHost, host);
        }
        return w;
    }

    QUrl newUrl = u;
    newUrl.setHost(host);
    w.url = newUrl.toString();
    return w;
}

AppConfig AppConfig::defaults()
{
    AppConfig cfg;
    cfg.pollIntervalMs = 2000;
    cfg.requestTimeoutMs = 3000;
    cfg.autoStart = false;
    cfg.defaultHost = QStringLiteral("127.0.0.1");

    const auto svc = [](const char *name, const char *host, int port,
                        const char *health, const char *status) {
        ServiceConfig s;
        s.name = QString::fromLatin1(name);
        s.host = QString::fromLatin1(host);
        s.port = port;
        s.healthPath = QString::fromLatin1(health);
        s.statusPath = QString::fromLatin1(status);
        return s;
    };

    cfg.services = {
        svc("Agent", "127.0.0.1", 8800, "/health", "/v1/branches"),
        svc("ASR", "127.0.0.1", 8801, "/health", ""),
        svc("TTS", "127.0.0.1", 8000, "/health", ""),
        svc("Arm", "127.0.0.1", 8802, "/health", "/v1/status"),
        svc("Vision", "127.0.0.1", 8803, "/health", "/v1/status"),
    };

    const auto ws = [](const char *name, const char *url) {
        WsConfig w;
        w.name = QString::fromLatin1(name);
        w.url = QString::fromLatin1(url);
        return w;
    };

    cfg.websockets = {
        ws("Agent", "ws://127.0.0.1:8800/v1/events/"),
        ws("Arm", "ws://127.0.0.1:8802/ws"),
        ws("Vision", "ws://127.0.0.1:8803/ws"),
    };

    return cfg;
}

AppConfig AppConfig::load(const QString &path)
{
    // 路径没给的话, 找几个常见位置:
    //   1) 程序所在目录的 config.json (build/ 旁边)
    //   2) 源码目录的 config.json (从 build/ 往上两级)
    QStringList candidates;
    if (!path.isEmpty()) {
        candidates << path;
    } else {
        const QString appDir = QDir::currentPath();
        candidates << appDir + QStringLiteral("/config.json");
        candidates << appDir + QStringLiteral("/../config.json");
        candidates << appDir + QStringLiteral("/../../config.json");
        candidates << appDir + QStringLiteral("/../monitor/config.json");
    }

    for (const QString &candidate : candidates) {
        QFile file(candidate);
        if (!file.exists() || !file.open(QIODevice::ReadOnly))
            continue;

        const QByteArray raw = file.readAll();
        file.close();

        QJsonParseError err{};
        const QJsonDocument doc = QJsonDocument::fromJson(raw, &err);
        if (err.error != QJsonParseError::NoError || !doc.isObject()) {
            qWarning("[配置] 解析失败 (%s): %s", qPrintable(candidate),
                     qPrintable(err.errorString()));
            continue;
        }

        const QJsonObject root = doc.object();
        AppConfig cfg = defaults();

        if (root.contains(QStringLiteral("pollIntervalMs")))
            cfg.pollIntervalMs = root.value(QStringLiteral("pollIntervalMs")).toInt(cfg.pollIntervalMs);
        if (root.contains(QStringLiteral("requestTimeoutMs")))
            cfg.requestTimeoutMs = root.value(QStringLiteral("requestTimeoutMs")).toInt(cfg.requestTimeoutMs);
        if (root.contains(QStringLiteral("autoStart")))
            cfg.autoStart = root.value(QStringLiteral("autoStart")).toBool(cfg.autoStart);
        if (root.contains(QStringLiteral("defaultHost")))
            cfg.defaultHost = root.value(QStringLiteral("defaultHost")).toString(cfg.defaultHost);

        if (root.contains(QStringLiteral("services"))) {
            const QJsonArray arr = root.value(QStringLiteral("services")).toArray();
            QVector<ServiceConfig> list;
            for (const QJsonValue &v : arr) {
                const QJsonObject o = v.toObject();
                if (!o.contains(QStringLiteral("name")))
                    continue;
                ServiceConfig s;
                s.name = o.value(QStringLiteral("name")).toString();
                s.host = o.value(QStringLiteral("host")).toString(QStringLiteral("127.0.0.1"));
                s.port = o.value(QStringLiteral("port")).toInt();
                s.healthPath = o.value(QStringLiteral("healthPath")).toString(QStringLiteral("/health"));
                s.statusPath = o.value(QStringLiteral("statusPath")).toString();
                list << s;
            }
            if (!list.isEmpty())
                cfg.services = list;
        }

        if (root.contains(QStringLiteral("websockets"))) {
            const QJsonArray arr = root.value(QStringLiteral("websockets")).toArray();
            QVector<WsConfig> list;
            for (const QJsonValue &v : arr) {
                const QJsonObject o = v.toObject();
                if (!o.contains(QStringLiteral("url")))
                    continue;
                WsConfig w;
                w.name = o.value(QStringLiteral("name")).toString();
                w.url = o.value(QStringLiteral("url")).toString();
                list << w;
            }
            if (!list.isEmpty())
                cfg.websockets = list;
        }

        qInfo("[配置] 已加载: %s", qPrintable(candidate));
        return cfg;
    }

    qInfo("[配置] 未找到 config.json, 使用内置默认配置");
    return defaults();
}
