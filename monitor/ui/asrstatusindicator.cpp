#include "ui/asrstatusindicator.h"

#include <QFontMetrics>
#include <QPainter>
#include <QPainterPath>
#include <QTimer>

namespace {

/// 动画帧间隔 (毫秒) —— ~60fps。
constexpr int kTickMs = 16;

/// 等待态的呼吸周期 (毫秒)。
constexpr int kWaitingCycleMs = 2000;

/// 识别态的声波周期 (毫秒)。
constexpr int kRecordingCycleMs = 1200;

/// 识别态同时画几圈声波。
constexpr int kRippleCount = 3;

/// 配色 —— 与整体主题 (theme_light.qss) 保持一致。
const QColor kColorWaiting(154, 158, 168);   ///< 灰蓝
const QColor kColorRecording(224, 83, 77);   ///< 红
const QColor kColorDone(46, 204, 113);       ///< 绿
const QColor kColorOffline(158, 158, 158);   ///< 灰
const QColor kColorText(52, 58, 72);         ///< 主文字
const QColor kColorSubText(138, 147, 166);   ///< 次要文字

/// 缓动: 0~1 的三角波 —— 用来做呼吸/扩散的节奏。
///
/// 比线性自然 —— 线性看起来像机械的匀速摆动, 三角波有「涨-落」的
/// 呼吸感。
qreal triangle(qreal t)
{
    t = std::fmod(t, 1.0);
    if (t < 0.0)
        t += 1.0;
    return t < 0.5 ? t * 2.0 : (1.0 - t) * 2.0;
}

}  // namespace

AsrStatusIndicator::AsrStatusIndicator(QWidget *parent)
    : QWidget(parent)
{
    setObjectName(QStringLiteral("asrStatusIndicator"));
    setMinimumSize(160, 96);
    setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Fixed);

    m_timer = new QTimer(this);
    m_timer->setInterval(kTickMs);
    connect(m_timer, &QTimer::timeout, this, &AsrStatusIndicator::onTick);
    m_timer->start();
}

AsrStatusIndicator::~AsrStatusIndicator()
{
    if (m_timer)
        m_timer->stop();
}

void AsrStatusIndicator::setState(State state, const QString &detail)
{
    // 状态没变且说明也没变 —— 不用重绘。
    if (m_state == state && m_detail == detail)
        return;

    // 从别的状态切到「识别完成」时, 重新计时 (决定停留多久)。
    if (state == State::Done && m_state != State::Done)
        m_doneElapsedMs = 0;

    m_state = state;
    m_detail = detail;
    update();
}

QSize AsrStatusIndicator::sizeHint() const
{
    return QSize(160, 96);
}

void AsrStatusIndicator::onTick()
{
    m_elapsedMs += kTickMs;

    // 「识别完成」停留够了 -> 回到等待态。
    if (m_state == State::Done) {
        m_doneElapsedMs += kTickMs;
        if (m_doneElapsedMs >= m_doneHoldMs) {
            m_state = State::Waiting;
            m_doneElapsedMs = 0;
            m_detail.clear();
        }
    }

    update();
}

void AsrStatusIndicator::paintEvent(QPaintEvent *)
{
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing, true);

    // ---- 动画区 (上半) ----
    //
    // 圆形动画画在左侧, 文字在右侧 —— 竖着排的话在窄屏上太挤。
    const int side = qMin(width(), height() * 2 / 3);
    const qreal d = qMin(side, 64);

    const QRectF animArea(6, (height() - d) / 2.0, d, d);
    const QRectF textArea(animArea.right() + 10, 0,
                          width() - animArea.right() - 16, height());

    switch (m_state) {
    case State::Waiting:
        paintWaiting(p, animArea);
        break;
    case State::Recording:
        paintRecording(p, animArea);
        break;
    case State::Done:
        paintDone(p, animArea);
        break;
    case State::Offline:
        paintOffline(p, animArea);
        break;
    }

    paintText(p, textArea);
}

void AsrStatusIndicator::paintWaiting(QPainter &p, const QRectF &area)
{
    // 灰色圆环 + 呼吸的「呼吸点」。
    //
    // 呼吸点沿着圆环走 —— 让人一眼看出「还在等, 没死」。
    const QPointF center = area.center();
    const qreal radius = area.width() / 2.0 - 5.0;

    p.setPen(QPen(QColor(kColorWaiting.red(), kColorWaiting.green(),
                         kColorWaiting.blue(), 60),
                   2.0));
    p.setBrush(Qt::NoBrush);
    p.drawEllipse(center, radius, radius);

    // 呼吸点: 沿圆周滑动的亮点。
    const qreal phase = triangle(m_elapsedMs / kWaitingCycleMs);
    const qreal angle = phase * 2.0 * M_PI;
    const QPointF dot(center.x() + radius * std::cos(angle),
                       center.y() + radius * std::sin(angle));

    // 亮度随呼吸变化。
    const int alpha = 90 + static_cast<int>(165 * phase);
    p.setPen(Qt::NoPen);
    p.setBrush(QColor(kColorWaiting.red(), kColorWaiting.green(),
                      kColorWaiting.blue(), alpha));
    p.drawEllipse(dot, 4.0, 4.0);

    // 中心一个淡点, 让图形有「实体感」。
    p.setBrush(QColor(kColorWaiting.red(), kColorWaiting.green(),
                      kColorWaiting.blue(), 45));
    p.drawEllipse(center, radius * 0.42, radius * 0.42);
}

void AsrStatusIndicator::paintRecording(QPainter &p, const QRectF &area)
{
    // 红色声波 —— 同心圆从里往外扩散, 循环往复。
    //
    // 3 圈错相位, 所以看起来是连续的波, 而不是一环一环地跳。
    const QPointF center = area.center();
    const qreal maxR = area.width() / 2.0 - 3.0;

    p.setBrush(Qt::NoBrush);
    for (int i = 0; i < kRippleCount; ++i) {
        // 每圈错开 1/kRippleCount 个周期。
        const qreal phase = triangle(m_elapsedMs / kRecordingCycleMs
                                     + static_cast<qreal>(i) / kRippleCount);
        const qreal r = maxR * (0.35 + 0.65 * phase);
        const int alpha = static_cast<int>(230 * (1.0 - phase) + 30);

        p.setPen(QPen(QColor(kColorRecording.red(), kColorRecording.green(),
                             kColorRecording.blue(), alpha),
                       2.5));
        p.drawEllipse(center, r, r);
    }

    // 中心实心圆 —— 声源。
    const qreal beat = 0.85 + 0.15 * triangle(m_elapsedMs / 400.0);
    p.setPen(Qt::NoPen);
    p.setBrush(kColorRecording);
    p.drawEllipse(center, maxR * 0.24 * beat, maxR * 0.24 * beat);
}

void AsrStatusIndicator::paintDone(QPainter &p, const QRectF &area)
{
    // 绿色对勾 —— 画完停留一会儿, 然后淡出回到等待态。
    const QPointF center = area.center();
    const qreal r = area.width() / 2.0 - 8.0;

    // 淡出: 停留时间过半后开始渐隐。
    qreal opacity = 1.0;
    if (m_doneHoldMs > 0) {
        const qreal t = static_cast<qreal>(m_doneElapsedMs) / m_doneHoldMs;
        if (t > 0.6)
            opacity = 1.0 - (t - 0.6) / 0.4;
    }
    opacity = qBound(0.0, opacity, 1.0);

    p.setPen(QPen(QColor(kColorDone.red(), kColorDone.green(),
                         kColorDone.blue(),
                         static_cast<int>(255 * opacity)),
                   3.0, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    p.setBrush(Qt::NoBrush);

    // 对勾的两段。
    QPainterPath path;
    path.moveTo(center.x() - r * 0.55, center.y());
    path.lineTo(center.x() - r * 0.14, center.y() + r * 0.42);
    path.lineTo(center.x() + r * 0.58, center.y() - r * 0.42);
    p.drawPath(path);

    // 外面套一圈淡绿 —— 强调「完成」。
    p.setPen(QPen(QColor(kColorDone.red(), kColorDone.green(),
                         kColorDone.blue(),
                         static_cast<int>(70 * opacity)),
                   2.0));
    p.drawEllipse(center, r, r);
}

void AsrStatusIndicator::paintOffline(QPainter &p, const QRectF &area)
{
    // 灰色叉 —— 服务没连上。
    const QPointF center = area.center();
    const qreal r = area.width() / 2.0 - 8.0;

    p.setPen(QPen(kColorOffline, 2.5, Qt::SolidLine, Qt::RoundCap));
    p.setBrush(Qt::NoBrush);
    p.drawLine(center.x() - r * 0.5, center.y() - r * 0.5,
               center.x() + r * 0.5, center.y() + r * 0.5);
    p.drawLine(center.x() + r * 0.5, center.y() - r * 0.5,
               center.x() - r * 0.5, center.y() + r * 0.5);
}

void AsrStatusIndicator::paintText(QPainter &p, const QRectF &area)
{
    QString title;
    QColor color;
    switch (m_state) {
    case State::Waiting:
        title = QStringLiteral("等待唤醒");
        color = kColorText;
        break;
    case State::Recording:
        title = QStringLiteral("识别中");
        color = kColorRecording;
        break;
    case State::Done:
        title = QStringLiteral("识别完成");
        color = kColorDone;
        break;
    case State::Offline:
        title = QStringLiteral("未连接");
        color = kColorOffline;
        break;
    }

    QFont titleFont = font();
    titleFont.setPointSizeF(qMax(9.0, titleFont.pointSizeF()));
    titleFont.setBold(true);
    p.setFont(titleFont);
    p.setPen(color);

    const QFontMetrics fm(titleFont);
    const QRectF titleRect(area.left(), area.top(), area.width(),
                           fm.height());
    p.drawText(titleRect, Qt::AlignLeft | Qt::AlignVCenter, title);

    // ---- 说明文字 ----
    if (m_detail.isEmpty())
        return;

    QFont subFont = font();
    subFont.setPointSizeF(qMax(7.5, subFont.pointSizeF() - 1.0));
    p.setFont(subFont);
    p.setPen(kColorSubText);

    const QFontMetrics subFm(subFont);
    const QRectF subRect(area.left(), titleRect.bottom() + 2, area.width(),
                          subFm.height());
    // 太长就省略 —— 详情在下面的列表里有, 这里放不下就截断。
    p.drawText(subRect, Qt::AlignLeft | Qt::AlignVCenter,
               subFm.elidedText(m_detail, Qt::ElideRight, int(area.width())));
}
