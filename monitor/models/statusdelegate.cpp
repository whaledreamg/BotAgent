#include "statusdelegate.h"

#include "monitormodel.h"

#include <QPainter>

StatusDelegate::StatusDelegate(QObject *parent)
    : QStyledItemDelegate(parent)
{
}

void StatusDelegate::paint(QPainter *painter, const QStyleOptionViewItem &option,
                            const QModelIndex &index) const
{
    const QString text = index.data(Qt::DisplayRole).toString();
    // 状态枚举通过 UserRole 传过来 (见 MonitorModel::data)
    const int stateRole = index.data(Qt::UserRole).toInt();

    painter->save();

    // 背景 (选中态)
    if (option.state & QStyle::State_Selected) {
        painter->fillRect(option.rect, option.palette.highlight());
        painter->setPen(option.palette.highlightedText().color());
    } else {
        painter->setPen(option.palette.text().color());
    }

    // 圆点
    static const QColor kOnline(0x2e, 0xcc, 0x71);
    static const QColor kDegraded(0xf1, 0xc4, 0x0f);
    static const QColor kOffline(0xe0, 0x53, 0x4d);
    static const QColor kUnknown(0x9e, 0x9e, 0x9e);

    QColor color = kUnknown;
    switch (static_cast<ServiceState>(stateRole)) {
    case ServiceState::Online:
        color = kOnline;
        break;
    case ServiceState::Degraded:
        color = kDegraded;
        break;
    case ServiceState::Offline:
        color = kOffline;
        break;
    case ServiceState::Unknown:
    default:
        color = kUnknown;
        break;
    }

    const int d = 10;
    const QRect dotRect(option.rect.left() + 6,
                        option.rect.center().y() - d / 2,
                        d, d);
    painter->setBrush(color);
    painter->setPen(QPen(color.darker(140), 1));
    painter->drawEllipse(dotRect);

    // 文字
    QRect textRect = option.rect;
    textRect.setLeft(dotRect.right() + 6);
    painter->drawText(textRect, Qt::AlignLeft | Qt::AlignVCenter, text);

    painter->restore();
}

QSize StatusDelegate::sizeHint(const QStyleOptionViewItem &option,
                                const QModelIndex &index) const
{
    QSize size = QStyledItemDelegate::sizeHint(option, index);
    size.setWidth(size.width() + 24);   // 给圆点留位置
    return size;
}
