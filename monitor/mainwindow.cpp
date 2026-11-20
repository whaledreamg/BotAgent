#include "mainwindow.h"
#include "ui_mainwindow.h"

#include "core/agentclient.h"
#include "core/servicepoller.h"
#include "core/wsclient.h"
#include "models/monitormodel.h"
#include "models/thinkingmodel.h"
#include "ui/flowcard.h"
#include "ui/flowpanel.h"
#include "ui/servicebar.h"

#include <QCloseEvent>
#include <QDateTime>
#include <QFrame>
#include <QGraphicsDropShadowEffect>
#include <QColor>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLineEdit>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QScrollBar>
#include <QTreeView>

namespace {

/// 平板横屏的参考逻辑尺寸 (Qt 的 high-DPI 缩放后的像素)。
///
/// 10 寸平板横屏通常是 1280x800 或 1920x1200 (物理像素), 在
/// density=2 的设备上对应 640x400 / 960x600 逻辑像素。
///
/// 监控台的布局是「左右两栏 + 上下几行」, 窄了就没法看, 所以
/// 最小宽度按 640 来 —— 竖屏 (宽度 < 640) 会放不下, 让系统自己
/// 处理旋转, 我们只保证横屏能用。
constexpr int kMinTabletWidth = 640;
constexpr int kMinTabletHeight = 380;

/// 触摸目标最小高度 (Android 推荐 48dp, 取 44 已经很接近了)。
constexpr int kTouchTarget = 44;

}  // namespace

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
    m_agentClient = new AgentClient(this);

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

    connect(m_agentClient, &AgentClient::dispatched,
            this, &MainWindow::onDispatched);
    connect(m_agentClient, &AgentClient::failed,
            this, &MainWindow::onDispatchFailed);

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

    // ---- 手动指令输入框 ----
    //
    // Enter 发送, Shift+Enter 换行 —— QPlainTextEdit 默认的回车是换行,
    // 这里用 event filter 拦截「不带 Shift 的回车」改成发送。
    connect(ui->btnSend, &QPushButton::clicked,
            this, &MainWindow::onCommandSend);
    connect(ui->editCommand, &QPlainTextEdit::textChanged,
            this, &MainWindow::onCommandTextChanged);
    ui->editCommand->installEventFilter(this);
    ui->btnSend->setEnabled(false);

    // ---- 指令输入框的浮起投影 ----
    //
    // Cursor 那种输入框是「浮」在面板上的, 靠一层很淡的投影撑出层次感。
    // QSS 画不出外阴影, 得用 QGraphicsDropShadowEffect。
    //
    // 投影挂在**输入框本身**上 —— 外层 commandBar 现在是透明的,
    // 挂它身上会透出一圈方形的光晕。
    {
        auto *shadow = new QGraphicsDropShadowEffect(ui->editCommand);
        shadow->setBlurRadius(16);
        shadow->setOffset(0, 1);
        shadow->setColor(QColor(30, 40, 70, 24));
        ui->editCommand->setGraphicsEffect(shadow);
    }

    applyConfig();
    updateOverview();
    updateWsStatus();

    // ---- 触摸尺寸 ----
    //
    // 平板上要放大触摸目标; 桌面端保持鼠标友好尺寸。
    applyTouchSizing();
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

bool MainWindow::eventFilter(QObject *watched, QEvent *event)
{
    // ---- 指令输入框: Enter 发送, Shift+Enter 换行 ----
    //
    // QPlainTextEdit 默认回车是换行, 这里把「不带 Shift 的回车」改成发送。
    if (watched == ui->editCommand && event->type() == QEvent::KeyPress) {
        auto *ke = static_cast<QKeyEvent *>(event);
        if (ke->key() == Qt::Key_Return || ke->key() == Qt::Key_Enter) {
            if (!ke->modifiers().testFlag(Qt::ShiftModifier)) {
                onCommandSend();
                return true;   // 吃掉这个回车 —— 别让它再插入一个换行
            }
            // Shift+回车 —— 放行, 正常换行
        }
    }
    return QMainWindow::eventFilter(watched, event);
}

void MainWindow::closeEvent(QCloseEvent *event)
{
    if (m_wsClient)
        m_wsClient->stop();
    if (m_poller)
        m_poller->stop();
    QMainWindow::closeEvent(event);
}

// =====================================================
// 平板适配 (横屏)
// =====================================================

bool MainWindow::event(QEvent *event)
{
    if (event->type() == QEvent::Resize)
        applyTouchSizing();

    return QMainWindow::event(event);
}

void MainWindow::applyTouchSizing()
{
    // 平板上用手指操作, 触摸目标要够大 (Android 推荐 48dp)。
    //
    // 桌面端保持紧凑的鼠标友好尺寸 —— 一律放大反而会让桌面版显得笨重。
    // 判据: 窗口够宽 => 是平板/桌面横屏。
    const bool touch = width() >= kMinTabletWidth;

    const int target = touch ? kTouchTarget : 0;
    ui->btnToggle->setMinimumHeight(target);
    ui->editHost->setMinimumHeight(target);
    ui->spinInterval->setMinimumHeight(target);
    ui->editCommand->setMinimumHeight(target);

    // 发送 / 附件按钮是圆形的, 尺寸跟着目标走。
    const int side = touch ? kTouchTarget : 28;
    ui->btnSend->setMinimumSize(side, side);
    ui->btnAttach->setMinimumSize(touch ? kTouchTarget : 26,
                                  touch ? kTouchTarget : 26);

    // 触摸设备的文字选择与悬停提示没有意义, 反而会挡住内容。
    if (touch) {
        ui->treeThinking->setMouseTracking(false);
    } else {
        ui->treeThinking->setMouseTracking(true);
    }
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

    // ---- 手动指令 ----
    //
    // session_id 固定成独立的一个, 手动输入的历史不会混进语音会话。
    m_agentSessionId = cfg.agentSessionId.trimmed().isEmpty()
                           ? QStringLiteral("monitor-ui")
                           : cfg.agentSessionId.trimmed();

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

    // ---- 手动指令客户端也指向同一个 host ----
    //
    // Agent 的端口从服务列表里找叫 "Agent" 的那条拿 —— 找不到就退回 8800。
    int agentPort = 8800;
    for (const ServiceConfig &svc : services) {
        if (svc.name.compare(QStringLiteral("Agent"), Qt::CaseInsensitive) == 0) {
            agentPort = svc.port;
            break;
        }
    }
    m_agentClient->setBaseUrl(QStringLiteral("http://%1:%2").arg(host).arg(agentPort));

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

            // 手动指令的那一轮 —— 把卡片标成「手动」
            if (!m_manualText.isEmpty() && m_manualText == text.trimmed())
                card->setManual(true);

            // 有新对话了 —— 从现在开始 ASR 快照才用来建卡
            m_turnStarted = true;

            // 新一轮开始 —— 最新的一轮展开, 旧的全部折叠起来
            collapseOldThinkingTurns();
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

            // 这一轮结束了 —— 手动标记用完就清掉
            if (!m_manualText.isEmpty() && m_manualText == text.trimmed()) {
                card->setManual(true);
                m_manualText.clear();
            }

            // 阶段都插完了 —— 把最新一轮的阶段展开
            expandLatestTurnSections();
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

void MainWindow::collapseOldThinkingTurns()
{
    // ---- 思考树是倒叙的: 最新一轮在第 0 行 ----
    //
    // 只留最新一轮展开, 其余的旧轮次全部收起 —— 用户关心的是
    // 「刚才那轮在想什么」, 旧轮次想看时再手动点开。
    QTreeView *tree = ui->treeThinking;
    QAbstractItemModel *model = tree->model();
    if (!model)
        return;

    const int rows = model->rowCount(QModelIndex());
    if (rows <= 0)
        return;

    // ---- 收起第 1 行往下的所有旧轮次 ----
    //
    // 不能用 collapseAll() —— 那会把最新那轮也收起来。
    for (int r = 1; r < rows; ++r) {
        const QModelIndex idx = model->index(r, 0, QModelIndex());
        if (idx.isValid())
            tree->collapse(idx);
    }

    // ---- 最新一轮 (第 0 行) 展开 ----
    //
    // 它是刚来的那一轮, 里面的「分流 / 工具调用 / 参数提取」应该
    // 铺开给用户看。
    //
    // 注意: turn_start 的时候这一轮还是空的 (阶段是后面 thinking /
    // turn_end 事件才陆续插进来的), 所以这里只展开轮次本身 ——
    // 新插进来的阶段会自动显示出来 (父节点是展开的)。
    const QModelIndex latest = model->index(0, 0, QModelIndex());
    if (latest.isValid())
        tree->expand(latest);
}

void MainWindow::expandLatestTurnSections()
{
    // ---- 把最新一轮里的各个「阶段」也展开 ----
    //
    // 「参数提取 -> 推理」这种要两级都展开才看得到推理全文。
    //
    // 必须在**所有阶段都插完之后**才做 —— turn_start 时树还是空的,
    // 阶段是随 thinking / turn_end 事件陆续插进来的, 所以这里
    // 在 turn_end 时才调。
    QTreeView *tree = ui->treeThinking;
    QAbstractItemModel *model = tree->model();
    if (!model)
        return;

    const QModelIndex latest = model->index(0, 0, QModelIndex());
    if (!latest.isValid())
        return;

    tree->expand(latest);
    for (int r = 0; r < model->rowCount(latest); ++r) {
        const QModelIndex sec = model->index(r, 0, latest);
        if (sec.isValid())
            tree->expand(sec);
    }
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

    // **手动指令在飞行中 —— 跳过建卡**。
    //
    // ASR 快照只有一个「最近一轮」, 手动指令跟它会抢同一张卡片。
    // 流水线状态条照常更新 (在上面已经做了), 只是不碰卡片。
    if (m_manualPending)
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

// =====================================================
// 手动指令
// =====================================================

void MainWindow::onCommandSend()
{
    const QString text = ui->editCommand->toPlainText().trimmed();
    if (text.isEmpty())
        return;

    // ---- 先清空输入框 ----
    //
    // 用户体验: 立刻能接着输入下一条, 不用等这一轮跑完。
    ui->editCommand->clear();

    // ---- 立刻建卡, 不等 HTTP 回来 ----
    //
    // 用户马上就能看到「我的指令发出去了」, 而不是对着空白等几秒。
    FlowCard *card = findOrCreateCard(text);
    card->setManual(true);
    card->setAsrText(text);
    card->setPhaseText(QStringLiteral("发送中"));

    m_manualText = text;
    m_manualPending = true;

    // 新一轮开始 —— 从现在开始 ASR 快照才用来建卡
    m_turnStarted = true;

    if (!m_agentClient->dispatch(text, m_agentSessionId)) {
        // 压根没发出去 (地址没配之类) —— dispatch() 内部会发 failed()
        m_manualPending = false;
        return;
    }
}

void MainWindow::onDispatched(const QJsonObject &result)
{
    m_manualPending = false;

    // ---- WS 为主, HTTP 只做兜底 ----
    //
    // WS 的 turn_end 带完整 trace, 会把阶段改成「已完成」。
    // 所以只有阶段还停在「发送中」时才用 HTTP 的结果回填。
    FlowCard *card = ui->scrollFlow->currentCard();
    if (!card || card->phaseText() != QStringLiteral("发送中"))
        return;   // WS 已经处理过了 —— 忽略

    const QString reply = result.value(QStringLiteral("reply")).toString();
    const QString intent = result.value(QStringLiteral("intent")).toString();
    const QString by = result.value(QStringLiteral("by")).toString();
    const int latency = result.value(QStringLiteral("latency_ms")).toInt();
    const bool speak = result.value(QStringLiteral("speak")).toBool();
    const QJsonObject action = result.value(QStringLiteral("action")).toObject();

    if (!intent.isEmpty())
        card->setRoute(intent, by);
    if (!reply.isEmpty())
        card->setReply(reply, speak, describeAction(action));
    if (latency > 0)
        card->setAgentTiming(latency,
                             result.value(QStringLiteral("route_ms")).toInt(),
                             result.value(QStringLiteral("handler_ms")).toInt());
    card->setPhaseText(QStringLiteral("已完成"));
}

void MainWindow::onDispatchFailed(const QString &error)
{
    m_manualPending = false;

    FlowCard *card = ui->scrollFlow->currentCard();
    if (card && card->phaseText() == QStringLiteral("发送中")) {
        card->setPhaseText(QStringLiteral("发送失败"));
        card->setReply(QStringLiteral("指令发送失败: %1").arg(error), false, QString());
    }

    ui->scrollFlow->appendSystemEvent(QStringLiteral("Agent"),
                                      QStringLiteral("指令发送失败: %1").arg(error));
}

void MainWindow::onCommandTextChanged()
{
    const bool hasText = !ui->editCommand->toPlainText().trimmed().isEmpty();
    ui->btnSend->setEnabled(hasText);
}
