#include "mainwindow.h"
#include "ui_mainwindow.h"

#include "core/servicepoller.h"
#include "core/wsclient.h"
#include "models/monitormodel.h"
#include "models/thinkingmodel.h"
#include "ui/flowcard.h"
#include "ui/flowpanel.h"
#include "ui/servicebar.h"

#include <QCloseEvent>
#include <QDateTime>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLineEdit>
#include <QMessageBox>
#include <QPushButton>
#include <QScrollBar>
#include <QTreeView>

MainWindow::MainWindow(QWidget *parent)
    : QMainWindow(parent)
    , ui(new Ui::MainWindow)
{
    ui->setupUi(this);

    // ---- 模型 ----
    m_monitorModel = new MonitorModel(this);
    m_thinkingModel = new ThinkingModel(this);

    // ---- 视图 ----
    //
    // 服务状态不再是左侧大表格, 而是底部一排胶囊 (ServiceBar)。
    // MonitorModel 仍然保留 —— 它是状态的唯一数据源, 胶囊只是它的视图。
    m_serviceBar = ui->serviceBar;

    ui->treeThinking->setModel(m_thinkingModel);
    ui->treeThinking->header()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
    ui->treeThinking->header()->setSectionResizeMode(1, QHeaderView::Stretch);
    // 悬停即出提示 —— 思考内容在树里是单行省略的, 完整版在 tooltip 里。
    ui->treeThinking->setMouseTracking(true);

    // ---- 数据源 ----
    m_poller = new ServicePoller(this);
    m_wsClient = new WsClient(this);

    connect(m_poller, &ServicePoller::serviceUpdated,
            this, &MainWindow::onServiceUpdated);
    connect(m_poller, &ServicePoller::serviceFailed,
            this, &MainWindow::onServiceFailed);
    connect(m_poller, &ServicePoller::pollCountChanged,
            this, &MainWindow::onPollCountChanged);

    connect(m_wsClient, &WsClient::eventReceived,
            this, &MainWindow::onEventReceived);
    connect(m_wsClient, &WsClient::connectionChanged,
            this, &MainWindow::onConnectionChanged);

    // ---- 工具条 ----
    connect(ui->btnToggle, &QPushButton::clicked,
            this, &MainWindow::onToggleMonitoring);
    connect(ui->btnClearEvents, &QPushButton::clicked,
            this, &MainWindow::onClearEvents);
    connect(ui->btnClearThinking, &QPushButton::clicked,
            this, &MainWindow::onClearThinking);
    connect(ui->spinInterval, QOverload<int>::of(&QSpinBox::valueChanged),
            this, &MainWindow::onIntervalChanged);
    connect(m_serviceBar, &ServiceBar::serviceClicked,
            this, &MainWindow::onServiceBarClicked);

    // IP 输入框: 回车即按新地址重新开始监控 (正在监控时先停再起)
    connect(ui->editHost, &QLineEdit::returnPressed,
            this, &MainWindow::onHostReturnPressed);
    connect(ui->editHost, &QLineEdit::editingFinished,
            this, &MainWindow::onHostEditingFinished);

    applyConfig();
    updateOverview();
    updateWsStatus();
}

MainWindow::~MainWindow()
{
    // 先停数据源, 再让 model 析构。
    //
    // WsClient/ServicePoller 是 QObject 子对象, 析构顺序是「后创建的先析构」,
    // 所以 poller/wsClient 会比 model 先死 —— 但它们的 close() 会**同步**
    // 发信号, 槽里还会碰 model。反过来 (model 先死) 就会 use-after-free。
    //
    // 显式在这里停掉, 保证顺序是: 停源 -> 断信号 -> model 析构。
    if (m_wsClient)
        m_wsClient->stop();
    if (m_poller)
        m_poller->stop();

    delete ui;
}

void MainWindow::closeEvent(QCloseEvent *event)
{
    if (m_wsClient)
        m_wsClient->stop();
    if (m_poller)
        m_poller->stop();
    QMainWindow::closeEvent(event);
}

void MainWindow::applyConfig()
{
    const AppConfig cfg = AppConfig::load();

    // ---- 监控 IP 输入框 ----
    //
    // 界面上填什么, 开始监控时就按什么去连。
    ui->editHost->setText(cfg.defaultHost.trimmed().isEmpty()
                              ? QStringLiteral("127.0.0.1")
                              : cfg.defaultHost.trimmed());

    // ---- 服务 ----
    for (const ServiceConfig &svc : cfg.services) {
        m_monitorModel->addService(svc.name, svc.host, svc.port);
        m_serviceBar->addService(svc.name, svc.host, svc.port);
    }

    // 存一份原始配置 —— 每次「开始监控」都基于它重新套用目标 IP。
    m_baseServices = cfg.services;
    m_baseWsSources = cfg.websockets;

    m_poller->setServices(cfg.services);
    m_poller->setInterval(cfg.pollIntervalMs);
    m_poller->setRequestTimeout(cfg.requestTimeoutMs);
    ui->spinInterval->setValue(cfg.pollIntervalMs);

    // ---- WebSocket ----
    m_wsClient->setSources(cfg.websockets);
    m_wsNames.clear();
    for (const WsConfig &ws : cfg.websockets)
        m_wsNames << ws.name;

    // ---- 默认不监控 ----
    //
    // 启动后停在「未监控」状态, 等用户点「开始监控」才开始轮询与订阅。
    // config.json 里的 autoStart=true 可以改回旧行为。
    setMonitoring(cfg.autoStart);
}

/// 把「监控 IP」输入框里的地址套用到所有服务与 WebSocket 上。
///
/// 只在**开始监控**时调用 —— 换了 IP 后要重新点「开始监控」才生效。
void MainWindow::applyHost()
{
    QString host = ui->editHost->text().trimmed();
    if (host.isEmpty())
        host = QStringLiteral("127.0.0.1");

    // 服务 —— 换 host, 端口/路径不变
    QVector<ServiceConfig> services;
    for (const ServiceConfig &svc : m_baseServices)
        services << svc.withHost(host);
    m_poller->setServices(services);

    // WebSocket —— 只换 URL 里的 host 部分
    QVector<WsConfig> sources;
    for (const WsConfig &ws : m_baseWsSources)
        sources << ws.withHost(host);
    m_wsClient->setSources(sources);

    // 状态条上的地址也跟着变 (悬停提示里会显示 host:port)
    for (const ServiceConfig &svc : services)
        m_serviceBar->setServiceHost(svc.name, svc.host, svc.port);

    m_currentHost = host;
    qInfo("[监控] 目标地址已切换到 %s", qPrintable(host));
}

void MainWindow::setMonitoring(bool on)
{
    if (on) {
        // 先把输入框里的 IP 应用到各服务, 再开始轮询/订阅。
        applyHost();

        m_poller->start();
        m_wsClient->start();
    } else {
        m_poller->stop();
        m_wsClient->stop();

        // 停止监控时重置 ASR 基线 —— 否则重新开始监控后,
        // ASR /health 里残留的「上一轮」会被当成新一轮显示出来。
        m_turnStarted = false;
        m_asrBaselineText.clear();
    }

    ui->btnToggle->setText(on ? QStringLiteral("暂停监控")
                              : QStringLiteral("开始监控"));
    m_serviceBar->setMonitoring(on);

    updateOverview();
    updateWsStatus();

    emit monitoringChanged(on);
}

void MainWindow::startMonitoring()
{
    setMonitoring(true);
}

void MainWindow::stopMonitoring()
{
    setMonitoring(false);
}

void MainWindow::toggleMonitoring()
{
    setMonitoring(!m_poller->isRunning());
}

bool MainWindow::isMonitoring() const
{
    return m_poller && m_poller->isRunning();
}

void MainWindow::onToggleMonitoring()
{
    toggleMonitoring();
}

void MainWindow::onHostReturnPressed()
{
    // 回车 —— 立刻按新 IP 重新开始监控。
    //
    // 正在监控的话先停再起, 让地址切换生效; 没在监控就只记下,
    // 等用户点「开始监控」时再用。
    if (isMonitoring()) {
        setMonitoring(false);
        setMonitoring(true);
    } else {
        applyHost();
    }
}

void MainWindow::onHostEditingFinished()
{
    // 失焦 —— 只提示一下, 不打断正在跑的监控。
    if (!isMonitoring())
        return;

    const QString host = ui->editHost->text().trimmed();
    if (host.isEmpty() || host == m_currentHost)
        return;

    ui->editHost->setToolTip(
        QStringLiteral("地址已改为 %1, 重新点「开始监控」生效").arg(host));
}

void MainWindow::onClearEvents()
{
    ui->scrollFlow->clearCards();
    // 清空后把「基线」也重置 —— 否则下一次轮询会把 ASR 的历史快照
    // 又当成新一轮显示出来。
    m_turnStarted = false;
    m_asrBaselineText.clear();
}

void MainWindow::onClearThinking()
{
    m_thinkingModel->clear();
}

void MainWindow::onIntervalChanged(int ms)
{
    m_poller->setInterval(ms);
}

void MainWindow::onServiceUpdated(const QString &name, int state, int latencyMs,
                                  const QString &detail, const QString &rawStatus,
                                  const QJsonObject &statusJson)
{
    m_monitorModel->updateStatus(name, state, latencyMs, detail, rawStatus, statusJson);
    m_serviceBar->updateService(name, state, detail, latencyMs);
    updateOverview();

    // ASR 的 /health 快照里带着「最近一轮」的完整信息 ——
    // 这是 ASR 识别文本和 TTS 播报的**唯一**来源 (ASR 服务没有 WS 推送)。
    if (name == QStringLiteral("ASR")) {
        updateFromAsrSnapshot(statusJson);
    }
}

void MainWindow::onServiceFailed(const QString &name, const QString &error)
{
    m_monitorModel->markFailure(name, error);
    m_serviceBar->updateService(name, static_cast<int>(ServiceState::Offline),
                                error, -1);
    updateOverview();
}

void MainWindow::onPollCountChanged(int count)
{
    ui->lblPollCount->setText(QStringLiteral("轮询 %1 次").arg(count));
}

void MainWindow::onConnectionChanged(const QString &name, bool connected,
                                     const QString &error)
{
    updateWsStatus();

    if (!connected && !error.isEmpty())
        ui->scrollFlow->appendSystemEvent(name, QStringLiteral("连接断开: %1").arg(error));
}

void MainWindow::updateWsStatus()
{
    if (!isMonitoring()) {
        ui->lblWsStatus->setText(QStringLiteral("WS: 未连接"));
        return;
    }

    QStringList bits;
    for (const QString &n : m_wsNames) {
        bits << QStringLiteral("%1 %2").arg(
            n, m_wsClient->isConnected(n) ? QStringLiteral("✓")
                                          : QStringLiteral("✗"));
    }
    ui->lblWsStatus->setText(QStringLiteral("WS: %1").arg(bits.join(QStringLiteral("  "))));
}

void MainWindow::onEventReceived(const QString &source, const QJsonObject &event)
{
    const QString type = event.value(QStringLiteral("type")).toString();

    // ---- Agent: 驱动「对话流程」卡片 ----
    if (source == QStringLiteral("Agent")) {
        m_thinkingModel->handleEvent(event);

        if (type == QStringLiteral("turn_start")) {
            const QJsonObject payload = event.value(QStringLiteral("payload")).toObject();
            const QString text = payload.value(QStringLiteral("text")).toString();

            // **找/建本轮的卡片**。
            //
            // 卡片可能已经由 ASR 快照建好了 (ASR 先记的文本, 状态 THINK),
            // 这时要把 ASR 那边的卡片接过来, 而不是另开一张 ——
            // 否则同一轮对话会显示成两张卡。
            FlowCard *card = findOrCreateCard(text);
            // Agent 收到的文本就是 ASR 识别出来的文本 —— 先填上,
            // 免得卡片建出来时正文还是空的 (ASR 轮询可能还没到)。
            if (card->asrText().isEmpty())
                card->setAsrText(text);
            card->setPhaseText(QStringLiteral("Agent 处理中"));

            // 有新对话了 —— 从现在开始 ASR 快照才用来建卡
            m_turnStarted = true;

            // 新一轮开始时自动展开思考树, 免得用户还得手动点
            ui->treeThinking->expandAll();
            // 倒叙 —— 最新的一轮在顶部, 所以滚到顶而不是底
            ui->treeThinking->scrollToTop();
        } else if (type == QStringLiteral("turn_end")) {
            const QJsonObject payload = event.value(QStringLiteral("payload")).toObject();
            const QString text = payload.value(QStringLiteral("text")).toString();

            // turn_end 带着完整的本轮文本, 用它来精确定位卡片。
            // (如果 ASR 那边建过卡, 文本一致就是同一张)
            FlowCard *card = findOrCreateCard(text);

            card->setRoute(payload.value(QStringLiteral("intent")).toString(),
                           payload.value(QStringLiteral("by")).toString());
            card->setReply(payload.value(QStringLiteral("reply")).toString(),
                           payload.value(QStringLiteral("speak")).toBool(),
                           describeAction(payload.value(QStringLiteral("action")).toObject()));
            card->setAgentTiming(payload.value(QStringLiteral("latency_ms")).toInt(),
                                 payload.value(QStringLiteral("route_ms")).toInt(),
                                 payload.value(QStringLiteral("handler_ms")).toInt());
            card->setPhaseText(QStringLiteral("已完成"));
        }
        return;
    }

    // ---- Arm / Vision: 不属于对话流程, 归到「系统事件」 ----
    ui->scrollFlow->appendSystemEvent(source, summarize(source, event));
}

FlowCard *MainWindow::findOrCreateCard(const QString &text)
{
    FlowCard *card = ui->scrollFlow->currentCard();

    // 文本一样 -> 还是这一轮, 直接复用
    if (card && !text.isEmpty() && card->asrText() == text.trimmed())
        return card;

    // 卡片已经结束 (上一轮), 或者没有卡片 -> 新开一张
    // (文本为空时也开新卡 —— 拿不到文本总比张冠李戴好)
    if (!card || card->isComplete())
        return ui->scrollFlow->beginTurn(QDateTime::currentDateTime());

    // 卡片还没结束但文本对不上 —— 说明是新一轮但 ASR 快照还没更新
    return ui->scrollFlow->beginTurn(QDateTime::currentDateTime());
}

QString MainWindow::describeAction(const QJsonObject &action)
{
    if (action.isEmpty())
        return QString();

    // action 可能是 {name, status, ...}, 也可能是 {items: [...]} (多个工具调用)
    if (action.contains(QStringLiteral("items"))) {
        const QJsonArray items = action.value(QStringLiteral("items")).toArray();
        QStringList names;
        for (const QJsonValue &v : items) {
            const QJsonObject o = v.toObject();
            const QString n = o.value(QStringLiteral("name")).toString();
            names << (n.isEmpty() ? QStringLiteral("?") : n);
        }
        return names.join(QStringLiteral(", "));
    }

    const QString name = action.value(QStringLiteral("name")).toString();
    return name.isEmpty() ? QString() : name;
}

void MainWindow::updateFromAsrSnapshot(const QJsonObject &snapshot)
{
    if (snapshot.isEmpty())
        return;

    // ---- 流水线状态 ----
    const QString state = snapshot.value(QStringLiteral("state")).toString();
    const int turns = snapshot.value(QStringLiteral("turns")).toInt();
    QString hint;
    if (state == QStringLiteral("REC"))
        hint = QStringLiteral("正在录音…");
    else if (state == QStringLiteral("THINK"))
        hint = QStringLiteral("等待 Agent 分流…");
    else if (state == QStringLiteral("SAY"))
        hint = QStringLiteral("正在播报…");
    else
        hint = QStringLiteral("已轮 %1 轮").arg(turns);
    ui->scrollFlow->setPipelineState(state, hint);

    // ---- 最近一轮的内容 ----
    const QString text = snapshot.value(QStringLiteral("last_text")).toString();
    if (text.trimmed().isEmpty())
        return;

    const QString reply = snapshot.value(QStringLiteral("last_reply")).toString();
    const QString intent = snapshot.value(QStringLiteral("last_intent")).toString();
    const bool speak = snapshot.value(QStringLiteral("last_speak")).toBool();
    const QJsonObject timings = snapshot.value(QStringLiteral("timings")).toObject();
    const QJsonObject action = snapshot.value(QStringLiteral("last_action")).toObject();

    // **启动时不建卡**。
    //
    // ASR 的 /health 快照里存着「最近一轮」, 那是**上一次**对话 ——
    // 监控台一启动就把它显示成一张卡片, 会让人误以为刚刚有过一轮对话。
    //
    // 所以先记下这段历史文本当作基线: 快照文本和基线一样时, 说明还是
    // 上一轮 (还没发生新对话), 不建卡。等 Agent 发 turn_start 之后
    // 才真正开始建卡。
    if (!m_turnStarted) {
        m_asrBaselineText = text.trimmed();
        return;
    }

    // ASR 快照只有一个「最近一轮」, 所以文本变了就是新的一轮 ——
    // 复用 findOrCreateCard, 保证 ASR 和 Agent 落到同一张卡片上。
    FlowCard *card = findOrCreateCard(text);
    card->setAsrText(text);

    if (!intent.isEmpty())
        card->setRoute(intent, QString());
    if (!reply.isEmpty()) {
        card->setReply(reply, speak, describeAction(action));
        card->setPhaseText(QStringLiteral("已完成"));
    }
    if (!timings.isEmpty())
        card->setAsrTiming(timings);
}

QString MainWindow::summarize(const QString &source, const QJsonObject &event)
{
    const QString type = event.value(QStringLiteral("type")).toString();
    const QJsonObject payload = event.value(QStringLiteral("payload")).toObject();

    if (source == QStringLiteral("Arm")) {
        if (type == QStringLiteral("state"))
            return QStringLiteral("%1 | %2")
                .arg(payload.value(QStringLiteral("phase")).toString(),
                     payload.value(QStringLiteral("detail")).toString());
        if (type == QStringLiteral("finished"))
            return QStringLiteral("动作播放结束");
        if (type == QStringLiteral("error"))
            return payload.value(QStringLiteral("message")).toString();
        return QString();
    }

    if (source == QStringLiteral("Vision")) {
        if (type == QStringLiteral("detection")) {
            const QJsonArray objects = payload.value(QStringLiteral("objects")).toArray();
            QStringList names;
            for (const QJsonValue &v : objects) {
                const QJsonObject o = v.toObject();
                names << QStringLiteral("%1(%2%)")
                             .arg(o.value(QStringLiteral("label")).toString())
                             .arg(o.value(QStringLiteral("confidence")).toDouble() * 100, 0, 'f', 0);
            }
            return QStringLiteral("检测到 %1 个物体: %2 (%3ms)")
                .arg(objects.size())
                .arg(names.join(QStringLiteral(", ")))
                .arg(payload.value(QStringLiteral("latency_ms")).toInt());
        }
        if (type == QStringLiteral("error"))
            return payload.value(QStringLiteral("message")).toString();
        return QString();
    }

    return QString();
}

void MainWindow::updateOverview()
{
    int online = 0;
    int degraded = 0;
    int offline = 0;

    const int rows = m_monitorModel->rowCount();
    for (int i = 0; i < rows; ++i) {
        const QModelIndex idx = m_monitorModel->index(i, MonitorModel::ColState);
        switch (idx.data(Qt::UserRole).toInt()) {
        case static_cast<int>(ServiceState::Online):
            ++online;
            break;
        case static_cast<int>(ServiceState::Degraded):
            ++degraded;
            break;
        case static_cast<int>(ServiceState::Offline):
            ++offline;
            break;
        default:
            break;
        }
    }

    ui->lblOverview->setText(
        isMonitoring()
            ? QStringLiteral("监控中 · 正常 %1 · 降级 %2 · 断开 %3")
                  .arg(online).arg(degraded).arg(offline)
            : QStringLiteral("监控未启动"));
}

void MainWindow::onServiceBarClicked(const QString &name)
{
    const ServiceStatus *status = m_monitorModel->status(name);
    if (!status)
        return;

    QString text = QStringLiteral("%1 (%2:%3)\n状态: %4\n")
                       .arg(status->name, status->host)
                       .arg(status->port)
                       .arg(status->stateText());
    if (status->latencyMs >= 0)
        text += QStringLiteral("延迟: %1 ms\n").arg(status->latencyMs);
    if (!status->detail.isEmpty())
        text += QStringLiteral("说明: %1\n").arg(status->detail);
    if (!status->lastError.isEmpty())
        text += QStringLiteral("错误: %1\n").arg(status->lastError);
    if (!status->statusJson.isEmpty()) {
        text += QStringLiteral("\n--- 原始状态 JSON ---\n");
        text += QString::fromUtf8(
            QJsonDocument(status->statusJson).toJson(QJsonDocument::Indented));
    }

    QMessageBox box(this);
    box.setWindowTitle(QStringLiteral("%1 详情").arg(status->name));
    box.setText(QStringLiteral("服务状态详情"));
    box.setDetailedText(text);
    box.setInformativeText(QStringLiteral("%1 · %2").arg(status->stateText(),
                                                        status->detail.isEmpty()
                                                            ? QStringLiteral("无附加信息")
                                                            : status->detail));
    box.exec();
}
