#include "ui/servicedetaildialog.h"

#include <QDialogButtonBox>
#include <QFont>
#include <QGridLayout>
#include <QJsonDocument>
#include <QLabel>
#include <QTextBrowser>
#include <QVBoxLayout>
#include <QWidget>

namespace {

/// 字段区的标签颜色 (次要文字)。
const char *kLabelColor = "#5A6478";
/// JSON 区里键名的颜色。
const char *kKeyColor = "#4A6FA5";
/// JSON 区里字符串值的颜色。
const char *kStrColor = "#2E7D4F";
/// JSON 区里数字/布尔的颜色。
const char *kNumColor = "#B5593B";
/// JSON 区的底色。
const char *kJsonBg = "#1E2430";
/// JSON 区的正文颜色。
const char *kJsonFg = "#C8D0E0";

}  // namespace

ServiceDetailDialog::ServiceDetailDialog(QWidget *parent)
    : QWidget(parent, Qt::Window)
{
    setObjectName(QStringLiteral("svcDetailDialog"));
    setWindowTitle(QStringLiteral("服务详情"));
    // 固定比例: 宽 : 高 = 3 : 2。JSON 可能有几十行, 给足高度。
    resize(780, 520);
    setMinimumSize(520, 380);

    auto *root = new QVBoxLayout(this);
    root->setContentsMargins(18, 16, 18, 14);
    root->setSpacing(12);

    // ---- 顶部: 状态徽标 + 名字 + 地址 ----
    auto *head = new QHBoxLayout;
    head->setSpacing(10);
    head->setContentsMargins(0, 0, 0, 0);

    m_badge = new QLabel(this);
    m_badge->setObjectName(QStringLiteral("svcDetailBadge"));
    m_badge->setFixedSize(14, 14);
    head->addWidget(m_badge);

    m_lblName = new QLabel(this);
    m_lblName->setObjectName(QStringLiteral("svcDetailName"));
    m_lblName->setTextInteractionFlags(Qt::TextSelectableByMouse);
    head->addWidget(m_lblName);

    m_lblAddr = new QLabel(this);
    m_lblAddr->setObjectName(QStringLiteral("svcDetailAddr"));
    head->addWidget(m_lblAddr);

    head->addStretch(1);
    root->addLayout(head);

    // ---- 中间: 关键字段 (两列) ----
    //
    // 两列 = 标签列 + 值列。标签列固定宽度, 值列吃掉剩余空间 ——
    // 这样「说明」那行再长也不会把布局挤歪。
    auto *fields = new QGridLayout;
    fields->setContentsMargins(0, 0, 0, 0);
    fields->setHorizontalSpacing(14);
    fields->setVerticalSpacing(5);
    fields->setColumnStretch(0, 0);
    fields->setColumnStretch(1, 1);

    auto addField = [&](const QString &caption, QLabel *&value) {
        auto *lbl = new QLabel(caption, this);
        lbl->setObjectName(QStringLiteral("svcDetailFieldName"));
        lbl->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
        lbl->setMinimumWidth(64);
        fields->addWidget(lbl, fields->rowCount(), 0);
        value = new QLabel(this);
        value->setObjectName(QStringLiteral("svcDetailFieldValue"));
        value->setWordWrap(true);
        value->setTextInteractionFlags(Qt::TextSelectableByMouse);
        fields->addWidget(value, fields->rowCount() - 1, 1);
    };

    addField(QStringLiteral("状态"), m_lblState);
    addField(QStringLiteral("延迟"), m_lblLatency);
    addField(QStringLiteral("说明"), m_lblDetail);
    addField(QStringLiteral("错误"), m_lblError);

    root->addLayout(fields);

    // ---- 底部: 原始 JSON ----
    m_lblJsonTitle = new QLabel(QStringLiteral("原始状态 JSON"), this);
    m_lblJsonTitle->setObjectName(QStringLiteral("svcDetailJsonTitle"));
    root->addWidget(m_lblJsonTitle);

    m_jsonView = new QTextBrowser(this);
    m_jsonView->setObjectName(QStringLiteral("svcDetailJson"));
    m_jsonView->setLineWrapMode(QTextBrowser::WidgetWidth);
    // 固定宽度字体 —— JSON 的对齐靠空格, 比例字体会歪。
    QFont mono(QStringLiteral("monospace"));
    mono.setStyleHint(QFont::Monospace);
    mono.setPointSize(9);
    m_jsonView->setFont(mono);
    m_jsonView->setOpenExternalLinks(false);

    // ---- 深色底 ----
    //
    // **QSS 里的 background-color 对 QTextBrowser 不可靠** —— 文本视口
    // 自己带一层背景, 会盖掉 QSS 设的底色。所以这里除了样式表, 还得
    // 直接对视口设一遍, 保证任何情况下都是深色底。
    m_jsonView->viewport()->setStyleSheet(
        QStringLiteral("background-color: %1;").arg(QLatin1String(kJsonBg)));
    m_jsonView->document()->setDefaultStyleSheet(
        QStringLiteral("body { color: %1; }").arg(QLatin1String(kJsonFg)));
    m_jsonView->document()->setDocumentMargin(0);

    // JSON 至少这么高 —— 不然字段一多就被挤没了。
    m_jsonView->setMinimumHeight(220);
    root->addWidget(m_jsonView, 1);  // 伸展: JSON 区吃掉多余高度

    // ---- 按钮 ----
    m_buttons = new QDialogButtonBox(QDialogButtonBox::Close, this);
    m_buttons->setObjectName(QStringLiteral("svcDetailButtons"));
    root->addWidget(m_buttons);

    connect(m_buttons, &QDialogButtonBox::rejected, this, &QWidget::close);
}

ServiceDetailDialog::~ServiceDetailDialog() = default;

void ServiceDetailDialog::setService(const QString &name, const QString &host,
                                     int port, const QString &stateText,
                                     const QColor &stateColor, int latencyMs,
                                     const QString &detail,
                                     const QString &lastError,
                                     const QJsonObject &statusJson)
{
    setWindowTitle(QStringLiteral("%1 详情").arg(name));

    m_badge->setStyleSheet(
        QStringLiteral("background-color: %1; border-radius: 7px;").arg(stateColor.name()));

    m_lblName->setText(name);
    m_lblAddr->setText(QStringLiteral("%1:%2").arg(host).arg(port));

    m_lblState->setText(stateText);
    m_lblState->setStyleSheet(QStringLiteral("color: %1;").arg(stateColor.darker(120).name()));

    m_lblLatency->setText(latencyMs >= 0
                              ? QStringLiteral("%1 ms").arg(latencyMs)
                              : QStringLiteral("—"));
    m_lblDetail->setText(detail.isEmpty() ? QStringLiteral("—") : detail);

    // 没有错误时不标红 —— 空的就用次要色。
    m_lblError->setText(lastError.isEmpty() ? QStringLiteral("—") : lastError);
    m_lblError->setStyleSheet(lastError.isEmpty()
                                  ? QStringLiteral("color: %1;").arg(QLatin1String(kLabelColor))
                                  : QStringLiteral("color: #E0534D;"));

    if (statusJson.isEmpty()) {
        m_lblJsonTitle->setText(QStringLiteral("原始状态 JSON (无)"));
        m_jsonView->setHtml(QStringLiteral(
            "<div style='color:%1;'>该服务没有提供状态 JSON</div>")
                                 .arg(QLatin1String(kLabelColor)));
    } else {
        m_lblJsonTitle->setText(QStringLiteral("原始状态 JSON"));
        m_jsonView->setHtml(_jsonToHtml(statusJson));
    }
}

QString ServiceDetailDialog::_jsonToHtml(const QJsonObject &obj) const
{
    // ---- 先拿到缩进好的纯文本 ----
    //
    // 用 QJsonDocument::Indented 再转 HTML —— 缩进和转义由 Qt 处理,
    // 不用自己拼 (自己拼要处理引号、换行、尖括号转义, 容易漏)。
    const QString text = QString::fromUtf8(
        QJsonDocument(obj).toJson(QJsonDocument::Indented));

    // ---- 转成带颜色的 HTML ----
    QString html;
    html.reserve(text.size() * 2);
    const QStringList lines = text.split(QLatin1Char('\n'));
    for (const QString &line : lines) {
        html += QStringLiteral("<div style=\"white-space:pre-wrap;\">");
        html += _highlightLine(line);
        html += QStringLiteral("</div>");
    }
    return html;
}

QString ServiceDetailDialog::_highlightLine(const QString &line) const
{
    // ---- 拆出「缩进 + 键 + 分隔符 + 值」 ----
    const int colon = line.indexOf(QLatin1Char(':'));
    if (colon < 0)
        return _escape(line);

    const QString indent = line.left(colon);
    const QString key = line.mid(indent.length(), colon - indent.length());
    const QString rest = line.mid(colon);   // 含冒号和后面的值

    QString html = _escape(indent);
    html += QStringLiteral("<span style=\"color:%1;\">%2</span>")
                .arg(QLatin1String(kKeyColor), _escape(key));
    html += QStringLiteral("<span style=\"color:%1;\">:</span>")
                .arg(QLatin1String(kLabelColor));

    // ---- 值 ----
    const QString value = rest.mid(1).trimmed();
    QString color = QLatin1String(kNumColor);
    if (value.startsWith(QLatin1Char('"')) || value.startsWith(QLatin1Char('\'')))
        color = QLatin1String(kStrColor);
    html += QStringLiteral("<span style=\"color:%1;\">%2</span>")
                .arg(color, _escape(rest.mid(1)));
    return html;
}

QString ServiceDetailDialog::_escape(const QString &text)
{
    QString out;
    out.reserve(text.size());
    for (const QChar &c : text) {
        switch (c.unicode()) {
        case '<':
            out += QStringLiteral("&lt;");
            break;
        case '>':
            out += QStringLiteral("&gt;");
            break;
        case '&':
            out += QStringLiteral("&amp;");
            break;
        default:
            out += c;
        }
    }
    return out;
}

void ServiceDetailDialog::showFor(QWidget *parent)
{
    if (parent) {
        // 居中于父窗口 (而不是屏幕) —— 弹窗跟着主窗口走。
        const QRect p = parent->geometry();
        move(p.center() - QPoint(width() / 2, height() / 2));
    }
    show();
    raise();
    activateWindow();
}
