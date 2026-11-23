#pragma once

#include <QColor>
#include <QJsonObject>
#include <QString>
#include <QWidget>

class QLabel;
class QTextBrowser;
class QDialogButtonBox;

/// 服务详情对话框 —— 点胶囊后弹出来的那块。
///
/// **为什么不用 QMessageBox**: 它的「详细信息」是个折叠区, 原始 JSON
/// 塞在里面既不好看 (比例字体会歪) 又不好读 (长行横向滚动、单色)。
/// 这里自己搭:
///   - 顶部: 状态色点 + 服务名 + 地址 (一眼看清)
///   - 中间: 关键字段两列对齐 (状态 / 延迟 / 说明 / 错误)
///   - 底部: 原始 JSON —— 等宽字体 + 深色底 + 分色, 长行自动换行
///
/// 尺寸固定 780x520 (宽高比 3:2), JSON 区至少 220px 高。
class ServiceDetailDialog : public QWidget
{
    Q_OBJECT

public:
    explicit ServiceDetailDialog(QWidget *parent = nullptr);
    ~ServiceDetailDialog() override;

    /// 填入某个服务的信息。
    ///
    /// Args:
    ///     name:      服务名 (如 "Agent")
    ///     host:      地址主机名
    ///     port:      端口
    ///     stateText: 状态文案 (如 "正常" / "断开")
    ///     stateColor: 状态颜色 (状态点用的)
    ///     latencyMs: 延迟 (毫秒), -1 表示未知
    ///     detail:    一句话说明
    ///     lastError: 最近一次错误 (空 = 没有)
    ///     statusJson: 原始状态 JSON (空 = 没有)
    void setService(const QString &name, const QString &host, int port,
                    const QString &stateText, const QColor &stateColor,
                    int latencyMs, const QString &detail,
                    const QString &lastError, const QJsonObject &statusJson);

    /// 显示 (居中于父窗口)。
    void showFor(QWidget *parent);

private:
    /// 把 JSON 排成带缩进的 HTML —— 深色底 + 等宽字体 + 分色。
    QString _jsonToHtml(const QJsonObject &obj) const;

    /// 给一行 JSON 上色 (键名 / 值分色)。
    QString _highlightLine(const QString &line) const;

    /// HTML 转义 (& < >)。
    static QString _escape(const QString &text);

    QLabel *m_badge = nullptr;
    QLabel *m_lblName = nullptr;
    QLabel *m_lblAddr = nullptr;
    QLabel *m_lblState = nullptr;
    QLabel *m_lblLatency = nullptr;
    QLabel *m_lblDetail = nullptr;
    QLabel *m_lblError = nullptr;
    QLabel *m_lblJsonTitle = nullptr;
    QTextBrowser *m_jsonView = nullptr;
    QDialogButtonBox *m_buttons = nullptr;
};
