#ifndef CORE_QSSMANAGER_H
#define CORE_QSSMANAGER_H

#include <QHash>
#include <QObject>
#include <QString>
#include <QWidget>

/**
 * @brief QSS 样式管理器 (单例)
 *
 * 负责加载全局 QSS 以及按模块加载样式。
 * 加载顺序: theme -> global -> 模块特定样式
 *
 * 配色约定:
 *   所有颜色都在 resources/styles/theme_light.qss 里, 其余文件只管结构
 *   (圆角/内边距/字号), 这样加载顺序不会互相覆盖。
 */
class QssManager : public QObject
{
    Q_OBJECT

public:
    static QssManager *instance();

    /// 加载全局基础样式 (theme + global)
    void loadGlobalStyle();

    /// 加载模块特定样式 (会覆盖该 widget 上的全局样式)
    void loadStyleForWidget(QWidget *widget, const QString &moduleName);

    /// 重新加载所有样式
    void reloadAllStyles();

    /// 读取 QSS 文件内容
    QString getStyleContent(const QString &resourcePath) const;

signals:
    /// 样式发生变化
    void styleChanged();

private:
    explicit QssManager(QObject *parent = nullptr);
    QssManager(const QssManager &) = delete;
    QssManager &operator=(const QssManager &) = delete;

    QString resolveResourcePath(const QString &moduleName) const;

    QHash<QWidget *, QString> m_widgetStyles;  ///< widget -> moduleName
};

#endif  // CORE_QSSMANAGER_H
