# 参数提取（LLM-First）

从用户文本中提取各 Skill 需要的参数（城市、物体名等）。代码在 `services/agent_service/`。

## 策略

```
用户文本 -> 缓存（约 1ms）-> LLM 提取（约 100 到 2000ms）-> 正则降级（约 0.2ms）
```

| 方案 | 准确度 | 说明 |
|------|--------|------|
| 正则优先（旧） | 约 81%（11 个用例中 9 个） | 快，但复杂句式出错 |
| LLM-First（当前） | 95% 以上（预期） | 加缓存后平均延迟更低；LLM 失败时回退正则 |

## 模块

| 文件 | 作用 |
|------|------|
| `parameter_extraction_manager.py` | 入口，`llm_first_extract(text, skill_spec)` 返回 `(params, confidence, reasoning)` |
| `llm_parameter_extractor.py` | `LLMParameterExtractor`、`MultiSkillExtractor`、`SkillSpec`、`ParameterSpec` |
| `parameter_cache.py` | 按 `skill:query_hash` 缓存，支持按 Skill 设置 TTL |
| `parameter_extractors.py` | 正则降级：`CityExtractor`、`ObjectExtractor`、`LocationExtractor` |
| `thinking_logger.py` | 记录每次提取的完整过程 |
| `query_api.py` | 日志查询接口 |
| `configs/param_skills.yaml` | Skill 参数定义（新增 Skill 只改这个文件） |

## 定义 Skill 参数

```yaml
skills:
  weather:
    label: "查询天气"
    description: "查询指定城市的天气"
    category: "query"
    parameters:
      - name: city
        description: "用户想查询的城市"
        type: string
        required: true
        examples: ["北京", "上海", "南京"]
        notes: "用户说「这里」时需要推断"
      - name: date
        type: string
        required: false
        default: "今天"
```

描述要具体，并给出 `examples`，LLM 的提取效果取决于这些文字。

## 调用

```python
params, confidence, reasoning = await manager.llm_first_extract(
    text="查北京天气", skill_spec=skill,
)
# {'city': '北京'}, 0.98, "用户明确提到北京"
```

`params is None` 表示提取失败，调用方应使用默认值或追问用户。

## 缓存 TTL 建议

| 类型 | TTL |
|------|-----|
| 实时性强（股票、交通） | 300 到 600 秒 |
| 相对稳定（天气、物体） | 1800 到 3600 秒 |
| 变化快（笑话、新闻） | 60 到 300 秒 |

```python
cache.set_skill_ttl("weather", 3600)
```

## 思考日志与查询接口

日志按 `logs/thinking/<日期>/<skill>/<小时>.jsonl` 存放，每行一条 JSON，包含提示词、LLM 调用、解析、校验各阶段，以及与正则结果的对比和耗时分解。

| 接口 | 用途 |
|------|------|
| `GET /v1/thinking/logs/{trace_id}` | 单条日志 |
| `GET /v1/thinking/logs` | 日志列表 |
| `GET /v1/thinking/stats?skill=weather&group_by=hour` | 准确度、延迟、缓存命中率 |
| `GET /v1/thinking/compare?skill=weather&date=YYYY-MM-DD` | LLM 与正则对比 |
| `GET /v1/thinking/mismatches?skill=weather&limit=10` | 两者不一致的案例 |

`manager.get_stats()` 返回缓存命中数、LLM 调用数、正则降级数、LLM 与正则一致/不一致数。

## 正则提取的已知限制

正则只作降级路径。2026-11-14 对「然后」类连接词做过修复（`parameter_extractors.py` 中 `CityExtractor`）：

| 查询 | 修复前 | 修复后 |
|------|--------|--------|
| 先查南京天气然后讲个笑话 | 南京然后讲个笑话 | 南京 |
| 查上海天气接着播放音乐 | 上海 | 上海 |
| 看杭州天气之后找个餐厅 | 看杭州 | 杭州 |

仍无法处理的情况，依赖 LLM：

- 多城市：「查广州和深圳的天气」得到「广州和深圳」
- 修饰词：「先看北京天气怎么样然后查现在几点」得到「看北京」
- 时序词：「查武汉天气后面再问时间」得到「武汉后面」
- 疑问形式的物体名：「有没有钥匙」

## 测试

```bash
cd services/agent_service
pytest test_llm_extractor.py test_comparison.py test_llm_first_integration.py -v
python3 test_regex_improvement.py
python3 test_tool_handler_integration.py   # 验证 ToolHandler 加载了全部 Skill
```

手动验证：

```bash
curl -X POST http://127.0.0.1:8800/v1/dispatch \
  -H "Content-Type: application/json" \
  -d '{"text": "查北京天气", "session_id": "t1"}'
```

日志中应出现 `[LLM-First] 天气参数提取: city=北京`，重复请求时出现 `缓存命中`。

## 迁移与调优

- 迁移时先并行运行新旧方案，用 `/v1/thinking/compare` 对比，再逐步切流。
- 准确度不够时换更强的模型或把 `temperature` 降到 0.1 左右。
- 日志约 100 到 200 字节/条，定期清理旧日期目录即可。
