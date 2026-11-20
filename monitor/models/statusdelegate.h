#pragma once

#include <QStyledItemDelegate>

/// 状态列的绘制委托: 在单元格里画一个彩色圆点 + 文字。
///
/// 颜色:
///   绿 = 正常, 黄 = 降级, 红 = 断开, 灰 = 未知
class StatusDelegate : public QStyledItemDelegate {
    Q_OBJECT

public:
    explicit StatusDelegate(QObject *parent = nullptr);

    void paint(QPainter *painter, const QStyleOptionViewItem &option,
               const QModelIndex &index) const override;
    QSize sizeHint(const QStyleOptionViewItem &option,
                   const QModelIndex &index) const override;
};
