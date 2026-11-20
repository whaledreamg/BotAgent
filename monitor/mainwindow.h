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

class FlowCard;
class FlowPanel;
class MonitorModel;
class ServiceBar;
class ServicePoller;
class ThinkingModel;
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

private:
    /// 读配置并把服务名灌进模型。
    void applyConfig();
    /// 把「监控 IP」输入框里的地址套用到所有服务与 WebSocket 上。
    void applyHost();
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

    Ui::MainWindow *ui;

    // ---- 模型 ----
    MonitorModel *m_monitorModel = nullptr;
    ThinkingModel *m_thinkingModel = nullptr;

    // ---- 数据源 ----
    ServicePoller *m_poller = nullptr;
    WsClient *m_wsClient = nullptr;

    // ---- 视图 ----
    ServiceBar *m_serviceBar = nullptr;

    // ---- 原始配置 (config.json 里的服务/WS 定义) ----
    //
    /// 每次「开始监控」都以它为底, 重新套用界面上填的目标 IP。
    QVector<ServiceConfig> m_baseServices;
    QVector<WsConfig> m_baseWsSources;

    /// 当前实际生效的目标 IP (最后一次 applyHost 时用的)。
    QString m_currentHost;

    /// WebSocket 源的名字 (状态栏显示用)
    QStringList m_wsNames;
    /// 本次运行是否已经经历过一轮对话。
    ///
    /// 启动时 ASR 的 /health 里存着「上一轮」的残留, 那不是本次运行
    /// 发生的对话 —— 在收到 turn_start 之前不建卡。
    bool m_turnStarted = false;
    /// 启动时记下的 ASR 历史文本 (基线)
    QString m_asrBaselineText;
};

#endif  // MAINWINDOW_H
