#pragma once

#include <QJsonObject>
#include <QObject>
#include <QString>

class QJsonArray;
class QNetworkAccessManager;
class QNetworkReply;

/// 手动指令客户端 —— 直接向 Agent 提问。
///
/// 走 `POST /v1/dispatch`, 与 ASR 语音链路**完全同一条路径**:
/// 分流、工具调用、参数提取、回复全都一样, 也就是说
/// **手动输入同样会触发设备动作** (机械臂 / 空调 / 灯 …)。
///
/// 思考过程不在这个响应里 —— 它走 Agent 的 WebSocket 事件推送
/// (`turn_start` / `thinking` / `turn_end`), 由监控端的 WsClient 接收。
/// 所以想看到推理过程, 必须先点「开始监控」。
class AgentClient : public QObject {
    Q_OBJECT

public:
    explicit AgentClient(QObject *parent = nullptr);
    ~AgentClient() override;

    /// 设置 Agent 的 baseUrl (如 http://127.0.0.1:8800)。
    void setBaseUrl(const QString &baseUrl);
    QString baseUrl() const { return m_baseUrl; }

    /// 设置请求超时 (毫秒)。Agent 要走 LLM, 默认给得比轮询长。
    void setTimeout(int ms);

    /// 发送一轮询问。
    ///
    /// 发之前会 abort 掉上一个还没回来的请求 —— 防止快速连点
    /// 造成一堆请求堆积、回复乱序。
    ///
    /// 返回 false 表示请求压根没发出去 (baseUrl 为空等)。
    bool dispatch(const QString &text, const QString &sessionId = QStringLiteral("monitor-ui"));

    /// 是否有一个请求在飞行中。
    bool isBusy() const { return m_reply != nullptr; }

signals:
    /// Agent 同步返回的结果 (字段与 WS 的 turn_end 一致)。
    void dispatched(const QJsonObject &result);
    /// 发送失败 (网络错误 / 非 200 / 超时)。
    void failed(const QString &error);

private slots:
    void onReplyFinished();

private:
    QNetworkAccessManager *m_nam = nullptr;
    QNetworkReply *m_reply = nullptr;
    QString m_baseUrl;
    QString m_sessionId;
    int m_timeoutMs = 30000;
};
