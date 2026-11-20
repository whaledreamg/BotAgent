#pragma once

#include <QAbstractTableModel>
#include <QJsonObject>
#include <QToolTip>
#include <QVector>

/// 服务连接状态。
enum class ServiceState {
    Unknown,    ///< 还没探过
    Online,     ///< 连上了
    Degraded,   ///< 连上了但服务自报异常 (如 TTS 还在 loading)
    Offline,    ///< 连不上
};

/// 单个服务的监控快照。
struct ServiceStatus {
    QString name;
    QString host;
    int port = 0;

    ServiceState state = ServiceState::Unknown;
    /// 本次探测的往返耗时 (毫秒)
    int latencyMs = -1;
    /// 一句话说明 (如 "model_loaded=true" 或 "连接被拒绝")
    QString detail;
    /// 服务自报的状态字段 (原样)
    QString rawStatus;
    /// 详细状态接口的原始 JSON
    QJsonObject statusJson;
    /// 最近一次错误
    QString lastError;

    QString stateText() const;
};

/// 服务状态表模型。
///
/// 列: 名称 | 状态 | 端口 | 延迟 | 说明
class MonitorModel : public QAbstractTableModel {
    Q_OBJECT

public:
    enum Column {
        ColName = 0,
        ColState,
        ColPort,
        ColLatency,
        ColDetail,
        ColCount,
    };

    explicit MonitorModel(QObject *parent = nullptr);

    int rowCount(const QModelIndex &parent = QModelIndex()) const override;
    int columnCount(const QModelIndex &parent = QModelIndex()) const override;
    QVariant data(const QModelIndex &index, int role = Qt::DisplayRole) const override;
    QVariant headerData(int section, Qt::Orientation orientation,
                        int role = Qt::DisplayRole) const override;
    Qt::ItemFlags flags(const QModelIndex &index) const override;
    /// 单元格的详细说明 (通过 Qt::ToolTipRole 暴露)。
    QString tooltipText(const QModelIndex &index) const;

    /// 按服务名取状态; 找不到返回 nullptr。
    const ServiceStatus *status(const QString &name) const;

public slots:
    /// 先把服务登记进来 (状态未知), 之后的探测结果更新到对应行。
    void addService(const QString &name, const QString &host, int port);
    /// 更新某个服务的状态 (找不到就忽略)。
    ///
    /// ``state`` 取 MonitorModel::ServiceState 的枚举值; 传 -1 表示
    /// 「只更新详细信息, 不改健康状态」—— 状态接口的回复走这条路。
    void updateStatus(const QString &name, int state, int latencyMs,
                      const QString &detail, const QString &rawStatus,
                      const QJsonObject &statusJson);
    /// 记录一次探测失败 (连接错误/超时)。
    void markFailure(const QString &name, const QString &error);

private:
    int rowOf(const QString &name) const;

    QVector<ServiceStatus> m_rows;
};
