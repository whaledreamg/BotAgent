#pragma once

#include <QJsonObject>
#include <QObject>
#include <QString>

class QNetworkAccessManager;
class QNetworkReply;

/// 视觉服务客户端 —— 触发一次识别。
///
/// **为什么不轮询**:
/// 之前是 Vision 的 WebSocket 连上就自己每秒拍一次, 监控端被动收。
/// 那等于服务端轮询 —— 没人看也在拍, 相机和 GPU 一直转, 界面上
/// 还一闪一闪的。
///
/// 现在改成**按需**: 用户点「识别一次」才拍一次, 拍完通过
/// WebSocket 广播回来, 界面显示标注图 + 识别到的物体。
///
/// 两种触发方式:
///   - ``detect()``  —— 走 HTTP ``POST /v1/detect``, 结果同样会
///     广播给 WS 订阅者 (所以用哪种都行)
///   - WebSocket 发 ``{"action":"detect"}`` —— 见 MainWindow
class VisionClient : public QObject {
    Q_OBJECT

public:
    explicit VisionClient(QObject *parent = nullptr);
    ~VisionClient() override;

    /// 设置视觉服务的 baseUrl (如 http://127.0.0.1:8803)。
    void setBaseUrl(const QString &baseUrl);
    QString baseUrl() const { return m_baseUrl; }

    /// 设置请求超时 (毫秒)。识别要走模型推理, 默认给得长一些。
    void setTimeout(int ms);

    /// 触发一次识别。
    ///
    /// Returns: false 表示请求压根没发出去 (baseUrl 为空 / 正在识别中)。
    bool detect();

    /// 当前是否有一个识别请求在飞行中。
    bool isBusy() const { return m_reply != nullptr; }

signals:
    /// 识别完成 —— 结果直接来自 HTTP 响应。
    ///
    /// **正常情况下这个信号不会触发** —— 结果走 WebSocket 广播回来
    /// (这样所有订阅者都能看到同一帧)。这里发出来只是为了
    /// 「WS 没连上」时还能兜底显示。
    void detected(const QJsonObject &result);

    /// 识别失败 (相机没连上 / 模型没加载 / 网络错误 / 超时)。
    void failed(const QString &error);

private slots:
    void onReplyFinished();

private:
    QNetworkAccessManager *m_nam = nullptr;
    QNetworkReply *m_reply = nullptr;
    QString m_baseUrl;
    int m_timeoutMs = 20000;
};
