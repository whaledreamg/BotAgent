#include "thinkingmodel.h"

#include <QDateTime>
#include <QJsonArray>
#include <QJsonDocument>

namespace {

/// 取 ISO 时间串里的时分秒 (显示用)。
QString shortTime(const QString &iso)
{
    if (iso.isEmpty())
        return QString();
    const QDateTime dt = QDateTime::fromString(iso, Qt::ISODateWithMs);
    if (!dt.isValid())
        return iso;
    return dt.toString(QStringLiteral("HH:mm:ss.zzz"));
}

/// 对象转紧凑 JSON (一行)。
QString compactJson(const QJsonObject &obj)
{
    if (obj.isEmpty())
        return QStringLiteral("{}");
    return QString::fromUtf8(QJsonDocument(obj).toJson(QJsonDocument::Compact));
}

}  // namespace

ThinkingModel::ThinkingModel(QObject *parent)
    : QAbstractItemModel(parent)
{
}

ThinkingModel::~ThinkingModel()
{
    qDeleteAll(m_roots);
    m_roots.clear();
}

QModelIndex ThinkingModel::index(int row, int column,
                                 const QModelIndex &parent) const
{
    if (!hasIndex(row, column, parent))
        return QModelIndex();

    if (!parent.isValid()) {
        if (row < 0 || row >= m_roots.size())
            return QModelIndex();
        return createIndex(row, column, m_roots.at(row));
    }

    const auto *node = static_cast<ThinkingNode *>(parent.internalPointer());
    if (!node || row < 0 || row >= node->children.size())
        return QModelIndex();
    return createIndex(row, column, node->children.at(row));
}

QModelIndex ThinkingModel::parent(const QModelIndex &child) const
{
    if (!child.isValid())
        return QModelIndex();

    auto *node = static_cast<ThinkingNode *>(child.internalPointer());
    if (!node || !node->parent)
        return QModelIndex();

    const int row = rowOf(node->parent);
    if (row < 0)
        return QModelIndex();
    return createIndex(row, 0, node->parent);
}

int ThinkingModel::rowOf(ThinkingNode *node) const
{
    if (!node)
        return -1;
    if (!node->parent)
        return m_roots.indexOf(node);
    return node->parent->children.indexOf(node);
}

int ThinkingModel::rowCount(const QModelIndex &parent) const
{
    if (!parent.isValid())
        return m_roots.size();

    const auto *node = static_cast<ThinkingNode *>(parent.internalPointer());
    return node ? node->children.size() : 0;
}

int ThinkingModel::columnCount(const QModelIndex &parent) const
{
    Q_UNUSED(parent);
    return 2;   ///< 阶段 | 值
}

QVariant ThinkingModel::data(const QModelIndex &index, int role) const
{
    if (!index.isValid())
        return QVariant();

    const auto *node = static_cast<ThinkingNode *>(index.internalPointer());
    if (!node)
        return QVariant();

    switch (role) {
    case Qt::DisplayRole:
        return index.column() == 0 ? node->title : node->value;

    case TitleRole:
        return node->title;

    case ValueRole:
        return node->value;

    case TooltipRole:
        return node->tooltip;

    case Qt::ToolTipRole:
        // 标准角色 —— QTreeView 悬停时自动弹出完整原文。
        // 树里显示的是单行省略的内容 (推理、模型输出、参数 JSON...),
        // 完整版都放在 tooltip 里。
        return tooltipText(index);

    case KindRole:
        return static_cast<int>(node->kind);

    case Qt::TextAlignmentRole:
        return QVariant(Qt::AlignLeft | Qt::AlignVCenter);

    default:
        break;
    }
    return QVariant();
}

QVariant ThinkingModel::headerData(int section, Qt::Orientation orientation,
                                   int role) const
{
    if (orientation != Qt::Horizontal || role != Qt::DisplayRole)
        return QVariant();
    return section == 0 ? QStringLiteral("思考阶段") : QStringLiteral("内容");
}

QString ThinkingModel::tooltipText(const QModelIndex &index) const
{
    if (!index.isValid())
        return QString();
    const auto *node = static_cast<ThinkingNode *>(index.internalPointer());
    if (!node)
        return QString();
    return node->tooltip.isEmpty() ? node->value : node->tooltip;
}

void ThinkingModel::clear()
{
    if (m_roots.isEmpty())
        return;
    beginResetModel();
    qDeleteAll(m_roots);
    m_roots.clear();
    m_currentTurn = nullptr;
    m_currentSession.clear();
    m_currentTurnId.clear();
    m_turnById.clear();
    m_turnCounter = 0;
    endResetModel();
}

ThinkingNode *ThinkingModel::ensureTurn(const QString &sessionId, const QString &text,
                                         const QString &turnId, bool forceNew)
{
    // ---- 分轮判定 ----
    //
    // 1. 带 turn_id 时: 同一个 turn_id 就是同一轮 (后端保证每轮一个)
    // 2. 没带 turn_id: 靠 turn_start 的边界分轮 (forceNew)
    // 3. 都不行才退回 session_id —— 但 session_id 是常驻的
    //    (比如 robot-01), 拿来分轮会让所有轮次塌成第一轮, 所以
    //    只在没有 turn_start 的场景下作为最后兜底。
    const QString incomingTurnId = turnId.trimmed();

    if (!forceNew && m_currentTurn) {
        if (!incomingTurnId.isEmpty()) {
            if (m_currentTurnId == incomingTurnId)
                return m_currentTurn;
        } else if (m_currentSession == sessionId) {
            // 兜底: 没有 turn_id 时的老协议
            return m_currentTurn;
        }
    }

    // 带 turn_id 但不是当前轮 —— 可能是**迟到的**事件 (上一轮的
    // thinking 在下一轮 turn_start 之后才到)。这种情况要挂回它
    // 本该属于的那一轮, 而不是新开一轮。
    if (!forceNew && !incomingTurnId.isEmpty()) {
        auto it = m_turnById.constFind(incomingTurnId);
        if (it != m_turnById.constEnd() && it.value()) {
            m_currentTurn = it.value();
            m_currentTurnId = incomingTurnId;
            return it.value();
        }
    }

    ++m_turnCounter;

    auto *node = new ThinkingNode();
    node->kind = ThinkingNode::Turn;
    node->title = QStringLiteral("第 %1 轮").arg(m_turnCounter);
    node->value = text;

    // ---- 倒叙: 最新的一轮插在最上面 ----
    //
    // 树顶永远是最新的那一轮, 往下翻是更早的对话。
    // (轮次编号仍然按时间递增 —— 「第 3 轮」在「第 1 轮」上面)
    const QModelIndex parentIndex;
    beginInsertRows(parentIndex, 0, 0);
    node->parent = nullptr;
    m_roots.prepend(node);
    endInsertRows();

    m_currentTurn = node;
    m_currentSession = sessionId;
    m_currentTurnId = incomingTurnId;
    if (!incomingTurnId.isEmpty())
        m_turnById.insert(incomingTurnId, node);
    return node;
}

ThinkingNode *ThinkingModel::ensureSection(ThinkingNode *turn, const QString &title)
{
    if (!turn)
        return nullptr;
    return turn->findChild(title);
}

ThinkingNode *ThinkingModel::ensureField(ThinkingNode *section, const QString &title)
{
    if (!section)
        return nullptr;
    return section->findChild(title);
}

void ThinkingModel::insertSection(ThinkingNode *turn, const QString &title,
                                  const QString &value, const QString &tooltip)
{
    if (!turn)
        return;
    const QModelIndex turnIndex = index(rowOf(turn), 0);
    beginInsertRows(turnIndex, turn->children.size(), turn->children.size());
    auto *node = new ThinkingNode();
    node->kind = ThinkingNode::Section;
    node->title = title;
    node->value = value;
    node->tooltip = tooltip;
    node->parent = turn;
    turn->children.append(node);
    endInsertRows();
}

ThinkingNode *ThinkingModel::appendSection(ThinkingNode *turn, const QString &baseTitle,
                                           const QString &value, const QString &tooltip)
{
    if (!turn)
        return nullptr;

    // 同名的阶段可能有好几个 (比如一轮里提取了两次参数) ——
    // 后面的用序号区分: 「参数提取」「参数提取 2」「参数提取 3」
    QString title = baseTitle;
    int seq = 2;
    while (turn->findChild(title)) {
        title = QStringLiteral("%1 %2").arg(baseTitle).arg(seq++);
    }

    insertSection(turn, title, value, tooltip);
    return turn->findChild(title);
}

void ThinkingModel::insertField(ThinkingNode *section, const QString &title,
                                const QString &value, const QString &tooltip)
{    if (!section)
        return;
    const QModelIndex sectionIndex = index(rowOf(section), 0, index(rowOf(section->parent), 0));
    beginInsertRows(sectionIndex, section->children.size(), section->children.size());
    auto *node = new ThinkingNode();
    node->kind = ThinkingNode::Field;
    node->title = title;
    node->value = value;
    node->tooltip = tooltip;
    node->parent = section;
    section->children.append(node);
    endInsertRows();
}

void ThinkingModel::handleEvent(const QJsonObject &event)
{
    const QString type = event.value(QStringLiteral("type")).toString();
    const QString sessionId = event.value(QStringLiteral("session_id")).toString();
    const QString turnId = event.value(QStringLiteral("turn_id")).toString();
    const QString ts = event.value(QStringLiteral("ts")).toString();
    const QJsonObject payload = event.value(QStringLiteral("payload")).toObject();

    if (type == QStringLiteral("hello") || type == QStringLiteral("heartbeat"))
        return;

    if (type == QStringLiteral("turn_start")) {
        const QString text = payload.value(QStringLiteral("text")).toString();
        // turn_start 是分轮的权威边界 —— forceNew=true, 强制开新一轮。
        //
        // (session_id 是常驻的会话标识, 不能拿来分轮)
        ThinkingNode *turn = ensureTurn(sessionId, text, turnId, /*forceNew=*/true);
        if (!turn)
            return;

        turn->value = text;
        turn->tooltip = QStringLiteral("会话: %1\n时间: %2\n输入: %3")
                            .arg(sessionId.isEmpty() ? QStringLiteral("-") : sessionId)
                            .arg(shortTime(ts))
                            .arg(text);
        if (!turnId.isEmpty()) {
            turn->tooltip += QStringLiteral("\n轮次: %1").arg(turnId);
        }
        const int row = rowOf(turn);
        emit dataChanged(index(row, 0), index(row, 0));
        return;
    }

    if (type == QStringLiteral("thinking")) {
        const QString phase = payload.value(QStringLiteral("phase")).toString();
        ThinkingNode *turn = ensureTurn(sessionId, payload.value(QStringLiteral("text")).toString(),
                                        turnId);
        if (!turn)
            return;

        if (phase == QStringLiteral("chat")) {
            // ---- 闲聊时的思考 ----
            //
            // 聊天分支的思考 (configs/agent.yaml 的 llm.thinking)。
            // 与工具调用分开显示 —— 一个是「模型自己想问题」,
            // 一个是「模型在决定调哪个工具」。
            // reasoning = 模型真正的推理过程 (reasoning_content)。
            // content 里放的是同一份内容 (Agent 端两个字段都发, 保持兼容)。
            const QString reasoning = payload.value(QStringLiteral("reasoning")).toString()
                                     .isEmpty()
                                 ? payload.value(QStringLiteral("content")).toString()
                                 : payload.value(QStringLiteral("reasoning")).toString();
            const QString model = payload.value(QStringLiteral("model")).toString();
            const int promptTokens = payload.value(QStringLiteral("prompt_tokens")).toInt();
            const int completionTokens = payload.value(QStringLiteral("completion_tokens")).toInt();
            const int reasoningTokens = payload.value(QStringLiteral("reasoning_tokens")).toInt();
            const double elapsed = payload.value(QStringLiteral("elapsed_ms")).toDouble();

            ThinkingNode *sec = appendSection(turn, QStringLiteral("闲聊思考"), QString(), QString());
            if (!sec)
                return;

            struct Field {
                QString title;
                QString value;
                QString tooltip;
            };
            QVector<Field> fields;
            if (!reasoning.isEmpty())
                fields.append({QStringLiteral("推理"), reasoning,
                               QStringLiteral("模型的思考过程:\n%1").arg(reasoning)});
            if (!model.isEmpty())
                fields.append({QStringLiteral("模型"), model, model});
            fields.append({QStringLiteral("Token"),
                           QStringLiteral("prompt %1 / completion %2 / 思考 %3")
                               .arg(promptTokens).arg(completionTokens).arg(reasoningTokens),
                           QString()});
            fields.append({QStringLiteral("耗时"),
                           QStringLiteral("%1 ms").arg(elapsed, 0, 'f', 1), QString()});

            for (const Field &f : fields) {
                ThinkingNode *node = ensureField(sec, f.title);
                if (node) {
                    node->value = f.value;
                    node->tooltip = f.tooltip;
                } else {
                    insertField(sec, f.title, f.value, f.tooltip);
                }
            }
            const int row = rowOf(turn);
            emit dataChanged(index(row, 0), index(row, columnCount() - 1));
            return;
        }

        if (phase == QStringLiteral("param_extraction")) {
            // ---- 参数提取: 最有价值的一段 ----
            //
            // 一轮里可能提取多次 (多个工具), 所以每次都追加新节点,
            // 不覆盖前一个。
            const QString skill = payload.value(QStringLiteral("skill")).toString();
            ThinkingNode *sec = appendSection(turn, QStringLiteral("参数提取"), skill,
                                              QStringLiteral("Skill: %1").arg(skill));
            if (!sec)
                return;
            sec->value = skill;
            sec->tooltip = QStringLiteral("Skill: %1").arg(skill);

            const QJsonObject params = payload.value(QStringLiteral("extracted_params")).toObject();
            const QString reasoning = payload.value(QStringLiteral("reasoning")).toString();
            const double confidence = payload.value(QStringLiteral("confidence")).toDouble();
            const double elapsed = payload.value(QStringLiteral("elapsed_ms")).toDouble();

            struct Field {
                QString title;
                QString value;
                QString tooltip;
            };
            const QVector<Field> fields = {
                {QStringLiteral("推理"), reasoning,
                 QStringLiteral("模型的推理过程:\n%1").arg(reasoning)},
                {QStringLiteral("置信度"), QString::number(confidence, 'f', 2),
                 QStringLiteral("置信度: %1").arg(confidence)},
                {QStringLiteral("参数"), compactJson(params), compactJson(params)},
                {QStringLiteral("耗时"), QStringLiteral("%1 ms").arg(elapsed, 0, 'f', 1), QString()},
            };

            for (const Field &f : fields) {
                ThinkingNode *node = ensureField(sec, f.title);
                if (node) {
                    node->value = f.value;
                    node->tooltip = f.tooltip;
                } else {
                    insertField(sec, f.title, f.value, f.tooltip);
                }
            }
            const int row = rowOf(turn);
            emit dataChanged(index(row, 0), index(row, columnCount() - 1));
            return;
        }

        if (phase == QStringLiteral("tool_calling")
            || phase == QStringLiteral("tool_calling_retry")) {
            // ---- 工具调用: 模型选了哪个工具、说了什么 ----
            //
            // 也可能触发多次 (retry / 多工具), 同样追加而不是覆盖。
            const QString title = phase == QStringLiteral("tool_calling")
                                      ? QStringLiteral("工具调用")
                                      : QStringLiteral("工具调用 (required 重试)");
            ThinkingNode *sec = appendSection(turn, title, QString(), QString());
            if (!sec)
                return;

            const QJsonArray tools = payload.value(QStringLiteral("tools")).toArray();
            const QString content = payload.value(QStringLiteral("content")).toString();
            // reasoning = 模型**真正的推理过程** (reasoning_content)。
            // 与 content 不同: content 是模型「说出来的话」(决定调不调工具),
            // reasoning 是它「心里想的」—— 更能说明为什么选了这个工具。
            //
            // 不回退到 content —— 两者语义不同, 回退会显示错内容。
            const QString reasoning = payload.value(QStringLiteral("reasoning")).toString();
            const int reasoningTokens = payload.value(QStringLiteral("reasoning_tokens")).toInt();
            const QString model = payload.value(QStringLiteral("model")).toString();
            const int promptTokens = payload.value(QStringLiteral("prompt_tokens")).toInt();
            const int completionTokens = payload.value(QStringLiteral("completion_tokens")).toInt();
            const double elapsed = payload.value(QStringLiteral("elapsed_ms")).toDouble();

            sec->value = tools.isEmpty()
                             ? QStringLiteral("(未选工具)")
                             : tools.first().toObject().value(QStringLiteral("name")).toString();

            struct Field {
                QString title;
                QString value;
                QString tooltip;
            };
            QVector<Field> fields;
            if (!reasoning.isEmpty())
                fields.append({QStringLiteral("推理"), reasoning,
                               QStringLiteral("模型的思考过程:\n%1").arg(reasoning)});
            if (!content.isEmpty())
                fields.append({QStringLiteral("模型输出"), content, content});
            for (const QJsonValue &tv : tools) {
                const QJsonObject t = tv.toObject();
                const QString name = t.value(QStringLiteral("name")).toString();
                const QJsonObject args = t.value(QStringLiteral("arguments")).toObject();
                fields.append({QStringLiteral("工具 · %1").arg(name),
                               compactJson(args),
                               QStringLiteral("%1\n参数: %2").arg(name, compactJson(args))});
            }
            fields.append({QStringLiteral("模型"), model, model});
            fields.append({QStringLiteral("Token"),
                           QStringLiteral("prompt %1 / completion %2 / 思考 %3")
                               .arg(promptTokens).arg(completionTokens).arg(reasoningTokens),
                           QString()});
            fields.append({QStringLiteral("耗时"),
                           QStringLiteral("%1 ms").arg(elapsed, 0, 'f', 1), QString()});

            for (const Field &f : fields) {
                ThinkingNode *node = ensureField(sec, f.title);
                if (node) {
                    node->value = f.value;
                    node->tooltip = f.tooltip;
                } else {
                    insertField(sec, f.title, f.value, f.tooltip);
                }
            }
            const int row = rowOf(turn);
            emit dataChanged(index(row, 0), index(row, columnCount() - 1));
            return;
        }
        return;
    }

    if (type == QStringLiteral("turn_end")) {
        ThinkingNode *turn = ensureTurn(sessionId, payload.value(QStringLiteral("text")).toString(),
                                        turnId);
        if (!turn)
            return;

        const QString intent = payload.value(QStringLiteral("intent")).toString();
        const QString by = payload.value(QStringLiteral("by")).toString();
        const bool speak = payload.value(QStringLiteral("speak")).toBool();
        const int routeMs = payload.value(QStringLiteral("route_ms")).toInt();
        const int handlerMs = payload.value(QStringLiteral("handler_ms")).toInt();
        const int totalMs = payload.value(QStringLiteral("latency_ms")).toInt();
        const QJsonArray trace = payload.value(QStringLiteral("trace")).toArray();

        // ---- 分流 ----
        {
            ThinkingNode *sec = ensureSection(turn, QStringLiteral("分流"));
            if (!sec) {
                insertSection(turn, QStringLiteral("分流"), QString(), QString());
                sec = ensureSection(turn, QStringLiteral("分流"));
            }
            if (sec) {
                sec->value = QStringLiteral("%1 (%2)").arg(intent, by);
                sec->tooltip = QStringLiteral("命中分支: %1\n判定方式: %2\n需朗读: %3")
                                   .arg(intent, by)
                                   .arg(speak ? QStringLiteral("是") : QStringLiteral("否"));

                for (const QJsonValue &tv : trace) {
                    const QJsonObject t = tv.toObject();
                    const QString step = t.value(QStringLiteral("step")).toString();
                    const QString detail = t.value(QStringLiteral("detail")).toString();
                    const double ms = t.value(QStringLiteral("ms")).toDouble();
                    const QString value = QStringLiteral("%1 ms · %2")
                                              .arg(ms, 0, 'f', 1)
                                              .arg(detail);

                    ThinkingNode *node = ensureField(sec, step);
                    if (node) {
                        node->value = value;
                        node->tooltip = value;
                    } else {
                        insertField(sec, step, value, value);
                    }
                }
            }
        }

        // ---- 结果 ----
        {
            ThinkingNode *sec = ensureSection(turn, QStringLiteral("结果"));
            if (!sec) {
                insertSection(turn, QStringLiteral("结果"), QString(), QString());
                sec = ensureSection(turn, QStringLiteral("结果"));
            }
            if (sec) {
                const QString reply = payload.value(QStringLiteral("reply")).toString();
                const QJsonObject action = payload.value(QStringLiteral("action")).toObject();
                sec->value = reply;
                sec->tooltip = reply;

                ThinkingNode *replyNode = ensureField(sec, QStringLiteral("回复"));
                if (replyNode) {
                    replyNode->value = reply;
                    replyNode->tooltip = reply;
                } else {
                    insertField(sec, QStringLiteral("回复"), reply, reply);
                }

                if (!action.isEmpty()) {
                    const QString actionText = compactJson(action);
                    ThinkingNode *actionNode = ensureField(sec, QStringLiteral("动作"));
                    if (actionNode) {
                        actionNode->value = actionText;
                        actionNode->tooltip = actionText;
                    } else {
                        insertField(sec, QStringLiteral("动作"), actionText, actionText);
                    }
                }
            }
        }

        // ---- 耗时 ----
        {
            ThinkingNode *sec = ensureSection(turn, QStringLiteral("耗时"));
            if (!sec) {
                insertSection(turn, QStringLiteral("耗时"),
                              QStringLiteral("总计 %1 ms").arg(totalMs),
                              QStringLiteral("总计: %1 ms\n分流: %2 ms\n分支: %3 ms")
                                  .arg(totalMs).arg(routeMs).arg(handlerMs));
                sec = ensureSection(turn, QStringLiteral("耗时"));
            }
            if (sec) {
                sec->value = QStringLiteral("总计 %1 ms").arg(totalMs);
                sec->tooltip = QStringLiteral("总计: %1 ms\n分流: %2 ms\n分支: %3 ms")
                                   .arg(totalMs).arg(routeMs).arg(handlerMs);

                ThinkingNode *node = ensureField(sec, QStringLiteral("总计"));
                if (node) {
                    node->value = QStringLiteral("%1 ms").arg(totalMs);
                } else {
                    insertField(sec, QStringLiteral("总计"),
                                QStringLiteral("%1 ms").arg(totalMs), QString());
                }
            }
        }

        const int row = rowOf(turn);
        emit dataChanged(index(row, 0), index(row, columnCount() - 1));
        return;
    }

    if (type == QStringLiteral("error")) {
        ThinkingNode *turn = ensureTurn(sessionId, payload.value(QStringLiteral("text")).toString(),
                                        turnId);
        if (!turn)
            return;

        ThinkingNode *sec = ensureSection(turn, QStringLiteral("错误"));
        if (!sec) {
            insertSection(turn, QStringLiteral("错误"), QString(), QString());
            sec = ensureSection(turn, QStringLiteral("错误"));
        }
        if (!sec)
            return;

        const QString message = payload.value(QStringLiteral("message")).toString();
        sec->value = message;
        sec->tooltip = message;

        const QString reason = payload.value(QStringLiteral("reason")).toString();
        ThinkingNode *reasonNode = ensureField(sec, reason);
        if (reasonNode) {
            reasonNode->value = message;
            reasonNode->tooltip = message;
        } else {
            insertField(sec, reason, message, message);
        }
        return;
    }
}
