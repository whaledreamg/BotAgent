#ifndef MAINWINDOW_H
#define MAINWINDOW_H

#include <QMainWindow>
#include <QJsonObject>
#include <QStringList>
#include <QVector>

#include "core/appconfig.h"

QT_BEGIN_NAMESPACE
namespace Ui { class MainWindow; }
QT_END_NAMESPACE

class QEvent;
class QKeyEvent;
class QListWidgetItem;

class AgentClient;
class FlowCard;
class FlowPanel;
class MonitorModel;
class ServiceBar;
class ServicePoller;
class ThinkingModel;
class VisionClient;
class WsClient;

/// BotAgent 监控主窗口。
///
/// 布局 (全部在 mainwindow.ui 里搭好):
///   上: 工具条 (开始/暂停监控、间隔、清空)
///   中: 两栏 —— 对话流程 (ASR 识别 -> Agent 分流 -> Agent 回复 -> 耗时)
///        与 Agent 思考过程 (每一轮的 trace / reasoning / 耗时)
///   底: 服务状态条 (五个服务的连接状态胶囊) + WS 状态 + 轮询次数
///
/// **只读** —— 不下发任何控制指令。
class MainWindow : public QMainWindow
{
    Q_OBJECT

public:
    explicit MainWindow(QWidget *parent = nullptr);
    ~MainWindow() override;

public slots:
    /// 开始监控 (轮询 + WebSocket 订阅)。
    void startMonitoring();
    /// 停止监控。
    void stopMonitoring();
    /// 开始/停止监控切换。
    void toggleMonitoring();
    /// 当前是否在监控。
    bool isMonitoring() const;

signals:
    /// 监控开关变化。
    void monitoringChanged(bool on);

protected:
    void closeEvent(QCloseEvent *event) override;
    /// 事件过滤器 —— 指令输入框里 Enter 发送 / Shift+Enter 换行。
    bool eventFilter(QObject *watched, QEvent *event) override;
    /// 窗口尺寸变化 —— 在「窄屏」与「宽屏」两种布局之间切换。
    bool event(QEvent *event) override;

private slots:
    // ===== 顶部工具条 =====
    void onToggleMonitoring();
    void onClearEvents();
    void onClearThinking();
    void onIntervalChanged(int ms);
    /// IP 输入框回车 —— 立刻按新地址重新开始监控。
    void onHostReturnPressed();
    /// IP 输入框失焦 —— 提示需要重新开始监控才生效。
    void onHostEditingFinished();

    // ===== 手动指令 =====
    /// 发送输入框里的指令到 Agent。
    void onCommandSend();
    /// Agent 同步返回了结果 (WS 还没到时用它兜底回填)。
    void onDispatched(const QJsonObject &result);
    /// 指令发送失败。
    void onDispatchFailed(const QString &error);
    /// 输入框内容变化 —— 控制发送按钮的可用状态。
    void onCommandTextChanged();

    // ===== 数据源回调 =====
    void onServiceUpdated(const QString &name, int state, int latencyMs,
                          const QString &detail, const QString &rawStatus,
                          const QJsonObject &statusJson);
    void onServiceFailed(const QString &name, const QString &error);
    void onEventReceived(const QString &source, const QJsonObject &event);
    void onConnectionChanged(const QString &name, bool connected,
                             const QString &error);
    void onPollCountChanged(int count);
    /// 点击底部服务胶囊 -> 弹原始 JSON 详情。
    void onServiceBarClicked(const QString &name);

    // ===== 快速轮询 (语音识别三态动画) =====
    /// ASR 的 /health 高频快照 -> 更新三态动画。
    void onFastSnapshot(const QString &name, const QJsonObject &json);
    /// 快速轮询失败 -> 动画切到「未连接」。
    void onFastFailed(const QString &name, const QString &error);

private:
    /// 读配置并把服务名灌进模型。
    void applyConfig();
    /// 把「监控 IP」输入框里的地址套用到所有服务与 WebSocket 上。
    void applyHost();
    /// 把当前 IP **持久化** 到 config.json。
    ///
    /// 只在**真正连上之后**调 —— 连不上就别存, 否则下次启动会指向
    /// 一个连不上的地址。
    void saveHost(const QString &host);
    /// 监控总开关 —— 统一起停轮询与 WebSocket, 并同步 UI。
    void setMonitoring(bool on);
    /// 找到「本轮」对应的卡片; 没有就新建一张。
    ///
    /// 文本一致 -> 复用同一张卡, 保证 ASR 和 Agent 的数据落在同一处。
    FlowCard *findOrCreateCard(const QString &text);
    /// 从 ASR 的 /health 快照里取「最近一轮」, 更新流程卡片。
    void updateFromAsrSnapshot(const QJsonObject &snapshot);
    /// 把 action JSON 概括成一行文字 (如 "wave" 或 "grab, move")。
    static QString describeAction(const QJsonObject &action);
    /// 生成系统事件摘要 (Arm / Vision)。
    static QString summarize(const QString &source, const QJsonObject &event);
    /// 刷新顶部的状态总览标签。
    void updateOverview();
    /// 刷新底部状态栏的 WS 连接状态。
    void updateWsStatus();

    // ===== 语音识别三态动画 =====

    /// 根据 ASR 快照更新三态动画。
    ///
    /// 三态怎么判:
    ///   - **识别中**: ``rec_state == "RECORDING"`` (已唤醒, 在录音)
    ///   - **识别完成**: ``rec_result`` 刚更新过 (识别那一刻就写进来了)
    ///   - **等待唤醒**: 其余情况
    void updateAsrIndicator(const QJsonObject &snapshot);

    /// 根据 Vision 的 detection 事件刷新「图像识别结果」区域。
    ///
    /// 每次 detection 是一次**完整快照** (不是增量) —— 所以整个列表替换掉。
    void updateVisionList(const QJsonObject &event);

    /// 拉取并显示标注图 (缩放到预览区大小)。
    ///
    /// @param imageFile  相对 trace_root 的图片路径 (如 ``20261123_101010_123/_annotated.jpg``)
    void showVisionImage(const QString &imageFile);

    /// 点图像识别结果 -> 去视觉服务拉那张标注图并显示。
    void onVisionItemClicked(QListWidgetItem *item);

    /// 点「识别一次」-> 触发一次视觉检测。
    void onDetectClicked();

    // ===== 平板适配 =====
    ///
    /// 平板横屏是主要目标 (Android 端), 桌面端沿用同一套布局。
    ///
    /// 与桌面端的唯一差别是**触摸目标尺寸** —— 手指比鼠标粗,
    /// 按钮/输入框在平板上要放大到 44dp 才点得准。
    ///
    /// 判据是窗口宽度 (`kMinTabletWidth`) —— 平板横屏一定够宽。
    void applyTouchSizing();
    /// 折叠旧的思考轮次 —— 只留最新一轮展开。
    ///
    /// 思考树是倒叙的 (最新一轮在顶部)。新的一轮进来时, 上一轮
    /// 还铺着大片屏幕就太吵了 —— 这里把除最新一轮之外的顶层节点
    /// 全部收起, 想看细节再手动点开。
    void collapseOldThinkingTurns();
    /// 展开最新一轮里的各个「阶段」 (第二层)。
    ///
    /// 要在 turn_end 时调 —— 阶段是随 thinking / turn_end 事件
    /// 陆续插进来的, turn_start 时还没到。
    void expandLatestTurnSections();

    Ui::MainWindow *ui;

    // ---- 模型 ----
    MonitorModel *m_monitorModel = nullptr;
    ThinkingModel *m_thinkingModel = nullptr;

    // ---- 数据源 ----
    ServicePoller *m_poller = nullptr;
    WsClient *m_wsClient = nullptr;
    /// 手动指令客户端 (POST /v1/dispatch)。
    AgentClient *m_agentClient = nullptr;
    /// 视觉服务客户端 (POST /v1/detect) —— 按需触发识别。
    VisionClient *m_visionClient = nullptr;

    // ---- 视图 ----
    ServiceBar *m_serviceBar = nullptr;

    // ---- 原始配置 (config.json 里的服务/WS 定义) ----
    //
    /// 每次「开始监控」都以它为底, 重新套用界面上填的目标 IP。
    QVector<ServiceConfig> m_baseServices;
    QVector<WsConfig> m_baseWsSources;

    /// 当前实际生效的目标 IP (最后一次 applyHost 时用的)。
    QString m_currentHost;

    /// 当前 IP 是否已经**存进 config.json** 了。
    ///
    /// 连上之后才存一次 —— 每次连接只存一次, 别每轮询一次写一遍盘。
    bool m_hostSaved = false;

    /// 手动指令用的 session id (默认 monitor-ui)。
    QString m_agentSessionId;

    /// 有手动指令正在飞行中。
    ///
    /// 这期间**跳过 ASR 快照建卡** —— 否则 ASR 的「最近一轮」会跟
    /// 手动指令抢同一张卡片。
    bool m_manualPending = false;

    /// 当前手动指令的文本 —— 用来在 WS 事件里认出「这是手动那一轮」。
    QString m_manualText;

    /// WebSocket 源的名字 (状态栏显示用)
    QStringList m_wsNames;

    /// 上一次看到的「识别完成」标记 (``rec_result`` 的 turn + at)。
    ///
    /// 用来判断「这一轮是不是刚识别完」—— ``rec_result`` 一直在快照里,
    /// 不记住上一次的值就没法区分「刚完成」和「早就完成了」。
    QString m_lastRecResultKey;

    /// 已经收到过至少一份 ASR 快照了吗?
    ///
    /// 第一份快照里的 ``rec_result`` 可能是**上一次会话**留下的 ——
    /// 那不该触发「识别完成」动画 (会让人以为刚刚说过话)。
    bool m_asrPrimed = false;

    /// 「识别完成」动画正在展示中。
    ///
    /// 动画会停留 2 秒才淡出, 这期间每 250ms 来一份快照 ——
    /// 不加这把锁, 「识别完成」会被下一份快照立刻覆盖, 一闪而过。
    bool m_asrDoneShown = false;

    /// 当前生效的目标 IP —— 拉视觉标注图时要用 (拼 URL)。
    QString m_visionHost;
    /// 视觉服务端口。
    int m_visionPort = 8803;

    /// 拉图像用的网络管理器 (跟 ServicePoller 的分开 —— 那边是轮询用的)。
    class QNetworkAccessManager *m_imageNam = nullptr;
    /// 显示标注图的标签页。
    class QLabel *m_lblVisionImage = nullptr;
    /// 「图像识别结果」列表。
    class QListWidget *m_listVision = nullptr;
    /// 识别状态提示 (识别中 / 成功 / 失败)。
    class QLabel *m_lblVisionStatus = nullptr;
    /// 「识别一次」按钮。
    class QPushButton *m_btnDetect = nullptr;
    /// 本次运行是否已经经历过一轮对话。
    ///
    /// 启动时 ASR 的 /health 里存着「上一轮」的残留, 那不是本次运行
    /// 发生的对话 —— 在收到 turn_start 之前不建卡。
    bool m_turnStarted = false;
    /// 启动时记下的 ASR 历史文本 (基线)
    QString m_asrBaselineText;

    // ===== 平板适配 =====
};

#endif  // MAINWINDOW_H
