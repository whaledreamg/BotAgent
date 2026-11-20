#pragma once

#include <QEvent>
#include <QHash>
#include <QString>
#include <QWidget>

class QFrame;
class QLabel;

/// 底部服务状态条 —— 一排「圆点 + 名称 + 状态」的小胶囊。
///
/// 每个胶囊代表一个被监控的服务:
///   ● Agent  正常  12ms
///
/// 胶囊可以被点击 (serviceClicked) —— 用来看服务的原始 JSON 详情。
///
/// 监控没启动时, 全部置灰 (setMonitoring(false))。
class ServiceBar : public QWidget
{
    Q_OBJECT

public:
    explicit ServiceBar(QWidget *parent = nullptr);
    ~ServiceBar() override;

    /// 登记一个服务 (重复登记会忽略)。
    void addService(const QString &name, const QString &host, int port);

    /// 更新某个服务的地址 (悬停提示里显示的 host:port)。
    void setServiceHost(const QString &name, const QString &host, int port);

    /// 更新某个服务的状态。
    ///
    /// ``state`` 取 MonitorModel::ServiceState 的枚举值
    /// (0=未知 1=正常 2=降级 3=断开); 传 -1 表示「只更新延迟/说明,
    /// 不改健康状态」。
    void updateService(const QString &name, int state, const QString &detail,
                       int latencyMs);

    /// 监控整体开关 —— 关闭时所有胶囊置灰, 提示「未监控」。
    void setMonitoring(bool on);
    bool isMonitoring() const;

signals:
    /// 用户点击了某个胶囊。
    void serviceClicked(const QString &name);

protected:
    /// 点击胶囊 -> 发 serviceClicked。
    bool eventFilter(QObject *watched, QEvent *event) override;

private:
    /// 一个胶囊的部件。
    struct Chip {
        QFrame *frame = nullptr;
        QLabel *dot = nullptr;
        QLabel *name = nullptr;
        QLabel *state = nullptr;
        QString host;
        int port = 0;
        int status = 0;
        QString detail;
        int latencyMs = -1;
    };

    Chip *chip(const QString &name);
    /// 重新绘制某个胶囊 (颜色 + 文案 + tooltip)。
    void refresh(const QString &name);

    QHash<QString, Chip *> m_chips;
    bool m_monitoring = false;
};
