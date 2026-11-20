#include "monitormodel.h"

#include <QJsonDocument>

QString ServiceStatus::stateText() const
{
    switch (state) {
    case ServiceState::Online:
        return QStringLiteral("正常");
    case ServiceState::Degraded:
        return QStringLiteral("降级");
    case ServiceState::Offline:
        return QStringLiteral("断开");
    case ServiceState::Unknown:
    default:
        return QStringLiteral("未知");
    }
}

MonitorModel::MonitorModel(QObject *parent)
    : QAbstractTableModel(parent)
{
}

int MonitorModel::rowCount(const QModelIndex &parent) const
{
    if (parent.isValid())
        return 0;
    return m_rows.size();
}

int MonitorModel::columnCount(const QModelIndex &parent) const
{
    if (parent.isValid())
        return 0;
    return ColCount;
}

QVariant MonitorModel::data(const QModelIndex &index, int role) const
{
    if (!index.isValid() || index.row() < 0 || index.row() >= m_rows.size())
        return QVariant();

    const ServiceStatus &s = m_rows.at(index.row());

    switch (role) {
    case Qt::DisplayRole:
        switch (index.column()) {
        case ColName:
            return s.name;
        case ColState:
            return s.stateText();
        case ColPort:
            return s.port;
        case ColLatency:
            return s.latencyMs >= 0 ? QStringLiteral("%1 ms").arg(s.latencyMs) : QStringLiteral("-");
        case ColDetail:
            return s.detail;
        default:
            break;
        }
        return QVariant();

    case Qt::ToolTipRole:
        return tooltipText(index);

    case Qt::TextAlignmentRole:
        if (index.column() == ColPort)
            return QVariant(Qt::AlignCenter);
        if (index.column() == ColLatency)
            return QVariant(Qt::AlignRight | Qt::AlignVCenter);
        return QVariant(Qt::AlignLeft | Qt::AlignVCenter);

    case Qt::UserRole:
        // 状态列把枚举值交给 StatusDelegate 画圆点
        if (index.column() == ColState)
            return QVariant(static_cast<int>(s.state));
        return QVariant();

    default:
        break;
    }
    return QVariant();
}

QString MonitorModel::tooltipText(const QModelIndex &index) const
{
    if (!index.isValid() || index.row() < 0 || index.row() >= m_rows.size())
        return QString();

    const ServiceStatus &s = m_rows.at(index.row());
    QStringList lines;
    lines << QStringLiteral("%1  (%2:%3)").arg(s.name, s.host).arg(s.port);
    lines << QStringLiteral("状态: %1").arg(s.stateText());
    if (s.latencyMs >= 0)
        lines << QStringLiteral("延迟: %1 ms").arg(s.latencyMs);
    if (!s.detail.isEmpty())
        lines << QStringLiteral("说明: %1").arg(s.detail);
    if (!s.lastError.isEmpty())
        lines << QStringLiteral("错误: %1").arg(s.lastError);
    if (!s.statusJson.isEmpty()) {
        lines << QStringLiteral("--- 原始状态 ---");
        lines << QString::fromUtf8(
            QJsonDocument(s.statusJson).toJson(QJsonDocument::Indented));
    }
    return lines.join(QLatin1Char('\n'));
}

QVariant MonitorModel::headerData(int section, Qt::Orientation orientation,
                                  int role) const
{
    if (orientation != Qt::Horizontal || role != Qt::DisplayRole)
        return QVariant();

    switch (section) {
    case ColName:
        return QStringLiteral("服务");
    case ColState:
        return QStringLiteral("状态");
    case ColPort:
        return QStringLiteral("端口");
    case ColLatency:
        return QStringLiteral("延迟");
    case ColDetail:
        return QStringLiteral("说明");
    default:
        break;
    }
    return QVariant();
}

Qt::ItemFlags MonitorModel::flags(const QModelIndex &index) const
{
    if (!index.isValid())
        return Qt::NoItemFlags;
    return Qt::ItemIsEnabled | Qt::ItemIsSelectable;
}

const ServiceStatus *MonitorModel::status(const QString &name) const
{
    for (const ServiceStatus &s : m_rows) {
        if (s.name == name)
            return &s;
    }
    return nullptr;
}

int MonitorModel::rowOf(const QString &name) const
{
    for (int i = 0; i < m_rows.size(); ++i) {
        if (m_rows.at(i).name == name)
            return i;
    }
    return -1;
}

void MonitorModel::addService(const QString &name, const QString &host, int port)
{
    if (rowOf(name) >= 0)
        return;

    ServiceStatus s;
    s.name = name;
    s.host = host;
    s.port = port;
    s.state = ServiceState::Unknown;
    s.detail = QStringLiteral("等待首次探测");

    beginInsertRows(QModelIndex(), m_rows.size(), m_rows.size());
    m_rows.append(s);
    endInsertRows();
}

void MonitorModel::updateStatus(const QString &name, int state,
                                int latencyMs, const QString &detail,
                                const QString &rawStatus,
                                const QJsonObject &statusJson)
{
    int row = rowOf(name);
    if (row < 0)
        return;

    {
        ServiceStatus &s = m_rows[row];
        // state 传 -1 表示「只更新详细信息, 不改健康状态」——
        // 状态接口的回复走这条路 (健康状态由健康接口决定)。
        if (state >= 0)
            s.state = static_cast<ServiceState>(state);
        if (latencyMs >= 0)
            s.latencyMs = latencyMs;
        if (!detail.isEmpty())
            s.detail = detail;
        if (!rawStatus.isEmpty())
            s.rawStatus = rawStatus;
        if (!statusJson.isEmpty())
            s.statusJson = statusJson;
        if (state >= 0)
            s.lastError.clear();
    }

    const QModelIndex topLeft = index(row, 0);
    const QModelIndex bottomRight = index(row, ColCount - 1);
    emit dataChanged(topLeft, bottomRight);
}

void MonitorModel::markFailure(const QString &name, const QString &error)
{
    int row = rowOf(name);
    if (row < 0)
        return;

    {
        ServiceStatus &s = m_rows[row];
        s.state = ServiceState::Offline;
        s.latencyMs = -1;
        s.detail = error;
        s.lastError = error;
    }

    const QModelIndex topLeft = index(row, 0);
    const QModelIndex bottomRight = index(row, ColCount - 1);
    emit dataChanged(topLeft, bottomRight);
}
