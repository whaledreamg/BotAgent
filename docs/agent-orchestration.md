# 复合查询编排

处理「先查南京天气再查时间」这类包含多个意图的请求。代码在 `services/agent_service/`。

## 模块

| 文件 | 作用 |
|------|------|
| `task_parser.py` | 把 LLM 返回的 JSON 解析为 `TaskSequence` / `Task`，并判断是否启用 thinking mode |
| `parameter_validator.py` | 按 Skill 约束校验参数，缺失时用提取器补全（默认覆盖 weather、time、date、find_object） |
| `orchestrator.py` | `TaskOrchestrator`：顺序或并行执行、单任务超时、结果聚合。单个任务失败不影响其他任务 |
| `orchestration.py` | `OrchestrationManager`：串起「LLM 解析、参数校验、执行」 |

## 流程

```
用户文本 -> LLM（可选 thinking）-> 任务 JSON -> parse_task_sequence
         -> 参数校验/补全 -> TaskOrchestrator.execute -> 聚合文本 -> TTS
```

任务 JSON 示例：

```json
{
  "tasks": [
    {"tool": "weather", "params": {"city": "北京"}, "order": 1},
    {"tool": "time", "params": {}, "order": 2}
  ],
  "sequence_type": "sequential"
}
```

`sequence_type` 取 `sequential` 或 `parallel`。DAG 依赖暂未支持。

## Thinking mode 配置

`configs/agent.yaml`：

```yaml
router:
  tool_calling:
    thinking:
      enabled: true
      budget_tokens: 8000
      min_complexity: 2   # 至少 2 个意图才启用
```

`router.py` 会把该配置透传给 `llm.tool_call(..., thinking=...)`。简单查询不启用，以节省延迟和成本。

## 超时

`handlers/tool_handler.py` 中 `timeout_per_task` 为 15 秒（原 5 秒）。

实测天气查询约 6 秒（地理编码 3 到 4 秒，天气接口 1 到 2 秒），时间查询小于 1 秒，所以复合查询总耗时约 6 到 7 秒。仍然超时时：

1. 把 `timeout_per_task` 提到 20 到 25 秒。
2. 把 `sequence_type` 改为 `parallel`。
3. 在 `weather_client.py` 中加重试（指数退避）。

## 天气接口故障排查

天气使用 Open-Meteo（`geocoding-api.open-meteo.com` 与 `api.open-meteo.com`）。日志出现 `[天气] 查询失败` 时按顺序检查：

```bash
# 网络与 DNS
ping geocoding-api.open-meteo.com
nslookup geocoding-api.open-meteo.com

# 接口本身
time curl -s "https://geocoding-api.open-meteo.com/v1/search?name=南京&language=zh"

# Agent 环境内直接调用，查看真实异常
cd services/agent_service
python3 tools/check_weather.py
```

| 现象 | 原因 | 处理 |
|------|------|------|
| Connection refused / timeout | 防火墙或网络受限 | 放行出站 443，或在 `weather_client.py` 的 `httpx.AsyncClient` 中配置 `proxies` |
| Name or service not known | DNS 失败 | 改 `/etc/resolv.conf`，加公共 DNS |
| HTTP 429 | 被限流 | 加重试与退避，或配合参数缓存 |
| 异常信息为空 | 超时类异常的 `str(e)` 为空 | 日志里记录 `type(e).__name__` 与堆栈 |

服务重启后若行为异常，先确认没有残留的 uvicorn 进程占用 8800 端口：

```bash
ps aux | grep "uvicorn.*8800" | grep -v grep
```

`scripts/stop_all.sh` 没有清理干净时，需要手动结束残留进程再启动。

## 验证

```bash
cd services/agent_service
python3 test_orchestration.py

curl -X POST http://127.0.0.1:8800/v1/dispatch \
  -H "Content-Type: application/json" \
  -d '{"text": "先查南京天气再查时间", "session_id": "test"}'
```

预期两个任务都成功，聚合回复形如「南京天气晴朗，气温 25 度。当前时间 14:30。」。
