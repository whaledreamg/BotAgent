// 监控 APP 的逻辑自测 —— 不依赖 GUI 截图, 直接验证模型层。
//
// 用法: monitor/build/botagent_monitor_selftest
#include "core/agentclient.h"
#include "core/appconfig.h"
#include "models/monitormodel.h"
#include "models/thinkingmodel.h"
#include "ui/flowcard.h"
#include "ui/flowpanel.h"

#include <QApplication>
#include <QDateTime>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QTextStream>

static QTextStream out(stdout);

/// 统计卡片里「可见的正文行」数量 (用来验证空白段被隐藏)。
///
/// 用 isVisibleTo(card) 而不是 isVisible() —— 卡片自己还没 show,
/// isVisible() 对所有子控件都返回 false, 测不出东西。
static int visibleValueCount(const FlowCard *card)
{
    int count = 0;
    for (QLabel *label : card->findChildren<QLabel *>()) {
        if (label->objectName() == QLatin1String("flowValue")
            && label->isVisibleTo(card) && !label->text().isEmpty()) {
            ++count;
        }
    }
    return count;
}

static void dumpTree(const ThinkingModel &model, const QModelIndex &parent, int depth){
    const int rows = model.rowCount(parent);
    for (int r = 0; r < rows; ++r) {
        const QModelIndex idx = model.index(r, 0, parent);
        const QModelIndex valIdx = model.index(r, 1, parent);
        const QString title = model.data(idx, Qt::DisplayRole).toString();
        const QString value = model.data(valIdx, Qt::DisplayRole).toString();
        out << QString(depth * 4, QLatin1Char(' '))
            << title << "  |  " << value << "\n";
        dumpTree(model, idx, depth + 1);
    }
}

int main(int argc, char *argv[])
{
    QApplication app(argc, argv);

    out << "========== 1. 配置 ==========\n";
    const AppConfig cfg = AppConfig::load();
    for (const ServiceConfig &s : cfg.services)
        out << "  " << s.name << " -> " << s.healthUrl() << "\n";
    for (const WsConfig &w : cfg.websockets)
        out << "  WS " << w.name << " -> " << w.url << "\n";

    out << "\n========== 2. 服务状态模型 ==========\n";
    MonitorModel monitor;
    monitor.addService("Agent", "127.0.0.1", 8800);
    monitor.addService("ASR", "127.0.0.1", 8801);
    monitor.updateStatus("Agent", 1, 12, "model=agnes-2.5-flash", "{}", QJsonObject());
    monitor.updateStatus("ASR", 3, -1, "连接被拒绝", "", QJsonObject());
    monitor.updateStatus("TTS", -1, 8, "model_loaded=true", "", QJsonObject());  // 不改状态
    for (int r = 0; r < monitor.rowCount(); ++r) {
        const auto name = monitor.index(r, MonitorModel::ColName).data().toString();
        const auto state = monitor.index(r, MonitorModel::ColState).data().toString();
        const auto lat = monitor.index(r, MonitorModel::ColLatency).data().toString();
        const auto det = monitor.index(r, MonitorModel::ColDetail).data().toString();
        out << "  " << name << " | " << state << " | " << lat << " | " << det << "\n";
    }

    out << "\n========== 3. 思考过程模型 ==========\n";
    ThinkingModel thinking;

    QJsonObject turnStart{
        {"type", "turn_start"},
        {"ts", "2026-11-18T15:01:16.949033"},
        {"session_id", "robot-01"},
        {"payload", QJsonObject{{"text", "查北京天气"}}}};
    thinking.handleEvent(turnStart);

    QJsonObject thinking1{
        {"type", "thinking"},
        {"ts", "2026-11-18T15:01:18.100000"},
        {"session_id", "robot-01"},
        {"payload", QJsonObject{
            {"phase", "param_extraction"},
            {"text", "查北京天气"},
            {"skill", "weather"},
            {"extracted_params", QJsonObject{{"city", "北京"}}},
            {"confidence", 0.98},
            {"reasoning", "用户明确提到'查北京天气'，其中'北京'是具体的城市名称"},
            {"elapsed_ms", 1343.4}}}};
    thinking.handleEvent(thinking1);

    QJsonObject thinking2{
        {"type", "thinking"},
        {"ts", "2026-11-18T15:01:18.200000"},
        {"session_id", "robot-01"},
        {"payload", QJsonObject{
            {"phase", "tool_calling"},
            {"text", "查北京天气"},
            {"content", "用户想查询北京的天气"},
            {"model", "agnes-2.5-flash"},
            {"prompt_tokens", 369},
            {"completion_tokens", 115},
            {"elapsed_ms", 580.2},
            {"tools", QJsonArray{QJsonObject{
                {"name", "query_weather"},
                {"arguments", QJsonObject{{"city", "北京"}}}}}}}}};
    thinking.handleEvent(thinking2);

    QJsonObject turnEnd{
        {"type", "turn_end"},
        {"ts", "2026-11-18T15:01:19.000000"},
        {"session_id", "robot-01"},
        {"payload", QJsonObject{
            {"text", "查北京天气"},
            {"intent", "tool"},
            {"by", "rule"},
            {"speak", true},
            {"reply", "北京今天晴，气温 25 度"},
            {"action", QJsonObject{{"name", "weather"}, {"status", "ok"}}},
            {"latency_ms", 7603},
            {"route_ms", 2},
            {"handler_ms", 7601},
            {"trace", QJsonArray{
                QJsonObject{{"step", "rules"}, {"ms", 0.0}, {"detail", "tool:天气"}},
                QJsonObject{{"step", "timing"}, {"ms", 2.0}, {"detail", "route:2ms"}}}}}}};
    thinking.handleEvent(turnEnd);

    dumpTree(thinking, QModelIndex(), 0);

    out << "\n========== 3.1 思考内容 tooltip (完整原文) ==========\n";
    {
        // 找「参数提取 -> 推理」这一格, 验证 tooltip 里带完整 reasoning。
        const QModelIndex turnIdx = thinking.index(0, 0);
        bool found = false;
        for (int r = 0; r < thinking.rowCount(turnIdx); ++r) {
            const QModelIndex sec = thinking.index(r, 0, turnIdx);
            if (thinking.data(sec, Qt::DisplayRole).toString() != QStringLiteral("参数提取"))
                continue;
            for (int f = 0; f < thinking.rowCount(sec); ++f) {
                const QModelIndex field = thinking.index(f, 0, sec);
                if (thinking.data(field, Qt::DisplayRole).toString() != QStringLiteral("推理"))
                    continue;
                const QString tip = thinking.data(field, Qt::ToolTipRole).toString();
                const QString value = thinking.data(field, ThinkingModel::ValueRole).toString();
                out << "  推理 value = " << value << "\n";
                out << "  推理 tooltip = " << tip << "\n";
                if (tip.contains(QStringLiteral("北京"))
                    && tip.contains(QStringLiteral("城市名称"))) {
                    out << "  [PASS] tooltip 含完整 reasoning\n";
                    found = true;
                } else {
                    out << "  [FAIL] tooltip 缺少 reasoning 全文\n";
                }
            }
        }
        if (!found)
            out << "  [FAIL] 没找到「参数提取 -> 推理」节点\n";
    }

    {
        // 工具调用里的参数 JSON 也应能在 tooltip 里看到全文。
        const QModelIndex turnIdx = thinking.index(0, 0);
        bool found = false;
        for (int r = 0; r < thinking.rowCount(turnIdx); ++r) {
            const QModelIndex sec = thinking.index(r, 0, turnIdx);
            if (thinking.data(sec, Qt::DisplayRole).toString() != QStringLiteral("工具调用"))
                continue;
            for (int f = 0; f < thinking.rowCount(sec); ++f) {
                const QModelIndex field = thinking.index(f, 0, sec);
                const QString title = thinking.data(field, Qt::DisplayRole).toString();
                if (!title.startsWith(QStringLiteral("工具 ·")))
                    continue;
                const QString tip = thinking.data(field, Qt::ToolTipRole).toString();
                out << "  " << title << " tooltip = " << tip << "\n";
                if (tip.contains(QStringLiteral("query_weather")) && tip.contains(QStringLiteral("北京"))) {
                    out << "  [PASS] 工具参数 tooltip 含完整内容\n";
                    found = true;
                }
            }
        }
        if (!found)
            out << "  [FAIL] 没找到「工具 · xxx」节点或 tooltip 不完整\n";
    }

    out << "\n========== 3.2 多轮分轮 (turn_id / turn_start 边界) ==========\n";
    {
        ThinkingModel m;

        // ---- 场景 A: 老协议 (无 turn_id), 只有 turn_start 分轮 ----
        //
        // session_id 一直是 robot-01 —— 如果靠它分轮, 所有轮次会塌成第一轮。
        const auto mkStart = [](const QString &text) {
            return QJsonObject{
                {"type", "turn_start"},
                {"ts", "2026-11-18T15:02:00.000000"},
                {"session_id", "robot-01"},
                {"payload", QJsonObject{{"text", text}}}};
        };
        const auto mkEnd = [](const QString &text, const QString &reply) {
            return QJsonObject{
                {"type", "turn_end"},
                {"ts", "2026-11-18T15:02:01.000000"},
                {"session_id", "robot-01"},
                {"payload", QJsonObject{
                    {"text", text},
                    {"intent", "tool"},
                    {"by", "rule"},
                    {"speak", true},
                    {"reply", reply},
                    {"latency_ms", 100},
                    {"route_ms", 1},
                    {"handler_ms", 99},
                    {"trace", QJsonArray{}}}}};
        };

        m.handleEvent(mkStart(QStringLiteral("第一句")));
        m.handleEvent(mkEnd(QStringLiteral("第一句"), QStringLiteral("回答一")));
        m.handleEvent(mkStart(QStringLiteral("第二句")));
        m.handleEvent(mkEnd(QStringLiteral("第二句"), QStringLiteral("回答二")));
        m.handleEvent(mkStart(QStringLiteral("第三句")));
        m.handleEvent(mkEnd(QStringLiteral("第三句"), QStringLiteral("回答三")));

        out << "  [老协议] 轮次数 = " << m.turnCount() << " (应为 3)\n";
        if (m.turnCount() == 3)
            out << "  [PASS] turn_start 边界正确分轮\n";
        else
            out << "  [FAIL] 轮次数不对 —— session_id 被误用来分轮了\n";

        // 每轮的内容也要各自独立, 不能互相覆盖
        bool contentsOk = true;
        for (int r = 0; r < m.rowCount(); ++r) {
            const QModelIndex turnIdx = m.index(r, 0);
            const QString title = m.data(turnIdx, Qt::DisplayRole).toString();
            const QString value = m.data(m.index(r, 1), Qt::DisplayRole).toString();
            out << "    " << title << " -> " << value << "\n";
            if (value.isEmpty())
                contentsOk = false;
        }
        if (contentsOk)
            out << "  [PASS] 每轮内容独立, 没有互相覆盖\n";
        else
            out << "  [FAIL] 有轮次内容为空\n";

        // ---- 倒叙: 最新的一轮在顶部 ----
        const QString topTitle = m.data(m.index(0, 0), Qt::DisplayRole).toString();
        const QString topValue = m.data(m.index(0, 1), Qt::DisplayRole).toString();
        const QString bottomTitle = m.data(m.index(m.rowCount() - 1, 0), Qt::DisplayRole).toString();
        out << "  顶部 = " << topTitle << " (" << topValue << ")\n";
        out << "  底部 = " << bottomTitle << "\n";
        if (topTitle == QStringLiteral("第 3 轮")
            && topValue == QStringLiteral("第三句")
            && bottomTitle == QStringLiteral("第 1 轮")) {
            out << "  [PASS] 倒叙显示 —— 最新一轮在顶部\n";
        } else {
            out << "  [FAIL] 倒叙顺序不对 (最新一轮应该在顶部)\n";
        }
    }

    {
        // ---- 场景 B: 新协议 (带 turn_id) ----
        //
        // turn_id 每轮不同 —— 同一 turn_id 的事件归同一轮。
        ThinkingModel m;
        const auto mkStart = [](const QString &text, const QString &tid) {
            return QJsonObject{
                {"type", "turn_start"},
                {"ts", "2026-11-18T15:03:00.000000"},
                {"session_id", "robot-01"},
                {"turn_id", tid},
                {"payload", QJsonObject{{"text", text}}}};
        };

        m.handleEvent(mkStart(QStringLiteral("甲"), QStringLiteral("turn-a")));
        m.handleEvent(mkStart(QStringLiteral("乙"), QStringLiteral("turn-b")));
        m.handleEvent(mkStart(QStringLiteral("丙"), QStringLiteral("turn-c")));

        out << "  [新协议] 轮次数 = " << m.turnCount() << " (应为 3)\n";
        if (m.turnCount() == 3)
            out << "  [PASS] turn_id 正确分轮\n";
        else
            out << "  [FAIL] turn_id 分轮失败\n";

        // 同一 turn_id 的后续事件要挂回原来那一轮
        const QJsonObject late{
            {"type", "thinking"},
            {"ts", "2026-11-18T15:03:02.000000"},
            {"session_id", "robot-01"},
            {"turn_id", "turn-a"},
            {"payload", QJsonObject{
                {"phase", "param_extraction"},
                {"text", "甲"},
                {"skill", "weather"},
                {"extracted_params", QJsonObject{{"city", "南京"}}},
                {"confidence", 0.9},
                {"reasoning", "识别到南京"},
                {"elapsed_ms", 10.0}}}};
        m.handleEvent(late);

        // 「甲」是 turn-a 那一轮 —— 倒叙下它在最后一行 (第 1 轮)。
        // 它的子节点里应该能找到「参数提取」。
        //
        // 先按内容找到 turn-a 那一行 (而不是写死行号 —— 倒叙后行号会变)。
        QModelIndex targetTurn;
        for (int r = 0; r < m.rowCount(); ++r) {
            if (m.data(m.index(r, 1), Qt::DisplayRole).toString() == QStringLiteral("甲")) {
                targetTurn = m.index(r, 0);
                break;
            }
        }
        bool found = false;
        if (targetTurn.isValid()) {
            for (int r = 0; r < m.rowCount(targetTurn); ++r) {
                if (m.data(m.index(r, 0, targetTurn), Qt::DisplayRole).toString()
                    == QStringLiteral("参数提取")) {
                    found = true;
                }
            }
        }
        if (found)
            out << "  [PASS] turn_id 事件挂到了正确的轮次\n";
        else
            out << "  [FAIL] turn_id 事件挂错轮次了\n";
    }

    out << "\n========== 4. 对话流程卡片 ==========\n";
    FlowPanel flow;
    FlowCard *card = flow.beginTurn(QDateTime::fromString(
        QStringLiteral("2026-11-18T15:01:16.949"), Qt::ISODateWithMs));

    // 刚建卡时什么都没填 —— 所有正文行都应该隐藏
    out << "  [空卡片] 可见的正文行数=" << visibleValueCount(card) << " (应为 0)\n";

    card->setAsrText(QStringLiteral("查南京天气"));
    out << "  [只填ASR] 可见的正文行数=" << visibleValueCount(card) << " (应为 1)\n";

    card->setRoute(QStringLiteral("tool"), QStringLiteral("tools"));
    card->setReply(QStringLiteral("南京今天晴，气温 25 度"), true, QStringLiteral("query_weather"));
    card->setAgentTiming(7603, 2, 7601);
    card->setAsrTiming(QJsonObject{
        {"asr_record_ms", 1200},
        {"agent_total_ms", 7603},
        {"tts_total_ms", 900},
        {"turn_total_ms", 8503},
        {"end_to_end_ms", 9703}});
    card->setPhaseText(QStringLiteral("已完成"));

    out << "  卡片数=" << flow.cardCount() << "\n";
    out << "  [填完] 可见的正文行数=" << visibleValueCount(card) << " (应为 4)\n";
    out << "  ASR=" << card->findChild<QLabel *>(QStringLiteral("flowValue"))->text() << "\n";

    out << "\n========== 5. 手动指令标识 (FlowCard::setManual) ==========\n";
    {
        // 找「ASR 识别 / 输入文本」那一行的标题
        auto findAsrCaption = [](FlowCard *c) -> QLabel * {
            for (QLabel *label : c->findChildren<QLabel *>()) {
                if (label->objectName() == QLatin1String("flowCaption")
                    && (label->text() == QStringLiteral("ASR 识别")
                        || label->text() == QStringLiteral("输入文本"))) {
                    return label;
                }
            }
            return nullptr;
        };

        FlowPanel p;
        FlowCard *mc = p.beginTurn(QDateTime::currentDateTime());
        mc->setAsrText(QStringLiteral("手动输入的指令"));

        // ---- 默认 (语音轮次) ----
        QLabel *caption = findAsrCaption(mc);
        QLabel *manualBadge = mc->findChild<QLabel *>(QStringLiteral("flowManualBadge"));
        out << "  默认标题 = " << (caption ? caption->text() : QStringLiteral("<没找到>")) << "\n";
        out << "  默认 isManual = " << (mc->isManual() ? "true" : "false") << "\n";
        if (caption && caption->text() == QStringLiteral("ASR 识别")
            && !mc->isManual()) {
            out << "  [PASS] 默认是「ASR 识别」且非手动\n";
        } else {
            out << "  [FAIL] 默认状态不对\n";
        }
        if (manualBadge && !manualBadge->isVisibleTo(mc)) {
            out << "  [PASS] 默认不显示「手动」徽标\n";
        } else {
            out << "  [FAIL] 「手动」徽标默认应该隐藏\n";
        }

        // ---- 切到手动 ----
        mc->setManual(true);
        caption = findAsrCaption(mc);
        out << "  手动标题 = " << (caption ? caption->text() : QStringLiteral("<没找到>")) << "\n";
        out << "  手动 isManual = " << (mc->isManual() ? "true" : "false") << "\n";
        if (caption && caption->text() == QStringLiteral("输入文本")
            && mc->isManual()) {
            out << "  [PASS] 手动模式下标题为「输入文本」\n";
        } else {
            out << "  [FAIL] 手动模式下标题没切过去\n";
        }
        if (manualBadge && manualBadge->isVisibleTo(mc)) {
            out << "  [PASS] 手动模式显示「手动」徽标\n";
        } else {
            out << "  [FAIL] 手动模式应该显示「手动」徽标\n";
        }

        // ---- 切回语音 ----
        mc->setManual(false);
        caption = findAsrCaption(mc);
        out << "  切回标题 = " << (caption ? caption->text() : QStringLiteral("<没找到>")) << "\n";
        if (caption && caption->text() == QStringLiteral("ASR 识别")
            && !mc->isManual()) {
            out << "  [PASS] 切回语音模式, 标题恢复「ASR 识别」\n";
        } else {
            out << "  [FAIL] 切回语音模式失败\n";
        }
    }

    out << "\n========== 6. 手动指令客户端 (AgentClient) ==========\n";
    {
        AgentClient client;
        client.setBaseUrl(QStringLiteral("http://127.0.0.1:8800/"));
        out << "  baseUrl = " << client.baseUrl() << "\n";
        // 尾部斜杠要被去掉 —— 否则会拼出 http://host:8800//v1/dispatch
        if (client.baseUrl() == QStringLiteral("http://127.0.0.1:8800"))
            out << "  [PASS] baseUrl 规范化正常 (尾部斜杠已去掉)\n";
        else
            out << "  [FAIL] baseUrl 规范化不对\n";

        // 空地址时 dispatch 要直接失败, 不能发请求
        AgentClient noAddr;
        bool failedEmitted = false;
        QObject::connect(&noAddr, &AgentClient::failed,
                         [&](const QString &) { failedEmitted = true; });
        const bool ok = noAddr.dispatch(QStringLiteral("测试"), QStringLiteral("monitor-ui"));
        out << "  空地址 dispatch 返回 = " << (ok ? "true" : "false") << " (应为 false)\n";
        if (!ok && failedEmitted)
            out << "  [PASS] 空地址时立即报错\n";
        else
            out << "  [FAIL] 空地址时应该直接失败\n";
    }

    out << "\n========== 7. 手动指令 session id ==========\n";
    {
        out << "  agentSessionId = " << cfg.agentSessionId << "\n";
        if (!cfg.agentSessionId.isEmpty() && cfg.agentSessionId != QStringLiteral("robot-01"))
            out << "  [PASS] 手动指令用独立 session, 不污染语音历史\n";
        else
            out << "  [FAIL] agentSessionId 不对\n";
    }

    out << "\n自测完成\n";
    return 0;
}
