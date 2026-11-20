#include "mainwindow.h"

#include "core/qssmanager.h"

#include <QApplication>
#include <QLoggingCategory>
#include <QPixmap>
#include <QTimer>

int main(int argc, char *argv[])
{
    QApplication app(argc, argv);

    QApplication::setApplicationName(QStringLiteral("BotAgent Monitor"));
    QApplication::setApplicationVersion(QStringLiteral("1.0.0"));
    QApplication::setOrganizationName(QStringLiteral("BotAgent"));

    // 加载全局样式 (theme -> global -> common/*)
    QssManager::instance()->loadGlobalStyle();

    // ---- 调试用: --screenshot <路径> [等待毫秒] ----
    //
    // 打开窗口, 等一小段时间让数据流进来, 然后把窗口内容存成 PNG 再退出。
    // 用 QWidget::render() 强制画一遍控件树, 不依赖 X 的截屏能力。
    QString screenshotPath;
    int waitMs = 6000;
    // --start —— 跳过「点开始监控」这一步, 直接进入监控态 (调试/截图用)
    bool autoStart = false;
    const QStringList args = app.arguments();
    for (int i = 1; i < args.size(); ++i) {
        if (args.at(i) == QStringLiteral("--screenshot") && i + 1 < args.size()) {
            screenshotPath = args.at(++i);
            if (i + 1 < args.size())
                waitMs = args.at(++i).toInt();
        } else if (args.at(i) == QStringLiteral("--start")) {
            autoStart = true;
        }
    }

    MainWindow window;
    window.show();
    if (autoStart)
        window.startMonitoring();

    if (!screenshotPath.isEmpty()) {
        QTimer::singleShot(waitMs, [&window, screenshotPath]() {
            // 先让事件循环把布局与绘制跑完, 再截。
            window.repaint();
            QApplication::processEvents();

            QPixmap pixmap(window.size());
            pixmap.fill(QColor(242, 244, 248));
            window.render(&pixmap);
            const bool ok = pixmap.save(screenshotPath);
            qInfo("[截图] 已保存 %s (成功=%d, 尺寸=%dx%d)",
                  qPrintable(screenshotPath), ok ? 1 : 0,
                  pixmap.width(), pixmap.height());
            QApplication::exit(0);
        });
    }

    return app.exec();
}
