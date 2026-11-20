#include "ui/flowpanel.h"

#include "ui/flowcard.h"

#include <QDateTime>
#include <QLabel>
#include <QScrollBar>
#include <QVBoxLayout>

FlowPanel::FlowPanel(QWidget *parent)
    : QScrollArea(parent)
{
    setObjectName(QStringLiteral("flowPanel"));
    setWidgetResizable(true);
    setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);

    m_container = new QWidget;
    m_container->setObjectName(QStringLiteral("flowContainer"));

    auto *layout = new QVBoxLayout(m_container);
    layout->setContentsMargins(8, 8, 8, 8);
    layout->setSpacing(8);

    // ---- 顶部: 流水线状态条 ----
    auto *stateBar = new QFrame;
    stateBar->setObjectName(QStringLiteral("flowStateBar"));
    auto *stateLayout = new QHBoxLayout(stateBar);
    stateLayout->setContentsMargins(10, 6, 10, 6);
    stateLayout->setSpacing(8);

    auto *stateCaption = new QLabel(QStringLiteral("流水线状态"));
    stateCaption->setObjectName(QStringLiteral("flowCaption"));
    stateLayout->addWidget(stateCaption);

    m_stateValue = new QLabel(QStringLiteral("—"));
    m_stateValue->setObjectName(QStringLiteral("flowStateValue"));
    stateLayout->addWidget(m_stateValue);

    m_stateHint = new QLabel;
    m_stateHint->setObjectName(QStringLiteral("flowStateHint"));
    stateLayout->addWidget(m_stateHint, 1);

    layout->addWidget(stateBar);

    // ---- 中间: 流程卡片区 ----
    //
    // 卡片加在一个**独立的容器**里, 而不是直接塞进主布局。
    //
    // 关键: 容器必须 **横向填满** 滚动区域。QScrollArea 的 widget 宽度
    // 取决于 widget 的 sizeHint, 如果容器不拉伸, 里面的卡片会按自己的
    // 宽度排版 —— 表现为卡片挤在右边、超出可视区被裁掉。
    //
    // 注意: 这里**不能给垂直方向的 stretch factor**。加 stretch(1) 会让
    // 容器在纵向也撑满, 卡片被强行拉高 —— 卡片头部的行会被拉伸, 徽标
    // 变成一个竖条。横向填充靠 QSizePolicy::Expanding 实现, 不需要 stretch。
    m_cardBox = new QWidget;
    m_cardBox->setObjectName(QStringLiteral("flowCardBox"));
    m_cardBox->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Maximum);
    auto *cardLayout = new QVBoxLayout(m_cardBox);
    cardLayout->setContentsMargins(0, 0, 0, 0);
    cardLayout->setSpacing(8);

    layout->addWidget(m_cardBox);

    // ---- 弹性空白: 把系统事件顶到底部 ----
    layout->addStretch(1);

    // ---- 底部: 系统事件 ----
    m_eventBox = new QWidget;
    m_eventBox->setObjectName(QStringLiteral("flowEventBox"));
    auto *eventLayout = new QVBoxLayout(m_eventBox);
    eventLayout->setContentsMargins(0, 0, 0, 0);
    eventLayout->setSpacing(2);

    layout->addWidget(m_eventBox);

    setWidget(m_container);
}

FlowCard *FlowPanel::beginTurn(const QDateTime &at)
{
    auto *card = new FlowCard(at, m_cardBox);

    // 插到卡片区最上面 (最新的对话在最前)
    auto *layout = qobject_cast<QVBoxLayout *>(m_cardBox->layout());
    layout->insertWidget(0, card, 1);

    m_cards.append(card);
    m_current = card;

    // 卡片太多就丢最旧的
    while (m_cards.size() > m_maxCards) {
        FlowCard *old = m_cards.takeFirst();
        old->deleteLater();
    }

    // 新卡片出现时滚回顶部 —— 最新的一轮在最上面, 不跟着滚走
    QMetaObject::invokeMethod(
        this, [this] { verticalScrollBar()->setValue(0); }, Qt::QueuedConnection);

    return card;
}

FlowCard *FlowPanel::currentCard() const
{
    return m_current;
}

void FlowPanel::setPipelineState(const QString &state, const QString &hint)
{
    m_stateValue->setText(state.isEmpty() ? QStringLiteral("—") : state);
    m_stateHint->setText(hint);
}

void FlowPanel::appendSystemEvent(const QString &source, const QString &text)
{
    if (text.trimmed().isEmpty())
        return;

    auto *label = new QLabel(QStringLiteral("[%1] %2").arg(source, text));
    label->setObjectName(QStringLiteral("flowSystemEvent"));
    label->setWordWrap(true);
    label->setTextInteractionFlags(Qt::TextSelectableByMouse);

    auto *eventLayout = qobject_cast<QVBoxLayout *>(m_eventBox->layout());
    eventLayout->addWidget(label);
    m_events.append(label);

    while (m_events.size() > m_maxEvents) {
        QLabel *old = m_events.takeFirst();
        old->deleteLater();
    }
}

void FlowPanel::clearCards()
{
    for (FlowCard *card : m_cards)
        card->deleteLater();
    m_cards.clear();
    m_current = nullptr;

    for (QLabel *label : m_events)
        label->deleteLater();
    m_events.clear();
}

int FlowPanel::cardCount() const
{
    return m_cards.size();
}
