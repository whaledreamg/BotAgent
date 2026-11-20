#pragma once

#include <QDateTime>
#include <QJsonObject>
#include <QList>
#include <QScrollArea>
#include <QWidget>

class FlowCard;
class QLabel;

/// 对话流程面板 —— 一串「流程卡片」。
///
/// 每次对话产生一张卡片, 展示:
///   ASR 识别到什么 -> Agent 怎么分流 -> Agent 回了什么 -> 各阶段耗时
///
/// **刻意不用 QAbstractItemModel + QTableView** ——
/// 表格按列排布, 长文本会被截断/省略, 也看不到一条完整的流程。
/// 这里用自绘卡片 + 竖排滚动, 每条流程是一个整体, 文字完整换行显示。
///
/// 面板结构 (从上到下):
///   1. 流水线状态条 —— 实时显示 ASR 状态机 (IDLE / REC / THINK / SAY)
///   2. 流程卡片区 —— 最新的对话在最上面
///   3. 系统事件区 —— Arm / Vision 这类不属于对话流程的事件
class FlowPanel : public QScrollArea
{
    Q_OBJECT

public:
    explicit FlowPanel(QWidget *parent = nullptr);

    /// 一轮对话开始 —— 新建卡片。
    FlowCard *beginTurn(const QDateTime &at);
    /// 当前正在进行的卡片; 没有返回 nullptr。
    FlowCard *currentCard() const;

    /// 更新顶部流水线状态条 (如 "THINK")。
    void setPipelineState(const QString &state, const QString &hint = QString());
    /// 追加一条系统事件 (Arm / Vision / WS 错误等)。
    void appendSystemEvent(const QString &source, const QString &text);

    /// 清空所有卡片与事件。
    void clearCards();
    /// 对话卡片数量。
    int cardCount() const;

private:
    QWidget *m_container = nullptr;
    QWidget *m_cardBox = nullptr;   ///< 卡片容器 (卡片加在这里)
    QLabel *m_stateValue = nullptr;
    QLabel *m_stateHint = nullptr;
    QWidget *m_eventBox = nullptr;
    QList<FlowCard *> m_cards;
    QList<QLabel *> m_events;
    FlowCard *m_current = nullptr;
    int m_maxCards = 50;
    int m_maxEvents = 200;
};
