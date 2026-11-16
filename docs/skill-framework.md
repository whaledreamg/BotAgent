# Skill 框架

Agent 的 Skill 管理：注册、发现、意图解析、多轮执行。代码在 `services/agent_service/skill/`。

## 结构

```
skill/
├── spec.py        SkillSpec（id、参数 schema、branch、自然语言示例、职责边界）
├── base.py        Skill 基类
├── result.py      SkillResult
├── registry.py    SkillRegistry（按 ID、分类、分支查询；输出 OpenAI tools 格式）
├── catalog.py     SkillCatalog（Router 使用的统一目录）
├── loader.py      加载内置与远程 Skill
├── resolver.py    用户文本 -> Skill 调用列表
├── context.py     SkillContext（跨轮状态、感知缓存、决策链）
├── executor.py    SkillExecutor（按序执行）
├── builtin/       vision_recognize、vision_grasp、tool_skills（weather、time、date、find_object）
└── remote/        HTTP / MCP 基类（预留）
```

## 路由

```
用户输入 -> Router 规则分支（<50ms）或 LLM 分类分支（<1s）
         -> 从 SkillCatalog 取该分支的工具列表
         -> Tool Calling 选 Skill -> 执行 -> TTS
```

新增 Skill 不需要改 `agent.yaml` 或路由规则，Tool Calling 会从 `SkillCatalog` 动态发现。

当前已加载 6 个 Skill（tool 分支）：`vision.recognize`、`vision.grasp`、`weather`、`time`、`date`、`find_object`。

```bash
curl http://127.0.0.1:8800/v1/skills
```

## 使用

```python
loader = SkillLoader()
registry = await loader.load_all()
resolver = SkillResolver(registry)
executor = SkillExecutor(registry)

calls = await resolver.resolve("帮我抓个杯子")
ctx = SkillContext(session_id="s1", request_id="r1", max_turns=3)
result = await executor.execute_sequence(calls, ctx)
print(result.ok, result.final_output, ctx.state)
```

## 新增 Skill

1. 在 `skill/builtin/` 新建文件，继承 `Skill`，实现静态方法 `spec()` 与 `execute(args)`。
2. 加载器自动扫描，无需其他改动。
3. 填好 `natural_language_examples`，Tool Calling 的命中率取决于这些示例。

```python
class MySkill(Skill):
    @staticmethod
    def spec():
        return SkillSpec(id="custom.my_skill", name="我的技能",
                         branch="tool", natural_language_examples=["例子1", "例子2"])

    async def execute(self, args):
        return SkillResult(ok=True, output="...")
```

远程 Skill 在 `configs/skills.yaml` 配置：

```yaml
http:
  - id: remote.service
    url: http://example.com:8000
    endpoint: /api/my-skill
mcp:
  - id: third_party.ai
    transport: stdio
    command: ["python", "-m", "mcp_ai_server"]
```

运行时增删 Skill 见 [hot-reload.md](./hot-reload.md)。

## 内置 Skill

### vision.recognize

识别相机画面中的物体。超时 15 秒。

| 参数 | 必需 | 默认 | 说明 |
|------|------|------|------|
| `question` | 是 | - | 「有什么」列出全部；「有没有 X」判断存在；「有几个 X」计数 |
| `conf` | 否 | 0.15 | 置信度阈值 0 到 1 |

成功后写入 `ctx.state["last_recognition"]` 与感知缓存 `ctx.perception["detected_objects"]`，供 `vision.grasp` 使用。

### vision.grasp

定位物体并计算抓取位姿与预抓取点。超时 15 秒。

| 参数 | 必需 | 默认 | 说明 |
|------|------|------|------|
| `target` | 是 | - | 物体名 |
| `frame` | 否 | `arm_base` | `camera` / `world` / `arm_base`，无效值回退 `arm_base` |
| `approach_distance` | 否 | 0.1 | 预抓取距离（米），沿 Z 轴后退 |

输出 payload：`target_pose`、`approach_pose`、`frame`、`confidence`；同时写入 `ctx.state["grasp_target"]`。

机械臂运动顺序：先到 `approach_pose`，直线靠近 `target_pose`，再闭合夹爪。

限制：只支持单物体；预抓取点不考虑物体形状；精度依赖标定。

### 多轮抓取

```
vision.recognize -> vision.grasp -> 机械臂动作
```

## 错误处理与 TTS 播报

两个视觉 Skill 把异常转换为用户可听懂的原因，设置 `reply` 与 `speak=True`（`_get_error_reason()`）：

| 异常关键词 | 播报 |
|-----------|------|
| 相机没连上 | 相机没有连接，请检查相机设备。 |
| 采帧超时、timeout | 相机采帧超时，请检查相机是否正常工作。 |
| 没拿到、no color | 无法获取相机图像，请检查相机权限。 |
| 深度、depth | 深度信息采集失败，无法测量距离。 |
| 检测、detect、model | 物体检测模型加载失败，请检查模型文件。 |
| 连接、connection、refused | 无法连接到视觉服务，请检查视觉服务是否运行。 |
| 权限、permission、denied | 没有权限访问相机设备。 |
| 硬件、hardware | 相机硬件故障，请检查设备。 |
| 其他相机错误 | 相机出错，请检查相机连接和设置。 |

## 工具脚本

位置 `services/agent_service/tools/`：

| 脚本 | 用途 |
|------|------|
| `check_skill_registry.py` | Skill 系统自检（加载、分类、意图解析、状态） |
| `demo_skill_workflow.py` | 演示识别、多轮编排、状态管理 |
| `check_tool_calling.py` | 验证 LLM function calling |
| `check_providers.py` | 检查 LLM 提供商配置 |
| `check_weather.py` | 检查天气集成 |
| `debug_dispatch.py` | 调试请求分发 |
| `selftest.py` | 完整自测 |
| `bench_llm.py` | LLM 性能基准 |
| `check_traces.py` | 查询视觉追溯记录 |

```bash
cd services/agent_service
python3 tools/check_skill_registry.py
python3 tools/demo_skill_workflow.py
```

## 排查

- 新 Skill 没被发现：确认已 `register`、`branch` 正确（chat/tool/device）、示例覆盖了用户的真实说法。
- Tool Calling 选错 Skill：补充 `natural_language_examples`。
- 查看 Skill 的 Tool Calling 格式：`svc.skill_catalog.to_openai_tools_by_branch("tool")`。
- 旧的 `TOOLS` 字典和路由规则仍然有效，新旧可并行。
