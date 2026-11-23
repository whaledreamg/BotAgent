#include "mainwindow.h"
#include "ui_mainwindow.h"

#include "core/agentclient.h"
#include "core/servicepoller.h"
#include "core/visionclient.h"
#include "core/wsclient.h"
#include "models/monitormodel.h"
#include "models/thinkingmodel.h"
#include "ui/asrstatusindicator.h"
#include "ui/flowcard.h"
#include "ui/flowpanel.h"
#include "ui/servicedetaildialog.h"
#include "ui/servicebar.h"

#include <QCloseEvent>
#include <QDateTime>
#include <QFrame>
#include <QGraphicsDropShadowEffect>
#include <QColor>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QListWidgetItem>
#include <QMessageBox>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QPixmap>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QScrollBar>
#include <QTreeView>
#include <QUrl>

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

    // ---- 快速轮询 (语音识别三态动画用) ----
    //
    // 普通轮询 2 秒一次, 画动画太慢 —— 单独开一路 250ms 的。
    connect(m_poller, &ServicePoller::fastSnapshot,
            this, &MainWindow::onFastSnapshot);
    connect(m_poller, &ServicePoller::fastFailed,
            this, &MainWindow::onFastFailed);

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

    // ---- 中间那一列: 语音识别动画 + 图像识别结果 ----
    //
    // 布局 (从上到下):
    //   语音识别三态动画
    //   「识别一次」按钮 + 状态提示
    //   标注图预览 (缩放显示, 识别完自动刷新)
    //   识别到的物体列表
    //
    // **不轮询** —— 识别只在用户点按钮时发生 (或者别处触发
    // 的一次性检测), 结果靠 WebSocket 广播回来。
    m_listVision = ui->listVision;
    m_imageNam = new QNetworkAccessManager(this);
    connect(m_listVision, &QListWidget::itemClicked,
            this, &MainWindow::onVisionItemClicked);

    m_lblVisionImage = ui->lblVisionImage;
    m_lblVisionStatus = ui->lblVisionStatus;
    m_btnDetect = ui->btnDetect;
    connect(m_btnDetect, &QPushButton::clicked, this, &MainWindow::onDetectClicked);

    // 标注图用「缩放显示」而不是「原图大小」——
    // 相机出图 640x400, 直接塞进来会把这一列撑开。
    m_lblVisionImage->setScaledContents(false);
    m_lblVisionImage->setAlignment(Qt::AlignCenter);
    m_lblVisionImage->setMinimumHeight(140);

    m_visionClient = new VisionClient(this);
    connect(m_visionClient, &VisionClient::failed, this, [this](const QString &err) {
        if (m_btnDetect)
            m_btnDetect->setEnabled(true);
        if (m_lblVisionStatus)
            m_lblVisionStatus->setText(QStringLiteral("识别失败: %1").arg(err));
    });

    // ---- 三栏的宽度比例 ----
    //
    // 对话流程最宽 (卡片要放整句话), 思考过程次之 (树里的内容能看
    // tooltip), 识别状态最窄 (动画 + 标注图 + 列表)。
    ui->mainSplitter->setStretchFactor(0, 45);
    ui->mainSplitter->setStretchFactor(1, 35);
    ui->mainSplitter->setStretchFactor(2, 20);

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

    // ---- 视觉服务地址 (拉标注图 / 触发识别时要用) ----
    m_visionHost = host;
    m_visionPort = 8803;
    for (const ServiceConfig &svc : services) {
        if (svc.name.compare(QStringLiteral("Vision"), Qt::CaseInsensitive) == 0) {
            m_visionPort = svc.port;
            break;
        }
    }
    m_visionClient->setBaseUrl(
        QStringLiteral("http://%1:%2").arg(host).arg(m_visionPort));

    m_currentHost = host;
    // 换了地址 -> 之前存的那份作废, 连上后要重新存。
    m_hostSaved = false;
    qInfo("[监控] 目标地址已切换到 %s", qPrintable(host));
}

/// 把当前 IP 持久化到 config.json。
///
/// **只在真正连上之后调** —— 连不上的地址不该被记住, 否则下次启动
/// 会指向一个根本连不通的地方。
void MainWindow::saveHost(const QString &host)
{
    const QString h = host.trimmed();
    if (h.isEmpty() || m_hostSaved)
        return;

    AppConfig cfg = AppConfig::load();
    cfg.defaultHost = h;
    if (AppConfig::save(cfg))
        m_hostSaved = true;
}

void MainWindow::setMonitoring(bool on)
{
    if (on) {
        // 先把输入框里的 IP 应用到各服务, 再开始轮询/订阅。
        applyHost();

        m_poller->start();
        m_wsClient->start();

        // ---- 快速轮询 (语音识别三态动画) ----
        //
        // 只盯 ASR 的 /health, 250ms 一次 —— 普通轮询 2 秒太慢,
        // 动画会一顿一顿的。
        m_poller->setFastTarget(QStringLiteral("ASR"),
                                QStringLiteral("/health"), 250);
        m_poller->startFast();
    } else {
        m_poller->stop();
        m_poller->stopFast();
        m_wsClient->stop();

        // 停止监控时重置 ASR 基线 —— 否则重新开始监控后,
        // ASR /health 里残留的「上一轮」会被当成新一轮显示出来。
        m_turnStarted = false;
        m_asrBaselineText.clear();
        m_lastRecResultKey.clear();
        m_asrPrimed = false;
        m_asrDoneShown = false;

        // 动画回到「未连接」, 别停在一个过时的状态上。
        ui->asrIndicator->setState(AsrStatusIndicator::State::Offline,
                                   QStringLiteral("监控已停止"));
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

    // ---- 连上了 -> 把这个 IP 存下来 ----
    //
    // state >= Online (1) 表示这个服务真的应答了 —— 这时候记下 IP
    // 才有意义。连不上的地址不该被记住。
    if (state >= static_cast<int>(ServiceState::Online))
        saveHost(m_currentHost);

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

    // ---- Vision: 刷新「图像识别结果」区域 ----
    //
    // 每次 detection 是一次完整快照 —— 列表整个换掉, 标注图自动刷新。
    //
    // **不进系统事件流** —— 识别结果有自己的展示区 (标注图 + 列表),
    // 再往对话流程里塞一条只是噪音。
    if (source == QStringLiteral("Vision")) {
        const QString type = event.value(QStringLiteral("type")).toString();
        if (type == QStringLiteral("detection")) {
            updateVisionList(event);
        } else if (type == QStringLiteral("error")) {
            if (m_lblVisionStatus) {
                m_lblVisionStatus->setText(
                    QStringLiteral("识别失败: %1")
                        .arg(event.value(QStringLiteral("message")).toString()));
            }
            if (m_btnDetect)
                m_btnDetect->setEnabled(true);
        }
        return;
    }

    // ---- Arm: 不属于对话流程, 归到「系统事件」 ----
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

// ------------------------------------------------------------ 三态动画

void MainWindow::onFastSnapshot(const QString &name, const QJsonObject &json)
{
    if (name != QStringLiteral("ASR"))
        return;
    updateAsrIndicator(json);
}

void MainWindow::onFastFailed(const QString &name, const QString &error)
{
    if (name != QStringLiteral("ASR"))
        return;
    // 错误详情放在 tooltip 里 —— 动画区只有一行, 塞不下长串错误。
    ui->asrIndicator->setToolTip(error);
    ui->asrIndicator->setState(AsrStatusIndicator::State::Offline,
                               QStringLiteral("连不上 ASR"));
}

/// 根据 ASR 的 /health 快照更新三态动画。
///
/// 三态怎么判 (顺序很重要):
///
/// 1. **识别中** —— ``rec_state == "RECORDING"``。
///    ASR 已唤醒, 正在录用户的语音。
/// 2. **识别完成** —— ``rec_result`` 比上一次「新」。
///    ASR 在识别完的那一瞬间就写了这个标记 (不等 Agent) ——
///    所以这里能第一时间显示「识别完成」。
/// 3. **等待唤醒** —— 其余情况。
void MainWindow::updateAsrIndicator(const QJsonObject &snapshot)
{
    if (snapshot.isEmpty())
        return;

    const QString recState = snapshot.value(QStringLiteral("rec_state")).toString();
    const QJsonObject recResult =
        snapshot.value(QStringLiteral("rec_result")).toObject();

    // ---- 「识别完成」的标记 ----
    //
    // rec_result 一直在快照里 (ASR 不清它), 所以光看「有没有 rec_result」
    // 分不出「刚识别完」和「上次会话就识别完了」。
    //
    // 用 (turn, at) 当钥匙, 记住上一次看到的值 —— 值变了才是「刚完成」。
    const QString key = recResult.isEmpty()
        ? QString()
        : QStringLiteral("%1|%2")
              .arg(recResult.value(QStringLiteral("turn")).toInt())
              .arg(recResult.value(QStringLiteral("at")).toDouble(), 0, 'f', 3);

    bool justDone = false;
    if (!key.isEmpty()) {
        if (m_asrPrimed && key != m_lastRecResultKey) {
            // 真的刚完成 (第一次快照除外, 见下)
            justDone = true;
        }
        m_lastRecResultKey = key;
    }
    m_asrPrimed = true;

    if (justDone) {
        const QString text = recResult.value(QStringLiteral("text")).toString();
        m_asrDoneShown = true;
        ui->asrIndicator->setState(
            AsrStatusIndicator::State::Done,
            text.isEmpty() ? QStringLiteral("识别完成") : text);
        return;
    }

    // ---- 「识别完成」的展示期 ----
    //
    // 动画自己会停留 2 秒再淡出回「等待」。这期间**不能**被下一轮
    // 轮询 (250ms 一次) 覆盖掉 —— 否则「识别完成」一闪而过, 根本看不见。
    //
    // 所以这里加一道锁: 展示期内只让「开始新录音」打断它。
    if (m_asrDoneShown) {
        if (recState == QStringLiteral("RECORDING")) {
            // 用户又开始说了 —— 回到「识别中」。
            m_asrDoneShown = false;
            ui->asrIndicator->setState(AsrStatusIndicator::State::Recording,
                                       QStringLiteral("正在录音…"));
            return;
        }
        // 动画已经自己淡回「等待」了 -> 解锁, 继续往下走。
        if (ui->asrIndicator->state() == AsrStatusIndicator::State::Waiting) {
            m_asrDoneShown = false;
        } else {
            return;   // 还在展示「识别完成」, 别动它
        }
    }

    // ---- 识别中? ----
    if (recState == QStringLiteral("RECORDING")) {
        ui->asrIndicator->setState(AsrStatusIndicator::State::Recording,
                                   QStringLiteral("正在录音…"));
        return;
    }

    // ---- 等待唤醒 ----
    //
    // 顺便把流水线状态也带出来 —— 用户想看的是「现在到哪一步了」。
    const QString state = snapshot.value(QStringLiteral("state")).toString();
    QString detail;
    if (state == QStringLiteral("THINK"))
        detail = QStringLiteral("等待 Agent 分流…");
    else if (state == QStringLiteral("SAY"))
        detail = QStringLiteral("正在播报…");
    else
        detail = QStringLiteral("等待唤醒词…");

    ui->asrIndicator->setState(AsrStatusIndicator::State::Waiting, detail);
}

// ------------------------------------------------------------ 图像识别结果

namespace {

/// 把服务端的 ISO 时间戳压成「时分秒」。
///
/// 服务端给的是 ``2026-11-23T17:10:03.123456`` —— 列表里显示全串
/// 太长, 毫秒和日期用不上, 只留时分秒。
///
/// 解析不了就**原样返回** —— 万一服务端换了格式, 至少还能看到
/// 点什么, 总比空着强。
QString formatTimestamp(const QString &iso)
{
    if (iso.isEmpty())
        return QStringLiteral("--:--:--");

    QDateTime dt = QDateTime::fromString(iso, Qt::ISODateWithMs);
    if (!dt.isValid())
        dt = QDateTime::fromString(iso, Qt::ISODate);
    if (!dt.isValid())
        return iso;

    return dt.toString(QStringLiteral("HH:mm:ss"));
}

}  // namespace

void MainWindow::updateVisionList(const QJsonObject &event)
{
    if (!m_listVision)
        return;

    // Vision 的 detection 事件字段是**平铺**的 (不在 payload 里)。
    const QJsonArray objects = event.value(QStringLiteral("objects")).toArray();

    // 每次 detection 是一次**完整快照** —— 整个列表换掉。
    m_listVision->clear();

    if (objects.isEmpty()) {
        m_listVision->addItem(QStringLiteral("(未识别到物体)"));
        if (m_lblVisionStatus)
            m_lblVisionStatus->setText(QStringLiteral("识别完成 · 未发现物体"));
        return;
    }

    for (const QJsonValue &v : objects) {
        const QJsonObject o = v.toObject();

        const int index = o.value(QStringLiteral("index")).toInt();
        const QString label = o.value(QStringLiteral("label")).toString();
        const QString labelZh = o.value(QStringLiteral("label_zh")).toString();
        const double confidence = o.value(QStringLiteral("confidence")).toDouble();
        const QString recordId = o.value(QStringLiteral("record_id")).toString();
        const QString imageFile = o.value(QStringLiteral("image_filename")).toString();
        const QString timestamp = o.value(QStringLiteral("timestamp")).toString();

        // 显示: 序号 + 物体名 + 识别率 + 时间戳
        //
        // 类别名优先用中文 (label_zh), 没有就退回英文 label。
        // 追溯编号和图片路径是内部细节, 不摆在这儿 (那只是噪音) ——
        // 但要看某个目标的单框图, 点一下列表项就行。
        QString text;
        if (index > 0)
            text += QStringLiteral("#%1").arg(index);
        else
            text += QStringLiteral("#-");
        const QString name = labelZh.isEmpty() ? label : labelZh;
        if (!name.isEmpty())
            text += QStringLiteral(" %1").arg(name);
        if (confidence > 0.0)
            text += QStringLiteral(" %1%").arg(confidence * 100, 0, 'f', 0);
        text += QStringLiteral(" %1").arg(formatTimestamp(timestamp));

        auto *item = new QListWidgetItem(text, m_listVision);
        // 原始数据挂在 item 上 —— 点开大图时要用。
        item->setData(Qt::UserRole, recordId);
        item->setData(Qt::UserRole + 1, imageFile);
        item->setData(Qt::UserRole + 2, o.value(QStringLiteral("bbox")));
        // tooltip 里补上英文类名 —— 中文名是翻译过的, 想看模型
        // 原始输出时能对照。
        if (!label.isEmpty() && label != name) {
            item->setToolTip(QStringLiteral("%1 (%2) · 识别率 %3%")
                                 .arg(name, label)
                                 .arg(confidence * 100, 0, 'f', 0));
        }
    }

    // ---- 状态提示 ----
    if (m_lblVisionStatus) {
        m_lblVisionStatus->setText(
            QStringLiteral("识别完成 · %1 个目标").arg(objects.size()));
    }

    // ---- 自动显示标注图 ----
    //
    // **不再要点一下才出图** —— 识别完直接把那张画了所有框的
    // 整图拉下来缩放显示。
    //
    // 优先用整图 (`annotated_image`); 服务端没有 (老版本) 就退回
    // 用第一个物体的那张单框图。
    const QString annotated = event.value(QStringLiteral("annotated_image")).toString();
    if (!annotated.isEmpty()) {
        showVisionImage(annotated);
    } else if (!objects.isEmpty()) {
        const QString fallback =
            objects.first().toObject().value(QStringLiteral("image_filename")).toString();
        if (!fallback.isEmpty())
            showVisionImage(fallback);
    }
}

void MainWindow::showVisionImage(const QString &imageFile)
{
    if (imageFile.isEmpty() || !m_imageNam || !m_lblVisionImage)
        return;

    // 视觉服务的追溯图像接口: /v1/traces/image/<相对路径>
    const QString url = QStringLiteral("http://%1:%2/v1/traces/image/%3")
                            .arg(m_visionHost)
                            .arg(m_visionPort)
                            .arg(QUrl::toPercentEncoding(imageFile));

    m_lblVisionImage->setText(QStringLiteral("正在加载…"));

    QNetworkRequest req{QUrl(url)};
    req.setTransferTimeout(8000);
    QNetworkReply *reply = m_imageNam->get(req);
    connect(reply, &QNetworkReply::finished, this, [this, reply] {
        reply->deleteLater();
        if (reply->error() != QNetworkReply::NoError) {
            m_lblVisionImage->setPixmap(QPixmap());
            m_lblVisionImage->setText(
                QStringLiteral("加载失败: %1").arg(reply->errorString()));
            return;
        }
        QPixmap pix;
        if (!pix.loadFromData(reply->readAll())) {
            m_lblVisionImage->setPixmap(QPixmap());
            m_lblVisionImage->setText(QStringLiteral("图像解码失败"));
            return;
        }
        // 缩到预览区大小 —— 别让一张大图把布局撑开。
        //
        // 宽度按**实际控件宽度**算 (不是当前 width, 那时候布局
        // 可能还没算完), 高度留个上限。
        const int availW = qMax(120, m_lblVisionImage->width() - 8);
        const int availH = qMax(120, m_lblVisionImage->height() - 8);
        m_lblVisionImage->setPixmap(pix.scaled(
            availW, availH, Qt::KeepAspectRatio, Qt::SmoothTransformation));
    });
}

void MainWindow::onVisionItemClicked(QListWidgetItem *item)
{
    if (!item)
        return;

    const QString imageFile = item->data(Qt::UserRole + 1).toString();
    if (imageFile.isEmpty()) {
        if (m_lblVisionImage) {
            m_lblVisionImage->setPixmap(QPixmap());
            m_lblVisionImage->setText(QStringLiteral("(该结果没有保存图像)"));
        }
        return;
    }

    showVisionImage(imageFile);
}

void MainWindow::onDetectClicked()
{
    if (!m_visionClient)
        return;

    if (m_btnDetect)
        m_btnDetect->setEnabled(false);
    if (m_lblVisionStatus)
        m_lblVisionStatus->setText(QStringLiteral("识别中…"));

    // 结果走 WebSocket 广播回来 (所有订阅者都看得到同一帧)。
    // HTTP 这条路只是「把这次检测跑起来」。
    m_visionClient->detect();
}

QString MainWindow::summarize(const QString &source, const QJsonObject &event)
{
    const QString type = event.value(QStringLiteral("type")).toString();
    const QJsonObject payload = event.value(QStringLiteral("payload")).toObject();

    // ---- 取字段: 平铺 or 嵌在 payload 里 ----
    //
    // 三种服务的信封格式不一样:
    //   - Agent:  ``{"type":..., "payload": {...}}``      (嵌在 payload 里)
    //   - Arm:    ``{"type":"state", "phase":..., ...}``  (平铺)
    //   - Vision: ``{"type":"detection", "objects":[...]}`` (平铺)
    //
    // 之前 Arm/Vision 都按 payload 读 -> 永远读到空对象,
    // 界面上一直显示「检测到 0 个物体」。这里两种都支持。
    const auto field = [&event, &payload](const QString &key) -> QJsonValue {
        if (event.contains(key))
            return event.value(key);
        return payload.value(key);
    };

    if (source == QStringLiteral("Arm")) {
        if (type == QStringLiteral("state"))
            return QStringLiteral("%1 | %2")
                .arg(field(QStringLiteral("phase")).toString(),
                     field(QStringLiteral("detail")).toString());
        if (type == QStringLiteral("finished"))
            return QStringLiteral("动作播放结束");
        if (type == QStringLiteral("error"))
            return field(QStringLiteral("message")).toString();
        return QString();
    }

    if (source == QStringLiteral("Vision")) {
        if (type == QStringLiteral("detection")) {
            const QJsonArray objects = field(QStringLiteral("objects")).toArray();
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
                .arg(field(QStringLiteral("latency_ms")).toInt());
        }
        if (type == QStringLiteral("error"))
            return field(QStringLiteral("message")).toString();
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

    // ---- 服务详情弹窗 ----
    //
    // 自己搭而不是用 QMessageBox: 后者的「详细信息」是个折叠区, 原始
    // JSON 塞在里面既不好看 (比例字体会歪) 又不好读 (长行横向滚动)。
    auto *dlg = new ServiceDetailDialog(this);
    dlg->setAttribute(Qt::WA_DeleteOnClose);
    dlg->setService(status->name, status->host, status->port,
                    status->stateText(),
                    serviceStateColor(status->state),
                    status->latencyMs, status->detail, status->lastError,
                    status->statusJson);
    dlg->showFor(this);
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
