#include "appconfig.h"

#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLoggingCategory>
#include <QSaveFile>
#include <QStandardPaths>
#include <QStringList>
#include <QUrl>

namespace {
/// 上次 load() 实际读到的配置文件路径 (空 = 没读到, 用了内置默认)。
QString g_loadedPath;
}

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
    cfg.agentSessionId = QStringLiteral("monitor-ui");

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

QStringList AppConfig::candidatePaths(const QString &path)
{
    // 路径没给的话, 找几个常见位置:
    //   1) 程序所在目录的 config.json (build/ 旁边)
    //   2) 源码目录的 config.json (从 build/ 往上两级)
    //   3) Android: 应用私有配置目录 (CWD 不保证是应用目录)
    QStringList candidates;
    if (!path.isEmpty()) {
        candidates << path;
    } else {
        const QString appDir = QDir::currentPath();
        candidates << appDir + QStringLiteral("/config.json");
        candidates << appDir + QStringLiteral("/../config.json");
        candidates << appDir + QStringLiteral("/../../config.json");
        candidates << appDir + QStringLiteral("/../monitor/config.json");

        // ---- Android ----
        //
        // Android 上的 CWD 既不是应用目录也不是源码目录, 上面的候选
        // 全都落空。这里补上 Qt 的标准配置目录 —— 应用私有目录,
        // 一定可写。
        const QString stdDir = QStandardPaths::writableLocation(
            QStandardPaths::AppConfigLocation);
        if (!stdDir.isEmpty())
            candidates << stdDir + QStringLiteral("/config.json");
    }
    return candidates;
}

AppConfig AppConfig::load(const QString &path)
{
    const QStringList candidates = candidatePaths(path);

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
        if (root.contains(QStringLiteral("agentSessionId")))
            cfg.agentSessionId = root.value(QStringLiteral("agentSessionId")).toString(cfg.agentSessionId);

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
        // 记住读的是哪个文件 —— save() 要写回同一个地方。
        g_loadedPath = candidate;
        return cfg;
    }

    qInfo("[配置] 未找到 config.json, 使用内置默认配置");
    g_loadedPath.clear();
    return defaults();
}

QString AppConfig::loadedPath()
{
    return g_loadedPath;
}

bool AppConfig::save(const AppConfig &cfg, const QString &path)
{
    // ---- 决定写到哪里 ----
    QString target = path;
    if (target.isEmpty())
        target = g_loadedPath;
    if (target.isEmpty()) {
        // 从来没读到过 config.json。
        //
        // 优先写 Qt 的标准配置目录 (Android 上的应用私有目录, 一定可写);
        // 桌面端写当前工作目录。
        const QString stdDir = QStandardPaths::writableLocation(
            QStandardPaths::AppConfigLocation);
        if (!stdDir.isEmpty())
            target = stdDir + QStringLiteral("/config.json");
        else
            target = QDir::currentPath() + QStringLiteral("/config.json");
    }

    // ---- 读出原有内容, 只改 defaultHost ----
    //
    // 整体重写会把用户手改过的端口/路径抹掉, 所以这里保留其余字段。
    QJsonObject root;
    QByteArray raw;
    QFile in(target);
    if (in.exists() && in.open(QIODevice::ReadOnly)) {
        raw = in.readAll();
        in.close();
        QJsonParseError err{};
        const QJsonDocument doc = QJsonDocument::fromJson(raw, &err);
        if (err.error == QJsonParseError::NoError && doc.isObject())
            root = doc.object();
        else
            qWarning("[配置] %s 解析失败, 将只写 defaultHost", qPrintable(target));
    }

    const QString newHost = cfg.defaultHost.trimmed();
    root.insert(QStringLiteral("defaultHost"), newHost);

    // ---- 写回 ----
    //
    // 用 QSaveFile: 先写临时文件再原子替换 —— 写到一半掉电/崩溃时,
    // 原来的 config.json 不会变成半截的坏文件。
    QSaveFile out(target);
    if (!out.open(QIODevice::WriteOnly | QIODevice::Text)) {
        qWarning("[配置] 写入失败 (%s): %s", qPrintable(target),
                 qPrintable(out.errorString()));
        return false;
    }

    // **尽量保持原文件的排版** ——
    //
    // 直接用 QJsonDocument::toJson(Indented) 会把键排序 + 缩进整个换掉,
    // 用户一存 IP, git diff 就满了, 看着像改了一堆东西。
    //
    // 所以先试「就地替换 defaultHost 的值」—— 成功的话原样不动其余部分;
    // 替换不了 (键不存在/格式特殊) 才退回整体重写。
    QByteArray data = replaceHostInJson(raw, newHost);
    if (data.isEmpty())
        data = QJsonDocument(root).toJson(QJsonDocument::Indented);

    if (out.write(data) != data.size() || !out.commit()) {
        qWarning("[配置] 写入失败 (%s): %s", qPrintable(target),
                 qPrintable(out.errorString()));
        return false;
    }

    qInfo("[配置] 已保存 defaultHost=%s -> %s",
          qPrintable(newHost), qPrintable(target));
    return true;
}

QByteArray AppConfig::replaceHostInJson(const QByteArray &json, const QString &host)
{
    /// 在 JSON 里就地替换 "defaultHost" 的值, 保持其余排版不变。
    ///
    /// 做法: 找到 "defaultHost" 这个键, 然后把它后面那个字符串值替换掉。
    /// 替换失败 (找不到键 / 不是字符串值 / 后面不是引号) 返回空 QByteArray ——
    /// 调用方会退回整体重写。
    ///
    /// 之所以不用正则: JSON 字符串里可能有转义引号, 正则容易切错位置。
    /// 这里手工扫一遍, 只认真正的键位置 (前面的引号前面必须是 { 或 ,)。

    const QByteArray needle = "\"defaultHost\"";
    int pos = json.indexOf(needle);
    if (pos < 0)
        return QByteArray();

    // ---- 定位键后面的冒号 ----
    int i = pos + needle.size();
    while (i < json.size() && (json.at(i) == ' ' || json.at(i) == '\t'
                                || json.at(i) == '\n' || json.at(i) == '\r'))
        ++i;
    if (i >= json.size() || json.at(i) != ':')
        return QByteArray();
    ++i;
    while (i < json.size() && (json.at(i) == ' ' || json.at(i) == '\t'
                                || json.at(i) == '\n' || json.at(i) == '\r'))
        ++i;

    // ---- 定位值的引号 ----
    if (i >= json.size() || json.at(i) != '"')
        return QByteArray();
    const int valueStart = i + 1;

    // ---- 找到值的结束引号 (处理转义) ----
    int valueEnd = valueStart;
    while (valueEnd < json.size()) {
        const char c = json.at(valueEnd);
        if (c == '\\') {
            valueEnd += 2;   // 跳过被转义的字符
            continue;
        }
        if (c == '"')
            break;
        ++valueEnd;
    }
    if (valueEnd >= json.size())
        return QByteArray();

    // ---- 拼出新内容 ----
    //
    // 只替换值那一段, 其余字节原样保留 -> 键顺序、缩进、注释风格都不变。
    QByteArray result = json;
    result.replace(valueStart, valueEnd - valueStart, host.toUtf8());

    return result;
}
