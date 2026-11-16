# 视觉识别追溯

每次识别后自动保存标注图像和物体信息，用于事后查询。

## 文件

| 路径 | 说明 |
|------|------|
| `services/vision_service/tracing.py` | `DetectionTracer`（内部加锁，线程安全） |
| `services/vision_service/test_tracing.py` | 单元测试 |
| `services/vision_service/service.py` | 每次识别后记录 |
| `services/vision_service/main.py` | 3 个查询接口 |
| `services/agent_service/vision_client.py` | `traces_stats` / `traces_query` / `traces_image_url` |
| `services/agent_service/tools/check_traces.py` | 命令行查询 |
| `services/agent_service/tools/traces_examples.py` | Agent Tool 集成示例 |

数据目录 `traces/`：

```
traces/
├── images/        每个物体一张图，画有检测框
├── records.jsonl  每行一条记录
└── index.json     按类别的统计索引
```

## 记录格式

```json
{
  "record_id": "1704110730000_0001",
  "timestamp": "2024-01-01T12:05:30.123456",
  "image_filename": "20240101_120530_1704110730000_0001.jpg",
  "label": "cup",
  "confidence": 0.9543,
  "bbox": [100.5, 150.2, 250.8, 350.9],
  "pose": {"x": 0.123, "y": -0.456, "z": 0.789, "frame": "camera", "depth_valid": true},
  "frame": "camera"
}
```

`bbox` 为像素坐标 `[x1, y1, x2, y2]`，`pose` 单位为米。

## 接口

视觉服务默认端口 8803。

| 接口 | 参数 | 说明 |
|------|------|------|
| `GET /v1/traces/stats` | - | 总数与各类别计数 |
| `GET /v1/traces/query` | `label`、`start_time`、`end_time`（ISO 8601）、`limit`（默认 50） | 查询记录 |
| `GET /v1/traces/image/{filename}` | - | 返回 JPEG |

```bash
curl http://127.0.0.1:8803/v1/traces/stats | jq
curl "http://127.0.0.1:8803/v1/traces/query?label=cup&limit=10" | jq
```

## 使用

```bash
# 验证
cd services/vision_service && python3 test_tracing.py

# 命令行
cd services/agent_service && python3 tools/check_traces.py

# 直接看文件
tail -n 20 traces/records.jsonl | jq
jq '.labels' traces/index.json
```

Python 客户端：

```python
client = VisionClient()
result = await client.traces_query(label="cup", limit=10)
for r in result["records"]:
    print(r["label"], r["confidence"], r["pose"])
await client.aclose()
```

## 配置与维护

- 目录由 `cfg.trace_dir` 指定，默认 `traces`。
- 每张图约 300 到 800 KB，需要定期清理：`find traces/images -mtime +7 -delete`。清理前备份 `records.jsonl`。
- 追溯默认开启。关闭方法是把 `service.py` 里 `DetectionTracer("traces")` 的目录换成临时目录。
