#include "ui/flowcard.h"

#include <QFont>
#include <QJsonValue>
#include <QLabel>
#include <QVBoxLayout>

namespace {

/// 小标题 (如「ASR 识别」) —— 灰色等宽, 固定不换行。
QLabel *makeCaption(const QString &text)
{
    auto *label = new QLabel(text);
    label->setObjectName(QStringLiteral("flowCaption"));
    label->setAlignment(Qt::AlignLeft | Qt::AlignTop);
    return label;
}

/// 内容区 —— 可换行, 长文本自动折行显示完整。
QLabel *makeValue(const QString &text)
{
    auto *label = new QLabel(text);
    label->setObjectName(QStringLiteral("flowValue"));
    label->setWordWrap(true);
    label->setTextInteractionFlags(Qt::TextSelectableByMouse);
    label->setAlignment(Qt::AlignLeft | Qt::AlignTop);
    label->setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Minimum);
    return label;
}

}  // namespace

FlowCard::FlowCard(const QDateTime &startedAt, QWidget *parent)
    : QFrame(parent)
{
    setObjectName(QStringLiteral("flowCard"));
    // 横向必须能拉伸 (填满面板), 纵向只跟内容走 ——
    // 纵向也跟着拉伸的话, 卡片头部的行会被抻高, 徽标变成竖条。
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Maximum);

    auto *root = new QVBoxLayout(this);
    root->setContentsMargins(12, 10, 12, 10);
    root->setSpacing(6);

    // ---- 头部: 时间 + 意图徽标 + 阶段 ----
    //
    // 头部要「贴着内容」, 不能被纵向拉伸 —— 拉伸后徽标会变成竖条。
    auto *header = new QHBoxLayout;
    header->setSpacing(8);
    header->setSizeConstraint(QLayout::SetFixedSize);

    m_time = new QLabel(startedAt.toString(QStringLiteral("HH:mm:ss")));
    m_time->setObjectName(QStringLiteral("flowTime"));
    header->addWidget(m_time);

    m_badge = new QLabel;
    m_badge->setObjectName(QStringLiteral("flowBadge"));
    m_badge->setVisible(false);
    // 徽标要「贴着文字」, 不能被拉伸 —— 拉伸后会变成一个竖条
    m_badge->setSizePolicy(QSizePolicy::Fixed, QSizePolicy::Fixed);
    m_badge->setAlignment(Qt::AlignCenter);
    header->addWidget(m_badge);

    header->addStretch(1);

    m_phase = new QLabel(QStringLiteral("进行中"));
    m_phase->setObjectName(QStringLiteral("flowPhase"));
    header->addWidget(m_phase);

    root->addLayout(header);

    // ---- 正文: 四段 ----
    auto *body = new QVBoxLayout;
    body->setSpacing(4);

    // 每段 = 一个小标题 + 一个内容值。
    // 初始没有内容, 整行先隐藏 —— 等真的有内容了再显示出来。
    auto makeRow = [](const QString &caption) {
        Row row;
        row.layout = new QHBoxLayout;
        row.layout->setSpacing(8);
        row.caption = makeCaption(caption);
        row.value = makeValue(QString());
        row.layout->addWidget(row.caption);
        row.layout->addWidget(row.value, 1);
        return row;
    };

    m_asrRow = makeRow(QStringLiteral("ASR 识别"));
    m_routeRow = makeRow(QStringLiteral("Agent 分流"));
    m_replyRow = makeRow(QStringLiteral("Agent 回复"));
    m_timingRow = makeRow(QStringLiteral("流程耗时"));

    body->addLayout(m_asrRow.layout);
    body->addLayout(m_routeRow.layout);
    body->addLayout(m_replyRow.layout);
    body->addLayout(m_timingRow.layout);

    root->addLayout(body);
}

void FlowCard::updateRow(Row &row, const QString &text)
{
    const QString t = text.trimmed();
    row.hasContent = !t.isEmpty();

    if (row.hasContent) {
        row.value->setText(t);
        // 整行显示
        row.caption->setVisible(true);
        row.value->setVisible(true);
    } else {
        row.value->clear();
        // 空白段连标题一起隐藏 —— 不占位置, 也不留「—」占位符
        row.caption->setVisible(false);
        row.value->setVisible(false);
    }
}

void FlowCard::setAsrText(const QString &text)
{
    const QString t = text.trimmed();
    m_asrText = t;
    updateRow(m_asrRow, t);
}

void FlowCard::setRoute(const QString &intent, const QString &by)
{
    QStringList bits;
    if (!intent.isEmpty())
        bits << QStringLiteral("分流 %1").arg(intent);
    if (!by.isEmpty())
        bits << QStringLiteral("判定 %1").arg(by);
    updateRow(m_routeRow, bits.join(QStringLiteral(" · ")));
    setBadge(intent);
}

void FlowCard::setReply(const QString &reply, bool speak, const QString &action)
{
    QString text = reply.trimmed();
    if (text.isEmpty())
        text = QStringLiteral("(无回复文本)");

    QStringList suffix;
    suffix << (speak ? QStringLiteral("已朗读") : QStringLiteral("未朗读"));
    if (!action.isEmpty())
        suffix << QStringLiteral("动作 %1").arg(action);

    updateRow(m_replyRow, QStringLiteral("%1\n%2")
                                 .arg(text, suffix.join(QStringLiteral(" · "))));
    m_complete = true;
}

void FlowCard::setAgentTiming(int totalMs, int routeMs, int handlerMs)
{
    m_agentTotal = totalMs;
    m_agentRoute = routeMs;
    m_agentHandler = handlerMs;
    rebuildTiming();
}

void FlowCard::setAsrTiming(const QJsonObject &timings)
{
    m_asrTimings = timings;
    rebuildTiming();
}

void FlowCard::setPhaseText(const QString &text)
{
    m_phase->setText(text);
}

void FlowCard::setBadge(const QString &intent)
{
    if (intent.isEmpty()) {
        m_badge->setVisible(false);
        return;
    }
    m_badge->setText(intent);
    m_badge->setVisible(true);
}

void FlowCard::rebuildTiming()
{
    QStringList bits;

    // ASR 侧 (来自 /health 的 timings)
    auto addTiming = [this, &bits](const QString &label, const QString &key) {
        const QJsonValue v = m_asrTimings.value(key);
        if (v.isDouble()) {
            bits << QStringLiteral("%1 %2ms").arg(label).arg(v.toInt());
        }
    };
    addTiming(QStringLiteral("录音+识别"), QStringLiteral("asr_record_ms"));
    addTiming(QStringLiteral("Agent 往返"), QStringLiteral("agent_total_ms"));
    addTiming(QStringLiteral("TTS 合成"), QStringLiteral("tts_total_ms"));
    addTiming(QStringLiteral("单轮总计"), QStringLiteral("turn_total_ms"));
    addTiming(QStringLiteral("端到端"), QStringLiteral("end_to_end_ms"));

    // Agent 侧 (来自 WS 事件) —— 只在 ASR 那边没给时补充, 避免重复
    if (m_agentTotal >= 0 && !m_asrTimings.contains(QStringLiteral("turn_total_ms"))) {
        bits << QStringLiteral("Agent 总计 %1ms").arg(m_agentTotal);
    }
    if (m_agentRoute >= 0 && m_agentHandler >= 0) {
        bits << QStringLiteral("分流 %1ms / 执行 %2ms").arg(m_agentRoute).arg(m_agentHandler);
    }

    updateRow(m_timingRow, bits.join(QStringLiteral(" · ")));
}
