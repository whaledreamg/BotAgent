#pragma once

#include <QDateTime>
#include <QFrame>
#include <QJsonObject>
#include <QLayout>
#include <QString>

class QLabel;

/// 一轮对话的「流程卡片」。
///
/// 固定四段:
///   1. ASR 识别 —— 语音转出来的原文
///   2. Agent 分流 —— 走了哪个分支、怎么判的
///   3. Agent 回复 —— 最终返回给用户的话
///   4. 流程耗时 —— 各阶段分别花了多久
///
/// 每段的值都是 **可换行** 的 QLabel, 长文本不会被截断;
/// 也不设固定列宽, 卡片高度随内容增长。
///
/// **空白段不显示**: 还没拿到内容的段 (显示为「—」的) 连标题一起隐藏。
/// 对话刚开始时卡片只有「ASR 识别」一行, 随流程推进逐段出现。
class FlowCard : public QFrame
{
    Q_OBJECT

public:
    explicit FlowCard(const QDateTime &startedAt, QWidget *parent = nullptr);

    /// ASR 识别到的文本。
    void setAsrText(const QString &text);
    /// 当前卡片对应的 ASR 文本 (用于判断「是不是新的一轮」)。
    QString asrText() const { return m_asrText; }
    /// 本轮是否已经结束 (收到回复)。
    bool isComplete() const { return m_complete; }
    /// 标记这一轮是**手动输入**的 (不是 ASR 语音)。
    ///
    /// 效果:
    ///   - 正文第一行的标题从「ASR 识别」改成「输入文本」
    ///   - 卡片头部多一个「手动」徽标
    void setManual(bool manual);
    /// 本轮是否是手动输入的。
    bool isManual() const { return m_manual; }
    /// 卡片当前的阶段文案 (如「发送中」「已完成」)。
    QString phaseText() const;
    /// 设置卡片当前的阶段文案。
    void setPhaseText(const QString &text);
    /// Agent 的分流结论 (intent + 判定方式)。
    void setRoute(const QString &intent, const QString &by);
    /// Agent 返回的回复内容。
    void setReply(const QString &reply, bool speak, const QString &action);
    /// Agent 侧的耗时 (来自 WS 事件)。
    void setAgentTiming(int totalMs, int routeMs, int handlerMs);
    /// ASR 侧的耗时 (来自 /health 快照的 timings 字段)。
    void setAsrTiming(const QJsonObject &timings);
    /// 意图徽标 (空则隐藏)。
    void setBadge(const QString &intent);

private:
    /// 一行 = 小标题 + 内容值。
    struct Row {
        QHBoxLayout *layout = nullptr;
        QLabel *caption = nullptr;
        QLabel *value = nullptr;
        bool hasContent = false;
    };

    /// 更新某一行的显示状态 —— 有内容才显示, 没内容整行隐藏。
    void updateRow(Row &row, const QString &text);
    /// 重新拼装耗时行。
    void rebuildTiming();

    Row m_asrRow;
    Row m_routeRow;
    Row m_replyRow;
    Row m_timingRow;

    QLabel *m_time = nullptr;
    QLabel *m_badge = nullptr;
    QLabel *m_phase = nullptr;
    /// 「手动」徽标 —— 只在手动输入的轮次显示。
    QLabel *m_manualBadge = nullptr;

    // 原始数值, 用于重新拼装耗时行
    int m_agentTotal = -1;
    int m_agentRoute = -1;
    int m_agentHandler = -1;
    QJsonObject m_asrTimings;

    /// 本轮对应的 ASR 文本
    QString m_asrText;
    /// 本轮是否已经拿到回复
    bool m_complete = false;
    /// 本轮是否是手动输入的 (不是 ASR 语音)
    bool m_manual = false;
};
