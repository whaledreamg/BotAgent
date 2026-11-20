#include "core/qssmanager.h"

#include <QApplication>
#include <QDebug>
#include <QFile>

QssManager *QssManager::instance()
{
    static QssManager inst;
    return &inst;
}

QssManager::QssManager(QObject *parent)
    : QObject(parent)
{
}

void QssManager::loadGlobalStyle()
{
    // 加载顺序: theme (配色) -> global (字体) -> mainwindow (果冻风控件)
    // theme 必须在前 —— 它定义的每个选择器都会覆盖后面的结构定义。
    // mainwindow.qss 全用 #objectName 选择器, 不会污染别的控件。
    QStringList styleFiles = {
        QStringLiteral(":/resources/styles/theme_light.qss"),
        QStringLiteral(":/resources/styles/global.qss"),
        QStringLiteral(":/resources/styles/mainwindow.qss"),
    };

    QString combined;
    for (const QString &path : styleFiles) {
        combined += getStyleContent(path) + QStringLiteral("\n");
    }

    qApp->setStyleSheet(combined);
    qDebug() << "[QssManager] Global style loaded.";
}

void QssManager::loadStyleForWidget(QWidget *widget, const QString &moduleName)
{
    if (!widget)
        return;

    const QString path = resolveResourcePath(moduleName);
    const QString content = getStyleContent(path);
    if (content.isEmpty()) {
        qWarning() << "[QssManager] No style content for module:" << moduleName;
        return;
    }

    widget->setStyleSheet(content);
    m_widgetStyles[widget] = moduleName;
}

void QssManager::reloadAllStyles()
{
    loadGlobalStyle();

    // 重新应用模块样式
    const QList<QWidget *> widgets = m_widgetStyles.keys();
    for (QWidget *w : widgets) {
        if (w) {
            loadStyleForWidget(w, m_widgetStyles.value(w));
        } else {
            m_widgetStyles.remove(w);
        }
    }

    emit styleChanged();
}

QString QssManager::getStyleContent(const QString &resourcePath) const
{
    QFile file(resourcePath);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text))
        return QString();
    return QString::fromUtf8(file.readAll());
}

QString QssManager::resolveResourcePath(const QString &moduleName) const
{
    // 支持: "controls/monitor" -> ":/resources/styles/controls/monitor.qss"
    if (moduleName.startsWith(QLatin1String(":/")))
        return moduleName;
    return QStringLiteral(":/resources/styles/%1.qss").arg(moduleName);
}
