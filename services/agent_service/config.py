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
from typing import Any, Dict, List

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
class Config:
    llm: LLMConfig
    router: RouterConfig
    handlers: Dict[str, HandlerConfig]
    session: SessionConfig
    system_prompt: str
    router_prompt: str
    #: 机械臂语音指令映射表
    arm_actions: ArmActionConfig = field(default_factory=ArmActionConfig)

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
    router = RouterConfig(
        mode=router_raw.get("mode", "hybrid"),
        llm_fallback_model=router_raw.get("llm_fallback_model") or llm.model,
        default_route=router_raw.get("default_route", "chat"),
        rules=list(router_raw.get("rules") or []),
        llm_timeout=float(router_raw.get("llm_timeout", 3.0)),
        trust_rules=bool(router_raw.get("trust_rules", True)),
        strict_keywords=bool(router_raw.get("strict_keywords", False)),
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
    )
