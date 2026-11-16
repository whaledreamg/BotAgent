# 热更新与管理接口

不重启 Agent 服务即可更新配置、增删 Skill（例如接入 MCP 工具）。代码在 `services/agent_service/`：

- `config_watcher.py`：每 2 秒检查 `configs/agent.yaml` 修改时间，变化时触发回调。
- `admin.py`：`AdminManager`，提供配置重载与 Skill 注册/移除。

## 接入 app.py

```python
from pathlib import Path
from config_watcher import ConfigWatcher
from admin import AdminManager

# Service.__init__ 末尾
self.admin = AdminManager(get_service=lambda: self)

# startup
config_watcher = ConfigWatcher(
    config_path=Path(__file__).parent.parent.parent / "configs" / "agent.yaml",
    callback=lambda: service.admin.reload_config(),
    check_interval=2.0,
)
config_watcher.spawn_task()

# shutdown
await config_watcher.stop()
```

## 接口

| 接口 | 说明 |
|------|------|
| `POST /admin/config/reload` | 手动重载 `agent.yaml`，重新编译路由规则。失败时保持旧配置 |
| `POST /admin/skills/register` | 注册 Skill |
| `POST /admin/skills/unregister?skill_id=...` | 移除 Skill |
| `GET /admin/skills/list?branch=tool` | 列出 Skill |

注册示例：

```bash
curl -X POST http://127.0.0.1:8800/admin/skills/register \
  -H "Content-Type: application/json" \
  -d '{
    "id": "mcp_translation",
    "name": "MCP 翻译服务",
    "branch": "tool",
    "description": "调用 MCP 服务进行多语言翻译",
    "category": "mcp",
    "source": "mcp",
    "tags": ["translation"],
    "example_inputs": ["翻译为英文", "用中文怎么说"]
  }'
```

## 行为

- 重载时先加载新配置，成功才替换 `svc.cfg` 与 `svc.router`；出错则恢复旧值，服务不中断。
- 已注册的 Skill 在重载后保留。
- 正在处理的请求继续使用旧 Router，新请求使用新 Router，不需要全局锁。

## 已知缺口

- 注册的 Skill 只存在内存里，重启后丢失。
- 没有配置版本号与回滚。
- MCP 侧不能主动推送变化，需要调用方自己调用注册接口。
