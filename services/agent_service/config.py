"""Agent 服务配置加载。

配置来源优先级 (后者覆盖前者):
1. 本文件内的默认值
2. configs/agent.yaml
3. 环境变量 (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL)
4. configs/.env (仅当环境变量未设置时生效)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

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
class LLMConfig:
    base_url: str = "https://api.agnes-ai.cn/v1"
    model: str = "agnes-3.0-flash"
    api_key: str = ""
    timeout: float = 60.0
    max_retries: int = 3
    temperature: float = 0.7
    max_tokens: int = 2048


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
    llm = LLMConfig(
        base_url=os.environ.get("LLM_BASE_URL", llm_raw.get("base_url", "")).rstrip("/"),
        model=os.environ.get("LLM_MODEL", llm_raw.get("model", "")),
        api_key=os.environ.get("LLM_API_KEY", ""),
        timeout=float(llm_raw.get("timeout", 60)),
        max_retries=int(llm_raw.get("max_retries", 3)),
        temperature=float(llm_raw.get("temperature", 0.7)),
        max_tokens=int(llm_raw.get("max_tokens", 2048)),
    )

    router_raw = merged.get("router", {})
    tc_raw = router_raw.get("tool_calling") or {}
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
    )
    router = RouterConfig(
        mode=router_raw.get("mode", "hybrid"),
        llm_fallback_model=router_raw.get("llm_fallback_model") or llm.model,
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
