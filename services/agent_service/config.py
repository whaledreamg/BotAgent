"""Agent 服务配置加载。

配置来源优先级 (后者覆盖前者):
1. 本文件内的默认值
2. configs/agent.yaml
3. 环境变量 (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL)
4. configs/.env (仅当环境变量未设置时生效)

LLM 部分支持**多 provider 预设** (configs/agent.yaml 的 `llm.providers`),
按角色 (chat / classify / tool) 分别绑定, 默认三个角色指向同一个 provider。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("agent.config")

try:
    import yaml
except ImportError:  # pragma: no cover - 依赖缺失时给出清晰提示
    yaml = None


#: 项目根目录 (Bot Agent/)
ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "configs"
AGENT_CONFIG_PATH = CONFIG_DIR / "agent.yaml"
#: 机械臂语音指令映射表
ACTIONS_CONFIG_PATH = CONFIG_DIR / "actions.yaml"
#: tool 定义 (模型看到的工具清单)
TOOLS_CONFIG_PATH = CONFIG_DIR / "tools.yaml"
ENV_PATH = CONFIG_DIR / ".env"

DEFAULTS: Dict[str, Any] = {
    "llm": {
        "base_url": "https://api.agnes-ai.cn/v1",
        "model": "agnes-3.0-flash",
        "timeout": 60,
        "max_retries": 3,
        "temperature": 0.7,
        "max_tokens": 2048,
    },
    "router": {
        "mode": "hybrid",
        "llm_fallback_model": None,
        "default_route": "chat",
        "rules": [],
        "llm_timeout": 1.2,
        "trust_rules": True,
    },
    "handlers": {},
    "session": {"max_turns": 6, "ttl_seconds": 1800},
    "system_prompt": "你是一个中文语音助手, 用简洁口语化的中文回答, 不超过三句话。",
    "router_prompt": (
        "你是一个语音指令分类器。只输出 JSON: "
        '{"intent": "chat" | "tool" | "device"}'
    ),
}

#: tool calling 专用的默认系统提示词。
#:
#: **不能用 system_prompt** —— 那是聊天用的, 它的指令是「回答用户」,
#: 模型会用文字描述动作已完成 (「空调关掉了」), 而不是真的去调工具。
#: 那就是**假确认**: 用户以为动作做了, 实际什么都没发生。
#:
#: 这里明确告诉模型: 想让设备动就必须调工具, 不要用嘴说。
DEFAULT_TOOL_CALLING_PROMPT = (
    "你是一个语音助手, 可以通过工具控制机器人和家电设备。\n"
    "规则:\n"
    "1. 如果用户想让你执行一个动作 (控制机械臂、开关灯、开空调、播放音乐、"
    "查询天气/时间/日期), 你**必须**调用对应的工具, 不要只用文字回答。\n"
    "2. 绝对不要声称动作已经完成 —— 只有真正调用工具才算执行。\n"
    "3. 如果用户只是在聊天、提问, 直接简短回答即可, 不要调用工具。\n"
    "4. 回答用简洁口语化的中文, 总长度不超过 40 个字。"
)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并字典, override 优先。"""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_dotenv(path: Path = ENV_PATH) -> Dict[str, str]:
    """读取 .env 文件, 不覆盖已有环境变量。返回解析出的键值。"""
    values: Dict[str, str] = {}
    if not path or not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        values[key] = val
        os.environ.setdefault(key, val)
    return values


@dataclass
class ProviderConfig:
    """一个 LLM provider 预设 (OpenAI 兼容协议)。

    一份 provider 预设描述「去哪家、用哪个模型、怎么鉴权」。
    多个角色 (chat / classify / tool) 可以各自绑定不同的 provider,
    也可以全部指向同一个 —— 默认就是后者。
    """

    #: 预设名 (configs/agent.yaml 里 `llm.providers` 的 key)
    name: str = ""
    #: OpenAI 兼容接口根地址, 形如 https://api.xxx.com/v1
    base_url: str = ""
    #: 默认模型名
    model: str = ""
    #: 直接写在 yaml 里的 key (本地无鉴权服务用占位值即可)
    api_key: str = ""
    #: 从哪个环境变量读 key (优先于上面的 api_key, 避免密钥进版本库)
    api_key_env: str = ""
    #: 超时秒数 (0 表示继承全局)
    timeout: float = 0.0
    #: 重试次数 (0 表示继承全局)
    max_retries: int = 0
    #: 温度 (None 表示继承全局)
    temperature: Optional[float] = None
    #: 生成长度上限 (0 表示继承全局)
    max_tokens: int = 0
    #: 是否支持原生 function calling
    supports_tools: bool = True
    #: 是否支持流式输出
    supports_stream: bool = True
    #: 备注 (如「本地服务, 无需 key」)
    notes: str = ""

    @property
    def has_key(self) -> bool:
        """是否配了可用的 key。"""
        return bool(self.api_key)

    def describe(self) -> str:
        """一行摘要 (不含密钥), 给日志和 /health 用。"""
        return f"{self.name} @ {self.base_url} model={self.model}"


@dataclass
class RoleConfig:
    """一个角色的 LLM 绑定。

    角色决定「这个用途用哪家模型」:
    - chat     -> 闲聊回复
    - classify -> 意图分类 (router 的 LLM 兜底)
    - tool     -> 原生 function calling
    """

    #: 主 provider 名
    provider: str = ""
    #: 覆盖该 provider 的默认模型 (留空则用 provider 的)
    model: str = ""
    #: 主 provider 失败后依次尝试的备用 provider
    fallback: List[str] = field(default_factory=list)
    #: 角色级温度覆盖 (None 表示继承)
    temperature: Optional[float] = None
    #: 角色级生成长度覆盖 (0 表示继承)
    max_tokens: int = 0


@dataclass
class LLMConfig:
    """LLM 配置容器。

    `providers` 是全部预设, `roles` 是角色 -> provider 的绑定。
    下面这些扁平字段是**全局默认**, 供 provider 未单独指定时继承,
    同时保持对旧代码 (cfg.llm.model 等) 的兼容。
    """

    #: 全部 provider 预设
    providers: Dict[str, ProviderConfig] = field(default_factory=dict)
    #: 角色 -> 绑定
    roles: Dict[str, RoleConfig] = field(default_factory=dict)
    #: 默认 provider 名 (角色未显式指定时用它)
    default_provider: str = ""
    #: 扁平字段 = 默认 provider 的取值 (兼容旧调用)
    base_url: str = "https://api.agnes-ai.cn/v1"
    model: str = "agnes-3.0-flash"
    api_key: str = ""
    timeout: float = 60.0
    max_retries: int = 3
    temperature: float = 0.7
    max_tokens: int = 2048

    @property
    def provider_names(self) -> List[str]:
        return list(self.providers.keys())

    def role(self, name: str) -> RoleConfig:
        """取角色配置; 没配就返回一个指向 default_provider 的空壳。"""
        cfg = self.roles.get(name)
        if cfg is not None:
            return cfg
        return RoleConfig(provider=self.default_provider)

    def role_names(self) -> List[str]:
        return list(self.roles.keys())


@dataclass
class ThinkingConfig:
    """Thinking Mode 配置 (仅在 NLU 阶段使用, 改进复合意图理解)。"""

    #: 是否启用 Thinking Mode
    enabled: bool = False
    #: 思考预算 (tokens)
    budget_tokens: int = 8000
    #: 最少任务数才启用 thinking (简单查询不用 thinking 节省成本)
    min_complexity: int = 2


@dataclass
class ToolCallingConfig:
    """原生 function calling 配置 (router.tool_calling)。

    规则命中时完全不进这条路 —— 它只负责「规则没命中」时的升级路径。
    """

    #: 是否启用
    enabled: bool = False
    #: 用哪个模型 (留空则用 llm.model)
    model: str = ""
    #: 超时秒数; 超时直接回落默认分支 (语音场景必须快)
    timeout: float = 5.0
    #: 工具选择是确定性的, 温度设 0
    temperature: float = 0.0
    #: 工具选择不需要长输出
    max_tokens: int = 256
    #: auto | none | required
    #: **不要用 required** —— 纯闲聊也会被强行调工具
    tool_choice: str = "auto"
    #: 工具结果回灌的最大轮数 (硬上限, 防死循环)
    max_rounds: int = 1
    #: 执行工具后是否让模型再生成一句口语化总结
    #: false = 用本地固定文案 (零额外延迟, TTS 合成也更快)
    feedback_round: bool = False
    #: tool calling 专用的系统提示词
    #: **不能直接用 system_prompt** —— 那是聊天用的, 会让模型用文字
    #: 「描述」动作已完成, 而不是真的去调工具 (假确认)。
    system_prompt: str = ""
    #: Thinking Mode 配置 (改进复合意图理解)
    thinking: ThinkingConfig = field(default_factory=ThinkingConfig)


@dataclass
class RouterConfig:
    mode: str = "hybrid"          # rules | llm | hybrid
    llm_fallback_model: str = ""
    default_route: str = "chat"
    rules: List[Dict[str, Any]] = field(default_factory=list)
    #: LLM 兜底分类的超时秒数; 超时直接回落 default_route (语音场景必须快)
    llm_timeout: float = 3.0
    #: 规则命中后是否直接相信 (跳过 LLM 二次确认)
    trust_rules: bool = True
    #: 规则命中的关键词是否要求独占整句 (避免"打开空调"里的"气"误伤)
    strict_keywords: bool = False
    #: 原生 function calling 配置 (规则未命中时的升级路径)
    tool_calling: ToolCallingConfig = field(default_factory=ToolCallingConfig)


@dataclass
class HandlerConfig:
    name: str
    module: str
    cls: str
    enabled: bool = True
    speak: bool = False
    #: 分支自定义选项 (原样透传给 handler, 由 handler 自己解释)
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionConfig:
    max_turns: int = 6
    ttl_seconds: int = 1800


@dataclass
class ArmActionConfig:
    """机械臂语音指令映射表 (configs/actions.yaml)。"""

    #: 机械臂动作服务地址
    base_url: str = "http://127.0.0.1:8802"
    #: 调用超时 (秒)
    timeout: float = 5.0
    #: 疑问句式 —— 命中则不算动作指令
    exclude: List[str] = field(default_factory=list)
    #: 动作条目: [{"action": "挥手", "say": ["挥个手"]}, ...]
    actions: List[Dict[str, Any]] = field(default_factory=list)
    #: 是否成功读到过映射表
    loaded: bool = False

    @property
    def say_phrases(self) -> List[str]:
        """所有说法 (展平), 供 router 生成规则。"""
        out: List[str] = []
        for item in self.actions:
            for phrase in item.get("say") or []:
                out.append(str(phrase))
        return out

    @property
    def action_names(self) -> List[str]:
        """所有动作名 (去重, 按配置顺序)。"""
        out: List[str] = []
        for item in self.actions:
            name = str(item.get("action", ""))
            if name and name not in out:
                out.append(name)
        return out


def load_arm_actions(path: Path = ACTIONS_CONFIG_PATH) -> ArmActionConfig:
    """加载机械臂语音指令映射表。

    文件缺失不算错误 —— device 分支会退化成「只按动作名精确匹配」。
    """
    if not path or not Path(path).is_file():
        return ArmActionConfig()

    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:  # pragma: no cover - 配置坏了不该让服务起不来
        return ArmActionConfig()

    arm = data.get("arm") or {}
    actions = []
    for item in arm.get("actions") or []:
        name = str(item.get("action", "") or "").strip()
        if not name:
            continue
        say = [str(x).strip() for x in (item.get("say") or []) if str(x).strip()]
        actions.append({"action": name, "say": say})

    cfg = ArmActionConfig(
        base_url=os.environ.get(
            "ARM_SERVICE_URL", arm.get("base_url", "http://127.0.0.1:8802")
        ),
        timeout=float(arm.get("timeout", 5.0)),
        exclude=[str(x) for x in (arm.get("exclude") or [])],
        actions=actions,
        loaded=True,
    )
    logger.info(
        "[机械臂] 已加载 %d 个动作说法: %s",
        len(cfg.actions),
        ", ".join(cfg.action_names),
    )
    return cfg


@dataclass
class ToolSpecConfig:
    """一个 tool 的原始定义 (来自 configs/tools.yaml)。

    这里存的是**未加工**的原始字段, 加工 (补默认值、生成 schema) 在
    `action_tools.py` 里做 —— 两边职责分开: 配置只负责「写什么」,
    代码负责「怎么变成 tool」。
    """

    #: function 名
    name: str = ""
    #: 工具类别: arm / device / query
    kind: str = "device"
    #: 中文名
    label: str = ""
    #: 给模型看的说明
    description: str = ""
    #: 结果是否朗读 (None = 按 kind 推断)
    speak: Optional[bool] = None
    #: 命中后走哪个分支 (None = 按 kind 推断)
    intent: Optional[str] = None
    #: 用哪个参数的值决定内部动作名
    value_arg: str = ""
    #: 参数值 -> 内部动作名
    action_map: Dict[str, str] = field(default_factory=dict)
    #: JSON Schema (parameters 段)
    parameters: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ToolConfig:
    """tool 定义清单 (configs/tools.yaml)。"""

    #: 工具条目
    tools: List[ToolSpecConfig] = field(default_factory=list)
    #: 是否成功读到过
    loaded: bool = False

    @property
    def names(self) -> List[str]:
        return [t.name for t in self.tools if t.name]


def _yaml_key_to_str(key: Any) -> str:
    """把 yaml 的 key 统一成小写字符串。

    **YAML 1.1 的坑**: 裸写的 `on` / `off` / `yes` / `no` 会被解析成布尔值,
    `str(True)` 得到 `"True"` —— 与 `"on"` 对不上, action_map 就查不到。
    这里统一还原成小写, 保证 `on:` 和 `"on":` 行为一致。
    """
    if isinstance(key, bool):
        return "on" if key else "off"
    return str(key)


def load_tool_specs(path: Path = TOOLS_CONFIG_PATH) -> ToolConfig:
    """加载 tool 定义清单。

    文件缺失不算错误 —— 只是没有自定义工具, 机械臂那一个仍会生成。
    """
    if not path or not Path(path).is_file():
        return ToolConfig()
    if yaml is None:
        return ToolConfig()

    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception as exc:  # pragma: no cover - 配置坏了不该让服务起不来
        logger.warning("[工具] 读取 %s 失败: %s", path, exc)
        return ToolConfig()

    specs: List[ToolSpecConfig] = []
    seen: set = set()
    for item in data.get("tools") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        if not name:
            logger.warning("[工具] 有一条定义缺 name, 跳过")
            continue
        if name in seen:
            logger.warning("[工具] 重复的 name %r, 跳过", name)
            continue
        seen.add(name)

        raw_map = item.get("action_map") or {}
        action_map: Dict[str, str] = {}
        if isinstance(raw_map, dict):
            for k, v in raw_map.items():
                # YAML 1.1 会把裸 on/off/yes/no 解析成布尔值, 这里还原成字符串。
                # 不还原的话 action_map 的键会变成 "True"/"False", 查不到。
                key = _yaml_key_to_str(k)
                action_map[key] = str(v)

        speak = item.get("speak")
        intent = item.get("intent")
        specs.append(
            ToolSpecConfig(
                name=name,
                kind=str(item.get("kind", "device") or "device"),
                label=str(item.get("label", name) or name),
                description=str(item.get("description", "") or ""),
                speak=None if speak is None else bool(speak),
                intent=str(intent) if intent else None,
                value_arg=str(item.get("value_arg", "") or ""),
                action_map=action_map,
                parameters=dict(item.get("parameters") or {}),
            )
        )

    logger.info("[工具] 已加载 %d 个 tool 定义: %s", len(specs), ", ".join(specs and [s.name for s in specs]))
    return ToolConfig(tools=specs, loaded=True)


@dataclass
class Config:
    llm: LLMConfig
    router: RouterConfig
    handlers: Dict[str, HandlerConfig]
    session: SessionConfig
    system_prompt: str
    router_prompt: str
    #: 机械臂语音指令映射表
    arm_actions: ArmActionConfig = field(default_factory=ArmActionConfig)
    #: tool 定义清单 (configs/tools.yaml)
    tool_specs: ToolConfig = field(default_factory=ToolConfig)

    @property
    def enabled_handlers(self) -> List[HandlerConfig]:
        return [h for h in self.handlers.values() if h.enabled]


#: 三个角色的名字 —— 缺省时全部指向 default_provider。
DEFAULT_ROLES = ("chat", "classify", "tool")


def _resolve_api_key(
    name: str,
    raw: Dict[str, Any],
    default_key: str = "",
) -> str:
    """解析一个 provider 的 API key。

    顺序: yaml 里的 api_key -> api_key_env 指定的环境变量
    -> `<NAME>_API_KEY` -> `LLM_API_KEY`。
    """
    direct = str(raw.get("api_key") or "").strip()
    if direct:
        return direct

    env_name = str(raw.get("api_key_env") or "").strip()
    if env_name:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value

    for candidate in (f"{name.upper().replace('-', '_')}_API_KEY", "LLM_API_KEY"):
        value = os.environ.get(candidate, "").strip()
        if value:
            return value

    return default_key


def _load_providers(
    llm_raw: Dict[str, Any],
) -> Dict[str, ProviderConfig]:
    """解析 `llm.providers` 预设表。

    **兼容旧写法**: 没有 `providers` 段时, 用扁平的 `llm.base_url` /
    `llm.model` 自动合成一个名为 `agnes` 的预设 —— 现有配置零改动仍能跑。
    """
    raw_providers = llm_raw.get("providers") or {}
    providers: Dict[str, ProviderConfig] = {}

    if isinstance(raw_providers, dict):
        for name, item in raw_providers.items():
            if not isinstance(item, dict):
                logger.warning("[LLM] provider %r 定义不是字典, 跳过", name)
                continue
            pname = str(name).strip()
            if not pname:
                continue
            providers[pname] = ProviderConfig(
                name=pname,
                base_url=str(item.get("base_url") or "").rstrip("/"),
                model=str(item.get("model") or ""),
                api_key=_resolve_api_key(pname, item),
                api_key_env=str(item.get("api_key_env") or "").strip(),
                timeout=float(item.get("timeout") or 0),
                max_retries=int(item.get("max_retries") or 0),
                temperature=(
                    float(item["temperature"])
                    if item.get("temperature") is not None
                    else None
                ),
                max_tokens=int(item.get("max_tokens") or 0),
                supports_tools=bool(item.get("supports_tools", True)),
                supports_stream=bool(item.get("supports_stream", True)),
                notes=str(item.get("notes") or ""),
            )

    if providers:
        return providers

    # ---- 旧写法兜底 ----
    legacy_url = str(llm_raw.get("base_url") or "").rstrip("/")
    legacy_model = str(llm_raw.get("model") or "")
    providers["agnes"] = ProviderConfig(
        name="agnes",
        base_url=legacy_url,
        model=legacy_model,
        api_key=os.environ.get("LLM_API_KEY", ""),
    )
    logger.info("[LLM] 未配置 llm.providers, 按旧写法合成预设 agnes")
    return providers


def _load_roles(
    llm_raw: Dict[str, Any],
    providers: Dict[str, ProviderConfig],
    default_provider: str,
) -> Tuple[Dict[str, RoleConfig], set]:
    """解析 `llm.roles` 角色绑定。

    **缺省时三个角色全部指向同一个 provider** —— 这就是「默认每个角色
    用相同的 LLM」。想拆开就在 yaml 里显式写 `llm.roles.<role>.provider`。

    返回 (角色表, 显式配置过的角色名集合)。显式配置过的角色**不再**
    受 deprecated 的 `router.llm_fallback_model` / `router.tool_calling.model`
    影响 —— 后者的优先级更低。
    """
    raw_roles = llm_raw.get("roles") or {}
    roles: Dict[str, RoleConfig] = {}
    explicit: set = set()

    if isinstance(raw_roles, dict):
        for name, item in raw_roles.items():
            if not isinstance(item, dict):
                logger.warning("[LLM] 角色 %r 定义不是字典, 跳过", name)
                continue
            rname = str(name).strip()
            if not rname:
                continue
            explicit.add(rname)
            provider = str(item.get("provider") or default_provider).strip()
            roles[rname] = RoleConfig(
                provider=provider,
                model=str(item.get("model") or ""),
                fallback=[str(x).strip() for x in (item.get("fallback") or []) if str(x).strip()],
                temperature=(
                    float(item["temperature"])
                    if item.get("temperature") is not None
                    else None
                ),
                max_tokens=int(item.get("max_tokens") or 0),
            )

    # 缺省补齐 —— 三个角色都用 default_provider
    for rname in DEFAULT_ROLES:
        if rname not in roles:
            roles[rname] = RoleConfig(provider=default_provider)

    # 校验: 引用了不存在的 provider 要**启动即报错**, 别等到运行时才发现
    for rname, rc in roles.items():
        for pname in [rc.provider] + list(rc.fallback):
            if pname and pname not in providers:
                raise RuntimeError(
                    f"[LLM] 角色 {rname!r} 引用了未定义的 provider {pname!r}; "
                    f"可用: {', '.join(sorted(providers)) or '(无)'}"
                )
    return roles, explicit


def load_config(path: Path = AGENT_CONFIG_PATH) -> Config:
    """加载配置。YAML 缺失或格式错误时回退到默认值。"""
    data: Dict[str, Any] = {}
    if yaml is None:
        raise RuntimeError("缺少 pyyaml 依赖, 请在当前 conda 环境执行: pip install pyyaml")
    if path and Path(path).is_file():
        raw = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
    merged = _deep_merge(DEFAULTS, data)

    load_dotenv()

    llm_raw = merged.get("llm", {})
    providers = _load_providers(llm_raw)

    # 默认 provider: yaml 的 llm.default_provider -> 环境变量 LLM_PROVIDER
    # -> 预设表第一个
    default_provider = str(llm_raw.get("default_provider") or "").strip()
    default_provider = os.environ.get("LLM_PROVIDER", default_provider).strip()
    if not default_provider:
        default_provider = next(iter(providers), "")
    if default_provider and default_provider not in providers:
        raise RuntimeError(
            f"[LLM] 默认 provider {default_provider!r} 未定义; "
            f"可用: {', '.join(sorted(providers)) or '(无)'}"
        )

    # 全局默认 (provider 未单独指定时继承)
    g_timeout = float(llm_raw.get("timeout", 60))
    g_max_retries = int(llm_raw.get("max_retries", 3))
    g_temperature = float(llm_raw.get("temperature", 0.7))
    g_max_tokens = int(llm_raw.get("max_tokens", 2048))

    # 环境变量只覆盖 default_provider —— 避免「以为在测 A 家, 模型名还是 B 家」
    if default_provider:
        dp = providers[default_provider]
        env_base = os.environ.get("LLM_BASE_URL", "").strip()
        env_model = os.environ.get("LLM_MODEL", "").strip()
        env_key = os.environ.get("LLM_API_KEY", "").strip()
        if env_base:
            dp.base_url = env_base.rstrip("/")
            logger.warning(
                "[LLM] 环境变量 LLM_BASE_URL 覆盖了 %s 的 base_url -> %s",
                default_provider, dp.base_url,
            )
        if env_model:
            dp.model = env_model
            logger.warning(
                "[LLM] 环境变量 LLM_MODEL 覆盖了 %s 的模型名 -> %s",
                default_provider, dp.model,
            )
        if env_key:
            dp.api_key = env_key

    roles, explicit_roles = _load_roles(llm_raw, providers, default_provider)

    # 扁平字段 = default_provider 的取值, 兼容旧代码 (cfg.llm.model 等)
    dp = providers.get(default_provider)
    llm = LLMConfig(
        providers=providers,
        roles=roles,
        default_provider=default_provider,
        base_url=(dp.base_url if dp else ""),
        model=(dp.model if dp else ""),
        api_key=(dp.api_key if dp else ""),
        timeout=g_timeout,
        max_retries=g_max_retries,
        temperature=g_temperature,
        max_tokens=g_max_tokens,
    )

    # provider 未单独指定的项继承全局默认
    for p in providers.values():
        if not p.timeout:
            p.timeout = g_timeout
        if not p.max_retries:
            p.max_retries = g_max_retries
        if p.temperature is None:
            p.temperature = g_temperature
        if not p.max_tokens:
            p.max_tokens = g_max_tokens

    logger.info(
        "[LLM] provider=%s roles=%s 可用预设=%s",
        default_provider,
        {k: v.provider for k, v in roles.items()},
        ", ".join(sorted(providers)) or "(无)",
    )

    router_raw = merged.get("router", {})
    tc_raw = router_raw.get("tool_calling") or {}
    
    # 解析 thinking 配置
    thinking_raw = tc_raw.get("thinking") or {}
    thinking_config = ThinkingConfig(
        enabled=bool(thinking_raw.get("enabled", False)),
        budget_tokens=int(thinking_raw.get("budget_tokens", 8000)),
        min_complexity=int(thinking_raw.get("min_complexity", 2)),
    )
    
    tool_calling = ToolCallingConfig(
        enabled=bool(tc_raw.get("enabled", False)),
        model=str(tc_raw.get("model") or ""),
        timeout=float(tc_raw.get("timeout", 5.0)),
        temperature=float(tc_raw.get("temperature", 0.0)),
        max_tokens=int(tc_raw.get("max_tokens", 256)),
        tool_choice=str(tc_raw.get("tool_choice", "auto")),
        max_rounds=int(tc_raw.get("max_rounds", 1)),
        feedback_round=bool(tc_raw.get("feedback_round", False)),
        system_prompt=str(tc_raw.get("system_prompt") or DEFAULT_TOOL_CALLING_PROMPT),
        thinking=thinking_config,
    )

    # **deprecated 的模型名** —— `router.llm_fallback_model` 与
    # `router.tool_calling.model` 是旧写法, 优先级**低于** `llm.roles`。
    # 只有当对应角色没在 llm.roles 里显式指定模型时, 才用它来补上。
    deprecated_fallback = str(router_raw.get("llm_fallback_model") or "").strip()
    deprecated_tool = str(tc_raw.get("model") or "").strip()

    if "classify" in explicit_roles:
        # 角色显式配了 -> 忽略 deprecated 值
        classify_model = roles["classify"].model
    else:
        classify_model = deprecated_fallback
        if classify_model:
            roles["classify"].model = classify_model

    if "tool" in explicit_roles:
        tool_model = roles["tool"].model
    else:
        tool_model = deprecated_tool
        if tool_model:
            roles["tool"].model = tool_model

    router = RouterConfig(
        mode=router_raw.get("mode", "hybrid"),
        # deprecated: 优先级低于 llm.roles.classify
        llm_fallback_model=classify_model or llm.model,
        default_route=router_raw.get("default_route", "chat"),
        rules=list(router_raw.get("rules") or []),
        llm_timeout=float(router_raw.get("llm_timeout", 3.0)),
        trust_rules=bool(router_raw.get("trust_rules", True)),
        strict_keywords=bool(router_raw.get("strict_keywords", False)),
        tool_calling=tool_calling,
    )

    handlers: Dict[str, HandlerConfig] = {}
    for name, h in (merged.get("handlers") or {}).items():
        handlers[name] = HandlerConfig(
            name=name,
            module=h.get("module", ""),
            cls=h.get("class", ""),
            enabled=bool(h.get("enabled", True)),
            speak=bool(h.get("speak", False)),
            options=dict(h.get("options") or {}),
        )

    s = merged.get("session", {})
    session = SessionConfig(
        max_turns=int(s.get("max_turns", 6)),
        ttl_seconds=int(s.get("ttl_seconds", 1800)),
    )

    return Config(
        llm=llm,
        router=router,
        handlers=handlers,
        session=session,
        system_prompt=merged.get("system_prompt", "").strip(),
        router_prompt=merged.get("router_prompt", "").strip(),
        arm_actions=load_arm_actions(),
        tool_specs=load_tool_specs(),
    )
