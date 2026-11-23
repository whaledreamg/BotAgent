#pragma once

#include <QColor>
#include <QString>
#include <QWidget>

class QTimer;

/// 语音识别状态指示器 —— 三态动画。
///
/// 三种状态, 各有一套动画:
///
///   Waiting   等待唤醒词   —— 灰色圆环缓慢呼吸 (周期 ~2s)
///   Recording 正在识别     —— 红色声波扩散   (周期 ~1.2s)
///   Done      识别完成     —— 绿色对勾, 短暂停留后淡出
///
/// 动画用 QPainter 现画 —— 不依赖图片资源, 桌面端与 Android 上
/// 表现完全一致 (高 DPI 也自动适配)。
class AsrStatusIndicator : public QWidget
{
    Q_OBJECT

public:
    /// 语音识别的三个阶段。
    enum class State {
        Waiting,   ///< 还没触发唤醒词 (在等)
        Recording, ///< 已唤醒, 正在识别
        Done,      ///< 识别完成
        Offline,   ///< 服务没连上
    };
    Q_ENUM(State)

    explicit AsrStatusIndicator(QWidget *parent = nullptr);
    ~AsrStatusIndicator() override;

    /// 切换状态。
    ///
    /// ``detail`` 是状态下面的一行说明 (如「正在录音…」)。
    void setState(State state, const QString &detail = QString());

    /// 当前状态。
    State state() const { return m_state; }

    /// 状态说明文字。
    QString detail() const { return m_detail; }

    /// 「识别完成」态停留多久后自动回到「等待」。
    ///
    /// 默认 2000ms —— 给用户看一眼「识别完了」的时间, 然后回到等待态
    /// (因为识别完就该等下一次唤醒了)。
    void setDoneHoldMs(int ms) { m_doneHoldMs = ms; }

    QSize sizeHint() const override;

protected:
    void paintEvent(QPaintEvent *event) override;

private slots:
    /// 动画心跳 —— 重绘。
    void onTick();

private:
    /// 画「等待唤醒词」的呼吸圆环。
    void paintWaiting(QPainter &p, const QRectF &area);
    /// 画「正在识别」的声波扩散。
    void paintRecording(QPainter &p, const QRectF &area);
    /// 画「识别完成」的对勾。
    void paintDone(QPainter &p, const QRectF &area);
    /// 画「服务没连上」的灰叉。
    void paintOffline(QPainter &p, const QRectF &area);
    /// 状态文字 + 说明文字。
    void paintText(QPainter &p, const QRectF &area);

    /// 当前状态。
    State m_state = State::Waiting;
    /// 状态说明文字。
    QString m_detail;

    /// 动画计时器 (固定 ~60fps)。
    QTimer *m_timer = nullptr;
    /// 动画已走过的毫秒数。
    qint64 m_elapsedMs = 0;
    /// 「识别完成」态已经停留了多久。
    qint64 m_doneElapsedMs = 0;
    /// 「识别完成」态停留多久后回到等待。
    int m_doneHoldMs = 2000;
};
