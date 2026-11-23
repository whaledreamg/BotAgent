#include "ui/servicebar.h"

#include "models/monitormodel.h"

#include <QHBoxLayout>
#include <QLabel>
#include <QMouseEvent>
#include <QToolTip>

namespace {

/// 状态圆点颜色 —— 与 StatusDelegate 保持一致。
QColor colorForState(int state)
{
    return serviceStateColor(static_cast<ServiceState>(state));
}

QString stateTextFor(int state)
{
    switch (static_cast<ServiceState>(state)) {
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

}  // namespace

ServiceBar::ServiceBar(QWidget *parent)
    : QWidget(parent)
{
    setObjectName(QStringLiteral("serviceBar"));
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Maximum);

    auto *layout = new QHBoxLayout(this);
    layout->setContentsMargins(0, 0, 0, 0);
    layout->setSpacing(8);
    layout->addStretch(1);
}

ServiceBar::~ServiceBar()
{
    qDeleteAll(m_chips);
    m_chips.clear();
}

ServiceBar::Chip *ServiceBar::chip(const QString &name)
{
    return m_chips.value(name, nullptr);
}

bool ServiceBar::eventFilter(QObject *watched, QEvent *event)
{
    if (event->type() == QEvent::MouseButtonRelease) {
        auto *me = static_cast<QMouseEvent *>(event);
        if (me->button() == Qt::LeftButton) {
            for (auto it = m_chips.begin(); it != m_chips.end(); ++it) {
                if (it.value()->frame == watched) {
                    emit serviceClicked(it.key());
                    break;
                }
            }
        }
    }
    return QWidget::eventFilter(watched, event);
}

void ServiceBar::addService(const QString &name, const QString &host, int port)
{
    if (name.isEmpty() || m_chips.contains(name))
        return;

    auto *c = new Chip;
    c->host = host;
    c->port = port;
    c->status = static_cast<int>(ServiceState::Unknown);
    c->detail = QStringLiteral("等待首次探测");

    // ---- 胶囊本体 ----
    c->frame = new QFrame(this);
    c->frame->setObjectName(QStringLiteral("svcChip"));
    c->frame->setCursor(Qt::PointingHandCursor);

    auto *layout = new QHBoxLayout(c->frame);
    layout->setContentsMargins(10, 3, 12, 3);
    layout->setSpacing(6);

    c->dot = new QLabel(QStringLiteral("●"), c->frame);
    c->dot->setObjectName(QStringLiteral("svcDot"));
    layout->addWidget(c->dot);

    c->name = new QLabel(name, c->frame);
    c->name->setObjectName(QStringLiteral("svcName"));
    layout->addWidget(c->name);

    c->state = new QLabel(c->frame);
    c->state->setObjectName(QStringLiteral("svcState"));
    layout->addWidget(c->state);

    // 点击胶囊 -> 弹原始 JSON 详情
    c->frame->installEventFilter(this);

    auto *barLayout = qobject_cast<QHBoxLayout *>(this->layout());
    // 插到末尾的 stretch 之前, 保持左对齐
    barLayout->insertWidget(barLayout->count() - 1, c->frame);

    m_chips.insert(name, c);
    refresh(name);
}

void ServiceBar::setServiceHost(const QString &name, const QString &host, int port)
{
    Chip *c = chip(name);
    if (!c)
        return;
    c->host = host;
    c->port = port;
    refresh(name);
}

void ServiceBar::updateService(const QString &name, int state, const QString &detail,
                               int latencyMs){
    Chip *c = chip(name);
    if (!c)
        return;

    // state 传 -1 表示「只更新详细信息, 不改健康状态」
    if (state >= 0)
        c->status = state;
    if (latencyMs >= 0)
        c->latencyMs = latencyMs;
    if (!detail.isEmpty())
        c->detail = detail;

    refresh(name);
}

void ServiceBar::setMonitoring(bool on)
{
    m_monitoring = on;
    for (auto it = m_chips.begin(); it != m_chips.end(); ++it)
        refresh(it.key());
}

bool ServiceBar::isMonitoring() const
{
    return m_monitoring;
}

void ServiceBar::refresh(const QString &name)
{
    Chip *c = chip(name);
    if (!c)
        return;

    if (!m_monitoring) {
        // 没在监控 —— 全部置灰
        c->dot->setStyleSheet(QStringLiteral("color: #9E9E9E;"));
        c->state->setText(QStringLiteral("未监控"));
        c->state->setStyleSheet(QStringLiteral("color: #8A93A6;"));
        c->frame->setToolTip(
            QStringLiteral("%1 (%2:%3)\n监控未启动 — 点击「开始监控」后开始探测")
                .arg(name, c->host)
                .arg(c->port));
        return;
    }

    c->dot->setStyleSheet(
        QStringLiteral("color: %1;").arg(colorForState(c->status).name()));

    QString text = stateTextFor(c->status);
    if (c->latencyMs >= 0)
        text += QStringLiteral(" %1ms").arg(c->latencyMs);
    c->state->setText(text);
    c->state->setStyleSheet(
        QStringLiteral("color: %1;").arg(colorForState(c->status).darker(130).name()));

    QStringList tip;
    tip << QStringLiteral("%1 (%2:%3)").arg(name, c->host).arg(c->port);
    tip << QStringLiteral("状态: %1").arg(stateTextFor(c->status));
    if (c->latencyMs >= 0)
        tip << QStringLiteral("延迟: %1 ms").arg(c->latencyMs);
    if (!c->detail.isEmpty())
        tip << QStringLiteral("说明: %1").arg(c->detail);
    tip << QStringLiteral("点击查看原始 JSON");
    c->frame->setToolTip(tip.join(QLatin1Char('\n')));
}
