"""统一动作层: 把机械臂动作与设备工具收敛成同一套 tool schema。

**为什么要有这一层**: 原来动作识别散在三处 ——
- `handlers/device_handler.py` 关键词匹配 + LLM 兜底
- `handlers/tool_handler.py` 正则匹配 TOOLS 表
- `router.py` 关键词规则 + LLM 分类

三个地方各写各的, 加一个动作要改三处。现在统一成一份 tool 定义,
既能给 LLM 的原生 function calling 用, 也能给规则匹配用。

**tool 定义放在 yaml 里** (`configs/tools.yaml`):
加一个工具 = 在 yaml 里加一条, 不用改 Python。只有「实际执行逻辑」
才需要动 `handlers/tool_handler.py`。

**tool 的划分原则** (Agnes 文档: 「工具描述与 JSON Schema 应保持精确、聚焦」):

- 机械臂: **一个** function `play_arm_action`, 动作名用 enum 列全。
  6 个动作就 6 个 function 会让 schema 冗余, 而且模型要在 6 个近似名字里
  挑 —— enum 单函数更准。
- 设备: 按设备聚合成 `control_light(state)` / `control_aircon(state)`,
  而不是 `light_on` / `light_off` 两个 function。「打开空调」这类话
  对应「选对设备 + 选对状态」, 让模型一次决定比让它在两个函数间二选一准。

**安全**: 机械臂的执行路径仍然经过 `DeviceHandler.execute()` ——
那条路径里有安全门阀预检。**这里不做任何旁路**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agent.actions")

#: 工具类别
KIND_ARM = "arm"        #: 机械臂动作
KIND_DEVICE = "device"  #: 设备控制 (结果不需要朗读)
KIND_QUERY = "query"    #: 信息查询 (结果要朗读)

#: 类别 -> 默认分支
INTENT_BY_KIND: Dict[str, str] = {
    KIND_ARM: "device",
    KIND_DEVICE: "tool",
    KIND_QUERY: "tool",
}

#: 类别 -> 默认是否朗读
SPEAK_BY_KIND: Dict[str, bool] = {
    KIND_ARM: True,
    KIND_DEVICE: False,
    KIND_QUERY: True,
}


class ActionError(RuntimeError):
    """动作执行失败 (参数非法 / 执行出错)。"""


@dataclass
class ActionSpec:
    """一个 tool 的完整定义。"""

    #: OpenAI function 名 (模型看到并调用这个名字)
    name: str
    #: 工具类别
    kind: str
    #: 中文名, 用于日志与回报
    label: str
    #: 给模型看的说明
    description: str = ""
    #: JSON Schema (parameters 段)
    parameters: Dict[str, Any] = field(default_factory=dict)
    #: 结果是否要朗读。
    #: None = 没显式指定, 交给执行时按内部动作判定 (与规则路径保持一致)。
    speak: Optional[bool] = None
    #: 命中后走哪个分支 (chat / tool / device)
    intent: str = "tool"
    #: 决定内部动作名的参数名 (如 control_light 的 "state")。
    #: 空字符串表示这个 tool 只对应一个固定动作。
    value_arg: str = ""
    #: 参数值 -> 内部动作名
    #: 例: control_light 的 {"on": "light_on", "off": "light_off"}
    action_map: Dict[str, str] = field(default_factory=dict)

    def to_openai(self) -> Dict[str, Any]:
        """转成 OpenAI tools 数组里的一项。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


# ------------------------------------------------------------ 参数校验


def validate_args(spec: ActionSpec, args: Dict[str, Any]) -> Dict[str, Any]:
    """按 JSON Schema 校验参数。

    只实现 schema 里用得到的部分 (type / enum / required) —— 够挡住
    「模型幻觉出不存在的动作名」和「参数类型不对」这两类真实问题,
    不引入 jsonschema 依赖。

    校验失败抛 `ActionError`。
    """
    schema = spec.parameters or {}
    properties: Dict[str, Any] = schema.get("properties") or {}
    required: List[str] = list(schema.get("required") or [])

    cleaned: Dict[str, Any] = {}
    for key in required:
        if key not in args or args[key] in (None, ""):
            raise ActionError(f"{spec.label} 缺少必填参数: {key}")

    for key, value in args.items():
        prop = properties.get(key)
        if prop is None:
            # schema 没声明的参数直接丢掉 —— 模型多给字段不该影响执行
            continue
        expected = prop.get("type")
        if expected == "string" and not isinstance(value, str):
            value = str(value)
        elif expected == "number":
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ActionError(f"{spec.label} 参数 {key} 必须是数字") from None
        elif expected == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ActionError(f"{spec.label} 参数 {key} 必须是整数") from None
        elif expected == "boolean":
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
        elif expected == "array":
            value = _validate_array(spec, key, value, prop)

        enum = prop.get("enum")
        if enum and value not in enum:
            # enum 不匹配时把合法值报出来 —— 排查时一眼能看出模型想干什么
            raise ActionError(
                f"{spec.label} 参数 {key} 的值 {value!r} 不合法, "
                f"合法值: {', '.join(str(x) for x in enum)}"
            )
        cleaned[key] = value

    for key in required:
        if key not in cleaned:
            raise ActionError(f"{spec.label} 缺少必填参数: {key}")

    return cleaned


def _validate_array(
    spec: ActionSpec, key: str, value: Any, prop: Dict[str, Any]
) -> List[Any]:
    """校验数组参数。

    机械臂的 `actions` 就是数组 —— 每一项都要过 item 的 enum 校验,
    否则模型填了不存在的动作名, 会在服务端才报错。
    """
    items = prop.get("items") or {}
    item_enum = items.get("enum") or []
    item_type = items.get("type")

    if isinstance(value, str):
        # 模型偶尔会把单元素数组写成裸字符串 —— 容忍一下
        value = [value]
    elif isinstance(value, (list, tuple)):
        value = list(value)
    else:
        raise ActionError(f"{spec.label} 参数 {key} 必须是数组")

    out: List[Any] = []
    for item in value:
        if item_type == "string" and not isinstance(item, str):
            item = str(item)
        if item_enum and item not in item_enum:
            raise ActionError(
                f"{spec.label} 参数 {key} 的值 {item!r} 不合法, "
                f"合法值: {', '.join(str(x) for x in item_enum)}"
            )
        out.append(item)

    if not out:
        raise ActionError(f"{spec.label} 参数 {key} 不能为空数组")
    return out


def _as_name_list(raw: Any) -> List[str]:
    """把参数值归一化成动作名列表。

    模型的输出可能是:
    - ``["挥手", "准备点"]`` —— 数组
    - ``"挥手"`` —— 单个字符串 (模型有时会偷懒)
    - ``["挥手", "挥手"]`` —— 重复 (去重但保序)
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = [raw]

    out: List[str] = []
    for item in items:
        name = str(item or "").strip()
        if name and name not in out:
            out.append(name)
    return out


# ------------------------------------------------------------ tool 定义构建


def build_arm_spec(cfg: Any) -> Optional[ActionSpec]:
    """从 configs/actions.yaml 生成机械臂 tool。

    动作名来自配置, 所以加动作只改 yaml, 不改代码。
    """
    arm = getattr(cfg, "arm_actions", None)
    if arm is None:
        return None
    actions = list(getattr(arm, "actions", []) or [])
    if not actions:
        return None

    names: List[str] = []
    say_map: Dict[str, List[str]] = {}
    for item in actions:
        name = str(item.get("action") or "").strip()
        if not name or name in names:
            continue
        names.append(name)
        say_map[name] = [str(p) for p in (item.get("say") or [])]

    # 描述里带上说法 —— 模型靠它把口语映射到动作名
    say_lines = []
    for name in names:
        says = say_map.get(name) or []
        if says:
            say_lines.append(f"{name} ({'、'.join(says)})")
        else:
            say_lines.append(name)

    return ActionSpec(
        name="play_arm_action",
        kind=KIND_ARM,
        label="机械臂动作",
        description=(
            "让机械臂执行预置动作。"
            f"可用动作: {'; '.join(say_lines)}。"
            "用户要求多个动作时, 按他说的先后顺序填进 actions 数组 "
            "(如「先准备点再挥手」-> [\"准备点\", \"挥手\"])。"
            "只在用户明确要求机器人做出这些动作时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": names,
                    },
                    "description": (
                        "要依次执行的动作名, 按用户说的顺序。"
                        "只要求一个动作时也用这个字段, 填单元素数组。"
                    ),
                },
                "speed": {
                    "type": "number",
                    "description": "播放倍速, 1.0 是原速, 范围 0.1~3.0",
                },
            },
            "required": ["actions"],
        },
        speak=True,
        intent=INTENT_BY_KIND[KIND_ARM],
    )


def build_spec_from_config(raw: Any) -> ActionSpec:
    """把一条 yaml tool 定义加工成 `ActionSpec`。

    yaml 只负责「写什么」, 这里负责「怎么变成 tool」:
    - 补默认 kind / label / speak / intent
    - 校验必填字段 (没有 action_map 的 tool 无法确定执行什么)
    """
    name = str(getattr(raw, "name", "") or "").strip()
    if not name:
        raise ActionError("tool 定义缺少 name")

    kind = str(getattr(raw, "kind", KIND_DEVICE) or KIND_DEVICE)
    label = str(getattr(raw, "label", "") or name)

    # speak / intent 没写就按 kind 推断
    speak = getattr(raw, "speak", None)
    intent = getattr(raw, "intent", None) or INTENT_BY_KIND.get(kind, "tool")

    action_map = dict(getattr(raw, "action_map", {}) or {})
    value_arg = str(getattr(raw, "value_arg", "") or "")

    # action_map 为空 = 这个 tool 无法确定要执行什么动作, 直接报错
    if not action_map:
        raise ActionError(f"tool {name} 缺少 action_map, 无法确定执行哪个动作")

    return ActionSpec(
        name=name,
        kind=kind,
        label=label,
        description=str(getattr(raw, "description", "") or ""),
        parameters=dict(getattr(raw, "parameters", {}) or {}),
        speak=speak,
        intent=intent,
        value_arg=value_arg,
        action_map=action_map,
    )


def build_config_specs(cfg: Any) -> List[ActionSpec]:
    """从 configs/tools.yaml 生成 tool 定义。

    单条定义坏了不应该让整个服务起不来 —— 跳过它并记日志。
    """
    tool_cfg = getattr(cfg, "tool_specs", None)
    if tool_cfg is None:
        return []

    specs: List[ActionSpec] = []
    for raw in list(getattr(tool_cfg, "tools", []) or []):
        try:
            specs.append(build_spec_from_config(raw))
        except ActionError as exc:
            logger.warning("[工具] 跳过一条定义: %s", exc)
    return specs


def build_tool_specs(cfg: Any) -> List[ActionSpec]:
    """生成全部 tool 定义。

    顺序: 机械臂 (自动生成) -> yaml 里定义的工具。
    机械臂放前面是因为它是「最需要被模型注意到」的那个。
    """
    specs: List[ActionSpec] = []
    arm = build_arm_spec(cfg)
    if arm is not None:
        specs.append(arm)
    specs.extend(build_config_specs(cfg))
    return specs


def to_openai_tools(specs: List[ActionSpec]) -> List[Dict[str, Any]]:
    """转成 OpenAI `tools` 数组。"""
    return [s.to_openai() for s in specs]


# ------------------------------------------------------------ 执行


@dataclass
class ActionOutcome:
    """一次工具执行的结果。"""

    ok: bool = True
    #: 内部动作名 (如 light_on / 挥手)
    action: str = ""
    #: 给用户听的回复
    reply: str = ""
    #: 要不要朗读
    speak: bool = False
    #: 附加信息 (状态、细节)
    detail: str = ""
    #: 完整的 action 载荷 (写进 DispatchResponse.action)
    payload: Dict[str, Any] = field(default_factory=dict)


class ActionCatalog:
    """tool 定义 + 执行。

    执行按 `kind` 分派:
    - `arm`   -> DeviceHandler.execute() (内含安全门阀预检)
    - 其他    -> ToolHandler 的内部动作实现

    **机械臂绝不走别的路** —— 安全门阀是硬要求, 不能因为模型选了 tool
    就绕过去。
    """

    def __init__(
        self,
        specs: List[ActionSpec],
        device_handler: Any = None,
        tool_handler: Any = None,
    ) -> None:
        self.specs = specs
        self._by_name = {s.name: s for s in specs}
        self._device_handler = device_handler
        self._tool_handler = tool_handler

    # ------------------------------------------------------------ 查询

    def get(self, name: str) -> Optional[ActionSpec]:
        return self._by_name.get(name)

    def to_openai(self) -> List[Dict[str, Any]]:
        return to_openai_tools(self.specs)

    # ------------------------------------------------------------ 执行

    async def execute(self, spec: ActionSpec, args: Dict[str, Any]) -> ActionOutcome:
        """执行一个 tool 调用。

        参数先校验再执行 —— 副作用动作不能让模型乱传参数。
        """
        cleaned = validate_args(spec, args or {})

        if spec.kind == KIND_ARM:
            return await self._execute_arm(spec, cleaned)
        return await self._execute_device(spec, cleaned)

    async def _execute_arm(self, spec: ActionSpec, args: Dict[str, Any]) -> ActionOutcome:
        """执行机械臂动作 (走 device_handler, 内含安全门阀)。"""
        if self._device_handler is None:
            raise ActionError("机械臂分支未装载")

        # 参数名是 actions (数组) —— 多个动作按顺序依次执行
        raw = args.get("actions")
        if raw is None:
            # 兼容旧格式 (单数)
            raw = args.get("action")
        names = _as_name_list(raw)
        if not names:
            raise ActionError("没指定要执行的动作")

        # 复用 device_handler 的实现 —— 规则路径与 tool calling 路径
        # 共用同一份安全门阀预检与播报文案, 行为完全一致。
        result = await self._device_handler.execute_many(names, args, source="tool_call")
        primary = names[0]
        return ActionOutcome(
            ok=(result.action or {}).get("status") in ("ok",),
            action=primary,
            reply=result.reply,
            speak=bool(result.speak),
            detail=str((result.action or {}).get("detail") or ""),
            payload=dict(result.action or {}),
        )

    async def _execute_device(self, spec: ActionSpec, args: Dict[str, Any]) -> ActionOutcome:
        """执行设备工具 / 查询工具。"""
        if self._tool_handler is None:
            raise ActionError("tool 分支未装载")

        # function 名 + 参数 -> 内部动作名
        # 例: control_light(state=off) -> light_off
        internal = ""
        if spec.value_arg:
            value = str(args.get(spec.value_arg, ""))
            internal = spec.action_map.get(value, "")
        else:
            internal = spec.action_map.get("", "")
        if not internal:
            raise ActionError(f"{spec.label} 无法确定要执行的动作")

        # 传给执行器的参数: 去掉用于选择动作的那个参数
        exec_args = {k: v for k, v in args.items() if k != spec.value_arg}
        status, detail = await self._tool_handler.execute(internal, exec_args)

        # 播报判定: yaml 里显式写了 speak 就用它, 没写则按内部动作判定
        # (与规则匹配路径保持一致 —— 同一个动作, 两条路径的播报行为一样)
        if spec.speak is None:
            speak = self._tool_handler.is_output_tool(internal)
        else:
            speak = bool(spec.speak)
        label = self._tool_handler.tool_label(internal)
        reply = detail if speak else f"已执行 {label}"

        return ActionOutcome(
            ok=(status == "ok"),
            action=internal,
            reply=reply,
            speak=speak,
            detail=detail,
            payload={
                "name": internal,
                "args": exec_args,
                "status": status,
                "detail": detail,
                "speak": speak,
            },
        )


__all__ = [
    "ActionCatalog",
    "ActionError",
    "ActionOutcome",
    "ActionSpec",
    "INTENT_BY_KIND",
    "KIND_ARM",
    "KIND_DEVICE",
    "KIND_QUERY",
    "SPEAK_BY_KIND",
    "build_arm_spec",
    "build_config_specs",
    "build_spec_from_config",
    "build_tool_specs",
    "to_openai_tools",
    "validate_args",
]
