#pragma once

#include <QAbstractItemModel>
#include <QHash>
#include <QJsonObject>
#include <QToolTip>
#include <QVector>

class ThinkingNode;

/// 思考过程树里的一个节点。
///
/// 用堆分配 + 父子指针 —— QVector 扩容会搬元素, 用下标当内部指针
/// 会失效, 所以节点必须是稳定地址。
struct ThinkingNode {
    enum Kind {
        Turn,      ///< 一轮对话 (顶层)
        Section,   ///< 阶段 (分流 / 工具调用 / 参数提取 / 结果 / 耗时)
        Field,     ///< 字段 (推理文本 / 置信度 / 参数 ...)
    };

    Kind kind = Section;
    QString title;
    QString value;
    QString tooltip;
    QVector<ThinkingNode *> children;
    ThinkingNode *parent = nullptr;

    ~ThinkingNode()
    {
        qDeleteAll(children);
    }

    /// 在子节点里按标题找; 找不到返回 nullptr。
    ThinkingNode *findChild(const QString &childTitle) const
    {
        for (ThinkingNode *c : children) {
            if (c->title == childTitle)
                return c;
        }
        return nullptr;
    }
};

/// Agent 思考过程模型 (树形)。
///
/// 结构:
///   ▸ 第 3 轮 「查南京天气」
///       ├ 分流          tool:天气 (规则命中, 2ms)
///       ├ 工具调用      模型选了 query_weather
///       │   └ 参数      {"city": "南京"}
///       ├ 参数提取      weather
///       │   ├ 推理      识别到'南京'和'天气'关键词
///       │   ├ 置信度    0.98
///       │   └ 参数      {"city": "南京"}
///       ├ 结果          南京今天晴，气温 25 度
///       └ 耗时          总计 1121ms
///
/// 数据来自 Agent 的 WebSocket 事件:
///   turn_start / thinking / turn_end / error
class ThinkingModel : public QAbstractItemModel {
    Q_OBJECT

public:
    enum Role {
        TitleRole = Qt::UserRole + 1,
        ValueRole,
        TooltipRole,
        KindRole,
    };

    explicit ThinkingModel(QObject *parent = nullptr);
    ~ThinkingModel() override;

    QModelIndex index(int row, int column,
                      const QModelIndex &parent = QModelIndex()) const override;
    QModelIndex parent(const QModelIndex &child) const override;
    int rowCount(const QModelIndex &parent = QModelIndex()) const override;
    int columnCount(const QModelIndex &parent = QModelIndex()) const override;
    QVariant data(const QModelIndex &index, int role = Qt::DisplayRole) const override;
    QVariant headerData(int section, Qt::Orientation orientation,
                        int role = Qt::DisplayRole) const override;
    /// 单元格的详细说明 (通过 Qt::ToolTipRole 暴露)。
    QString tooltipText(const QModelIndex &index) const;

    /// 已完成的轮次数 (从 1 开始); 0 表示还没有。
    int turnCount() const { return m_turnCounter; }

public slots:
    /// 处理一条 Agent 事件 (turn_start / thinking / turn_end / error)。
    void handleEvent(const QJsonObject &event);
    /// 清空整棵树。
    void clear();

private:
    /// 找到 (或新建) 当前轮次节点。
    ///
    /// 分轮依据 (优先级从高到低):
    ///   1. `turn_id` —— 后端每轮对话生成的唯一 id, 最可靠;
    ///   2. `turn_start` 事件 —— 分轮的权威边界;
    ///   3. `session_id` —— **不能单独用**, 它是常驻的会话标识
    ///      (比如 robot-01), 用户说一百句话也不变, 拿来分轮会让
    ///      所有轮次塌成第一轮。
    ThinkingNode *ensureTurn(const QString &sessionId, const QString &text,
                             const QString &turnId = QString(),
                             bool forceNew = false);
    /// 在 turn 下找到 (或新建) 一个阶段节点。
    ThinkingNode *ensureSection(ThinkingNode *turn, const QString &title);
    /// 在 section 下找到 (或新建) 一个字段节点。
    ThinkingNode *ensureField(ThinkingNode *section, const QString &title);

    /// 在 turn 下插入一个阶段, 走正常的 begin/endInsertRows。
    void insertSection(ThinkingNode *turn, const QString &title,
                       const QString &value, const QString &tooltip);
    /// 在 turn 下**追加**一个阶段 (同名的也不覆盖, 改成「标题 2」「标题 3」)。
    ///
    /// 同一轮里可能多次触发同一个阶段 (比如连续两次参数提取), 这时
    /// 不能覆盖前一个 —— 得都留着。
    ThinkingNode *appendSection(ThinkingNode *turn, const QString &baseTitle,
                                const QString &value, const QString &tooltip);
    /// 在 section 下插入一个字段。
    void insertField(ThinkingNode *section, const QString &title,
                     const QString &value, const QString &tooltip);

    /// 节点在父节点 children 里的行号; -1 表示它是根。
    int rowOf(ThinkingNode *node) const;

    QVector<ThinkingNode *> m_roots;
    int m_turnCounter = 0;
    /// 当前轮次节点
    ThinkingNode *m_currentTurn = nullptr;
    /// 当前轮次对应的 session id
    QString m_currentSession;
    /// 当前轮次对应的 turn id (后端每轮对话的唯一标识)
    QString m_currentTurnId;
    /// turn_id -> 轮次节点。
    ///
    /// 事件不保证严格按轮次顺序到达 (比如上一轮的 thinking 迟到了),
    /// 靠这张表能把迟到的���件挂回它本该属于的那一轮, 而不是新开一轮。
    QHash<QString, ThinkingNode *> m_turnById;
};
