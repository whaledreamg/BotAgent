"""物体抓取 skill —— 通用流程执行器。

流程定义在机械臂服务的 ``configs/flows/*.yaml`` 里 (见
``configs/flows/README.md``)。这个 skill **不硬编码任何步骤** ——
它拉流程定义, 按步骤类型分派执行, 收集每一步的结果。

**按物品分流**: 不同物品走不同流程 (``configs/flows/routing.yaml``
里配映射) —— 「抓瓶子」走 ``grasp_bottle``, 没配过的走默认流程
``grasp``。

扩展边界:

- 步骤**内容**与**参数** -> 只改 YAML, 不用动这里
- 步骤**类型** -> 要改这里的 ``_HANDLERS`` 注册表
- 物品到流程的**映射** -> 只改 YAML, 不用动这里
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from config import ROOT
from skill.base import Skill, SkillError
from skill.result import SkillResult
from skill.spec import SkillSpec

logger = logging.getLogger("skill.vision_grasp")

#: 抓取追溯记录 (JSONL, 每次抓取一行)。logs/ 已被 .gitignore 忽略
GRASP_TRACE_PATH = Path(__file__).resolve().parents[2] / "logs" / "grasp_trace.jsonl"

#: 物品名 -> 流程名 的映射 (configs/flows/routing.yaml)
#:
#: 路径从**项目根**算 —— 用 ``config.ROOT`` 而不是 ``parents[N]``:
#: 本模块在不同导入方式下深度不同 (``skill.builtin.vision_grasp`` vs
#: 直接当顶层模块), 写死 ``parents[N]`` 会在某种导入方式下指错地方。
ROUTING_CONFIG_PATH = ROOT / "configs" / "flows" / "routing.yaml"

#: 兜底的默认流程名 —— routing.yaml 没写 / 读不到时用它
DEFAULT_FLOW_NAME = "grasp"

#: 查映射前先剥掉的中文修饰词
#:
#: LLM 传的 target 可能带修饰词 (「那个瓶子」「一个瓶子」), 查表前
#: 剥掉才能对上表里的裸词。
#:
#: **只剥这些确定的词** —— 不做模糊匹配。猜错了比不猜更糟:
#: 走错流程可能抓不稳, 而走默认流程至少是「能用的通用流程」。
_TARGET_MODIFIERS = (
    "那个", "这个", "一个", "一只", "一张", "一把",
    "一根", "一颗", "一条", "一块",
    "那", "这", "的",
)

#: routing.yaml 里 routing 段下面的「物品名 -> 流程名」映射 (读一次就缓存)
_ROUTING_CACHE: Optional[Dict[str, str]] = None
#: routing.yaml 里的 default_flow (读一次就缓存)
_DEFAULT_FLOW_CACHE: Optional[str] = None


def _strip_modifiers(name: str) -> str:
    """剥掉中文修饰词, 留下裸词。

    「那个瓶子」-> 「瓶子」, 「一个杯子」-> 「杯子」。

    剥完还是空 (比如 target 就是「那个」) -> 返回原样。
    """
    text = (name or "").strip()
    if not text:
        return text
    changed = True
    while changed:
        changed = False
        for mod in _TARGET_MODIFIERS:
            if len(text) > len(mod) and text.startswith(mod):
                text = text[len(mod):].strip()
                changed = True
    return text or (name or "").strip()


def _load_routing() -> Tuple[Dict[str, str], str]:
    """读 routing.yaml, 返回 (映射, 默认流程名)。

    **读不到不算错误** —— 返回空映射 + 默认流程名, 抓取照常走默认流程。
    (配置文件缺失不该让整个服务起不来)

    读一次就缓存 —— 流程映射很少改, 每次抓取都读一遍文件太浪费。
    """
    global _ROUTING_CACHE, _DEFAULT_FLOW_CACHE
    if _ROUTING_CACHE is not None and _DEFAULT_FLOW_CACHE is not None:
        return _ROUTING_CACHE, _DEFAULT_FLOW_CACHE

    mapping: Dict[str, str] = {}
    default_flow = DEFAULT_FLOW_NAME
    try:
        import yaml

        raw = yaml.safe_load(
            ROUTING_CONFIG_PATH.read_text(encoding="utf-8")
        ) or {}
        if not isinstance(raw, dict):
            logger.warning(
                "[抓取] routing.yaml 格式不对 (期望字典), 忽略分流: %r",
                type(raw).__name__,
            )
        else:
            routing = raw.get("routing") or {}
            if isinstance(routing, dict):
                for key, value in routing.items():
                    name = str(key or "").strip()
                    flow = str(value or "").strip()
                    if name and flow:
                        mapping[name] = flow
            configured_default = str(raw.get("default_flow") or "").strip()
            if configured_default:
                default_flow = configured_default
    except FileNotFoundError:
        logger.info(
            "[抓取] 没有 routing.yaml, 一律走默认流程 %r", default_flow
        )
    except Exception as exc:
        logger.warning("[抓取] 读 routing.yaml 失败, 走默认流程: %s", exc)

    _ROUTING_CACHE = mapping
    _DEFAULT_FLOW_CACHE = default_flow
    return mapping, default_flow


#: 兜底的片段类型 —— 服务不可达时连流程定义都拉不到时的最小流程
#:
#: 只保留「能失败得干脆」的片段: 播准备点、拍照、移上去、移过去、闭爪。
#:
#: **格式与 configs/flows/*.yaml 一致** (片段语义) —— 服务端的
#: ``flow_from_legacy()`` 也产出同样的结构, 两条路径行为一致。
_FALLBACK_STEPS = [
    {"id": "ready", "type": "arm", "action": "准备点", "label": "回准备点"},
    {"id": "detect", "type": "detect", "duration": 1.5, "label": "拍照识别"},
    {"id": "object", "type": "target", "source": "detect", "label": "目标物体"},
    {"id": "above_point", "type": "target", "base": "object",
     "offset": {"x": 0.0, "y": 0.0, "z": 0.05}, "label": "目标上方"},
    {"id": "above", "type": "arm", "point": "above_point", "duration": 3.0,
     "label": "移到上方点"},
    {"id": "grasp", "type": "arm", "point": "object",
     "offset": {"x": 0.0, "y": 0.0, "z": 0.0}, "duration": 2.0,
     "label": "移到抓取点"},
    {"id": "close", "type": "hand", "action": "闭合夹爪", "label": "闭合夹爪"},
    {"id": "safe", "type": "arm", "action": "安全点", "label": "回安全点"},
]


class StepResult:
    """抓取流程里**一步**的结果。

    每一步都返回这个 —— 编排代码只看 ``ok`` 和 ``code``, 不关心具体
    细节, 细节都在 ``detail`` 里 (进追溯记录)。

    Attributes:
        ok:      这步成功了吗
        code:    机器可读的原因 ("ok" / "not_found" / "disabled" / ...)
        message: 能直接念给用户听的话
        detail:  进追溯记录的结构化细节
        name:    这步的 id (来自流程定义)
        label:   这步的说法 (来自流程定义), 播报时用
    """

    __slots__ = ("ok", "code", "message", "detail", "name", "label")

    def __init__(
        self,
        ok: bool,
        code: str = "",
        message: str = "",
        detail: Optional[Dict[str, Any]] = None,
        name: str = "",
        label: str = "",
    ) -> None:
        self.ok = ok
        self.code = code
        self.message = message
        self.detail = detail or {}
        #: 这一步的 id (ready / detect / above / grasp / safe ...)
        self.name = name
        #: 这一步的说法 (播报时用, 如「移到上方点」)
        self.label = label

    @classmethod
    def success(
        cls, message: str = "", name: str = "", label: str = "", **detail: Any
    ) -> "StepResult":
        return cls(True, "ok", message, detail, name, label)

    @classmethod
    def failure(
        cls, code: str, message: str, name: str = "", label: str = "",
        **detail: Any,
    ) -> "StepResult":
        return cls(False, code, message, detail, name, label)

    def to_trace(self) -> Dict[str, Any]:
        """转成追溯记录里的一行。"""
        out = {"ok": self.ok, "code": self.code}
        if self.name:
            out["name"] = self.name
        out.update(self.detail)
        if self.message:
            out["message"] = self.message
        return out


class FlowContext:
    """流程执行时的共享上下文。

    片段之间要传递东西 —— 比如 ``arm`` 片段要知道目标位姿 (由
    ``detect`` 片段产出), 具名点表要跨片段共享。

    Attributes:
        side:        用哪条臂 ("left" / "right")
        target:      要抓的物体名
        target_pos:  识别出的物体位姿 (世界系) —— detect 片段填
        arm_name:    控制器里的真实臂名 —— detect 片段填
        approach_distance: 上方点的高度 (米)
        duration:    统一覆盖所有移动片段的时长 (None = 各步自己定)
        defaults:    服务端给的兜底值 (close_action / open_action 等)
        named_points: 具名点表 —— 点名 -> (x, y, z)
        steps:       每一步的结果 (按顺序)
    """

    __slots__ = (
        "side", "target", "target_pos", "arm_name", "approach_distance",
        "duration", "defaults", "steps", "flow_name", "named_points",
        "orientation",
    )

    def __init__(self, side: str, target: str) -> None:
        self.side = side
        self.target = target
        self.target_pos: Optional[Tuple[float, float, float]] = None
        self.arm_name: str = ""
        self.approach_distance: float = 0.10
        self.duration: Optional[float] = None
        #: 服务端 ``/v1/flows/{name}`` 返回的 defaults (夹爪动作名等)
        self.defaults: Dict[str, Any] = {}
        self.steps: List[StepResult] = []
        #: 流程名 —— 序列模式下要传给机械臂服务
        self.flow_name: str = "grasp"
        #: 具名点表 —— 点名 -> (x, y, z)
        #:
        #: ``target`` 片段往这里登记, ``arm`` 片段按名字查。
        #: **按流程顺序登记** —— 基于另一个点的点要先有基准点。
        self.named_points: Dict[str, Tuple[float, float, float]] = {}
        #: 拍照姿态 (各臂 a/b/c) —— 来自 ``detect`` 片段的 ``orientation``。
        #:
        #: **拍照姿态 = 抓取姿态** —— 机械臂从拍照时的姿态直接抓, 中间
        #: 不换姿态 (换的话那个姿态在目标点可能根本不可达)。
        self.orientation: Dict[str, Dict[str, float]] = {}

    @property
    def disabled(self) -> bool:
        """总闸关着吗 (只算不动)。"""
        return any(s.code == "disabled" for s in self.steps)


class VisionGraspSkill(Skill):
    """物体抓取 Skill —— 通用流程执行器。"""

    def __init__(
        self,
        spec: SkillSpec,
        ctx: Optional[Any] = None,
        vision_client: Optional[Any] = None,
        arm_client: Optional[Any] = None,
        trace_path: Optional[Path] = None,
    ):
        super().__init__(spec, ctx)
        self.vision_client = vision_client
        self.arm_client = arm_client
        self.trace_path = Path(trace_path) if trace_path else GRASP_TRACE_PATH
        #: 这次用哪条臂 —— ``_step_detect`` 要靠它挑臂基座
        self._side = "right"
        #: 流程定义 (步骤列表), 由 ``_load_flow()`` 拉取后填
        #:
        #: **必须是 None 而不是预填的默认值** —— ``_load_flow()`` 用
        #: 「self._flow is not None」判断「已经拉过了」, 如果这里预填了,
        #: 那个判断永远成立, ``GET /v1/flows/{name}`` 就**永远不会被调用**
        #: —— configs/flows/ 里的流程定义全部被静默忽略。
        self._flow: Optional[Dict[str, Any]] = None
        #: 流程名 (默认 "grasp")
        self._flow_name: str = "grasp"

    @staticmethod
    def spec() -> SkillSpec:
        """返回该 skill 的规范。"""
        return SkillSpec(
            id="vision.grasp",
            name="物体抓取",
            description="按配置的流程抓取物体 (默认: 回准备点 -> 拍照识别 -> 移到上方点 -> 移到抓取点并闭合夹爪 -> 回安全点)。",
            category="vision",
            tags=["manipulation", "planning"],
            kind="action",
            speak=True,
            timeout_ms=90000,
            source="builtin",
            # Phase 1 新增：动态路由信息
            branch="tool",
            natural_language_examples=[
                "抓杯子",
                "帮我拿杯子",
                "拿瓶子",
                "物体位置在哪",
                "怎么抓起来"
            ],
            # Phase 1 新增：完整 Skill 定义
            definition={
                "boundary": "按流程定义抓取物体；流程在 configs/flows/*.yaml；移动必经 arm_service 的安全门阀；依赖手眼标定精度",
                "detector_class": "VisionGraspDetector",
                "post_processors": ["pose_formatter", "approach_calculator"],
                "documentation": """
# vision.grasp - 物体抓取 (通用流程执行器)

## 执行模式
**默认「序列模式」**: 先做图像识别, 识别完之后把 flow 里的**关节片段**
和**夹爪片段**合成**一条动作序列**, 一次入控连续执行。

```
识别 ──► 流程解析 ──► 本地逆解 ──► 合成一条时间轴 ──► 一次入控 ──► 播完
              │                      │
              │                      └─ 移动步 (上方点/抓取点) 解成关节角
              └─ 关节片段 (准备点/安全点) 直接用动作库的关节角
                                    └─ 夹爪片段 (闭手) = 时间点, 到点下发
```

**为什么夹爪片段不进时间轴**: 实时指令包里**没有手部字段** (控制器文档
§21.9: 字段只有 robot / controlSpace / commandMode / poseType / value)。
多塞一个字段会被整包判 invalidDatagram, 那时候连手臂都不动。

灵巧手走控制器的 `rs485/*` 透传 (Modbus RTU), 一次写+读是两次控制面
往返, 远慢于臂控循环 —— 所以它由**独立线程**下发, 臂控线程只管入队。

传 `sequence=false` 可以退回**逐步骤模式** (每步单独下发)。

## 流程
流程**不是写死的** —— 它定义在机械臂服务的 `configs/flows/*.yaml` 里。
默认流程 (`grasp`) 是:
```
1. 回准备点      播动作库的「准备点」, 保证拍照视角一致
2. 拍照识别      视觉服务 fresh=True (强制重新拍, 不走缓存)
3. 移到上方点    目标正上方 approach_distance 米
4. 移到抓取点    目标 + 配置的偏移
5. 闭合夹爪      播动作库的「闭合夹爪」
6. 回安全点      播动作库的「安全点」
```

改流程只要改 `configs/flows/*.yaml`, **不用改代码**。
新增/删除步骤、调顺序、换动作名、改装配量 —— 都是改 yaml。

各流程文件的格式见 `configs/flows/README.md`。

## 按物品分流
不同物品可以走不同的流程 —— 分流表是 `configs/flows/routing.yaml`。
「抓瓶子」走 `grasp_bottle.yaml`, 没配过的走默认的 `grasp.yaml`。

选流程的优先级 (高的覆盖低的):
1. `flow` 参数 (显式指定)
2. `routing.yaml` 的物品名映射
3. `routing.yaml` 的 `default_flow` (全局兜底)

**分流在拍照之前就决定** —— 拿物品名查表, 不用等拍完照才知道抓什么。

## 职责边界
- ✓ 识别指定物体 (世界系)
- ✓ 多个同类物体时取离机器人最近的一个
- ✓ 按流程定义顺序执行, 每一步失败都有安全收尾
- ✗ 不处理碰撞检测

## 输入参数
- target (必需): 要抓取的物体名, 如「杯子」
- arm (可选): left / right, 默认 right
- approach_distance (可选): 上方点在物体上方的距离, 覆盖流程里的值
- duration (可选): 统一覆盖所有移动步骤的时长
- flow (可选): 用哪条流程, 默认 "grasp"
- sequence (可选): 是否用序列模式 (先识别再合成序列), 默认 true

## 动不动由谁决定
**由机械臂服务决定, 不是这个 skill**。configs/arm.yaml 的 `arm.enabled`
是「控制指令总闸」: 设为 false 时, arm_service 不向控制器下发任何控制
指令, 只把算好的位置报出来 (move_to 返回 code="disabled")。

## 输出格式
```json
{
    "found": true,
    "target_pose": {"x": 0.5, "y": 0.2, "z": 0.8},
    "approach_pose": {"x": 0.5, "y": 0.2, "z": 0.9},
    "grasp_pose": {"x": 0.5, "y": 0.2, "z": 0.8},
    "confidence": 0.95,
    "sequence": {"duration": 12.5, "segments": 5, "hand_cues": []},
    "steps": [{"ok": true, "code": "ok", "name": "ready"}, {"ok": true, "code": "ok", "name": "detect"}]
}
```

## 失败收尾
- `detect` 失败 -> 直接返回, 不回安全点 (此时手还在准备点, 那是安全的)
- 中途失败 -> **先回安全点**再报失败, 否则手停在半空
- 最后一步失败 -> 前面的步骤已成功, 如实说「抓到了但收尾失败」
- 控制总闸关着 -> **不算失败**, 流程照走完, 只是不动

## 使用示例
- "抓杯子" -> 走默认流程
- "帮我拿水瓶" -> 走默认流程
"""
            },
            # 注: requires_state 用于多轮编排，此处不强制要求（可选）
            parameters={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "要抓取的物体名，如「杯子」「瓶子」",
                    },
                    "arm": {
                        "type": "string",
                        "description": "用哪条臂: left / right",
                        "enum": ["left", "right"],
                        "default": "right",
                    },
                    "approach_distance": {
                        "type": "number",
                        "description": "预抓取点在物体上方的距离（米），不给则用流程里配的值",
                    },
                    "duration": {
                        "type": "number",
                        "description": "运动时长（秒），不给则用各步自己配的时长",
                    },
                    "flow": {
                        "type": "string",
                        "description": (
                            "用哪条流程（configs/flows/ 里的流程名）。"
                            "不给就按物品名自动分流"
                            "（configs/flows/routing.yaml），"
                            "没配过的物品走默认流程 grasp。"
                        ),
                    },
                    "sequence": {
                        "type": "boolean",
                        "description": (
                            "是否用「先识别、再合成一条动作序列执行」的模式。"
                            "默认 true —— 识别完之后，flow 里的关节片段和夹爪片段"
                            "合成一条时间轴，一次入控连续播完。传 false 退回"
                            "逐步骤下发。"
                        ),
                        "default": True,
                    },
                },
                "required": ["target"],
            },
            returns={
                "type": "object",
                "properties": {
                    "found": {"type": "boolean", "description": "是否找到目标"},
                    "target_pose": {"type": "object", "description": "目标位置"},
                    "approach_pose": {"type": "object", "description": "上方点位置"},
                    "grasp_pose": {"type": "object", "description": "抓取点位置"},
                    "confidence": {"type": "number", "description": "识别置信度"},
                    "steps": {
                        "type": "array",
                        "description": "每一步的结果",
                    },
                },
            },
        )

    async def execute(self, args: Dict[str, Any]) -> SkillResult:
        """按流程定义跑一次抓取。

        **动不动由机械臂服务决定** —— 这个 skill 只管把位置算对,
        然后调 ``move_to``/``play``; 机械臂服务那边有个「控制指令总闸」
        (``configs/arm.yaml`` 的 ``arm.enabled``), 关着就只算位置、
        不下发任何控制指令。

        **总闸关着的时候流程全走完** (只是不动) —— 用户要看的正是
        每一步算出来的位置, 中途就停掉等于什么都看不到。
        """
        started = time.time()

        valid, error_msg = await self.validate_args(args)
        if not valid:
            return SkillResult(
                ok=False,
                error=error_msg,
                elapsed_ms=(time.time() - started) * 1000,
            )

        target = str(args.get("target", "")).strip()
        side = str(args.get("arm", "right") or "right").strip().lower()
        if side not in ("left", "right"):
            side = "right"

        if not target:
            return SkillResult(
                ok=False,
                error="目标物体名不能为空",
                elapsed_ms=(time.time() - started) * 1000,
            )

        #: 按物品名选流程 —— 「抓瓶子」-> grasp_bottle, 没配过的走默认流程
        flow_name, flow_reason = self._resolve_flow_name(target, args)
        logger.info(
            "[抓取] 流程分流: %s -> %s (%s)", target, flow_name, flow_reason
        )

        #: 这条流程真的存在吗 —— 不存在就用默认流程
        flow_name = await self._ensure_flow_exists(flow_name, flow_reason)

        #: 这次用哪条臂 —— detect 步骤挑基座要用
        self._side = side
        self._flow_name = flow_name
        flow = await self._load_flow(flow_name)

        #: 追溯记录: 每一步都往里填, 无论成功失败最后都落盘
        trace: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "target": target,
            "arm": side,
            "flow": flow_name,
            "flow_reason": flow_reason,
            "steps": [],
        }
        context = FlowContext(side=side, target=target)

        #: 流程里每一步的失败要不要收尾 (先回安全点)
        recovery = str(flow.get("recovery_action") or "")
        recovery_label = str(flow.get("recovery_label") or "回安全点")

        try:
            if not self.vision_client:
                return self._fail("视觉服务客户端未配置", trace, started)
            if not self.arm_client:
                return self._fail("机械臂服务客户端未配置", trace, started)

            # 上面点的高度: 参数优先, 其次流程里的, 最后兜底
            context.approach_distance = self._resolve_approach_distance(
                args, flow
            )
            trace["approach_distance"] = context.approach_distance
            duration = args.get("duration")
            context.duration = (
                float(duration) if duration not in (None, "") else None
            )
            #: 服务端给的兜底值 (夹爪动作名等) —— 步骤没写 action 时用
            context.defaults = dict(flow.get("defaults") or {})
            context.flow_name = flow_name

            # ---- 序列模式 ----
            #
            # 「先识别, 再把 flow 里的关节和夹爪片段组成一个动作序列执行」
            # —— 机械臂服务那边一次入控连续播完, 比逐步骤下发更连贯。
            #
            # 默认开着; 传 ``sequence=false`` 可以退回逐步骤执行 (老行为)。
            if args.get("sequence", True):
                seq_result = await self._run_as_sequence(
                    flow, context, trace, started
                )
                if seq_result is not None:
                    return seq_result
                logger.info(
                    "[抓取] 序列模式不可用, 退回逐步骤执行"
                )

            steps = flow.get("steps") or []
            for index, step in enumerate(steps):
                result = await self._run_step(step, context)
                context.steps.append(result)
                trace["steps"].append(result.to_trace())
                self._sync_trace(trace, context)

                if result.ok or result.code == "disabled":
                    continue

                # ---- 失败了 ----
                #
                # 「detect 失败」= 还没动过 (手在准备点, 那是安全的地方),
                # 不需要收尾。其它步骤失败 = 手可能在半空, 得收回去。
                is_last = index == len(steps) - 1
                if step.get("type") == "detect" or step.get("on_failure") == "abort":
                    # detect 失败: 直接用它的 code 当 outcome (not_found /
                    # no_valid_candidate), 这样事后看追溯一眼就知道是
                    # 「没找到」还是「测不准」—— 跟旧版一致。
                    trace["outcome"] = result.code
                    # 播报只说「为什么失败」, 坐标不念出来 (太吵) ——
                    # 位置细节在 payload / 追溯记录里, 排查时看那里。
                    msg = result.message
                    self._write_trace(trace, started)
                    return SkillResult(
                        ok=False, output=msg, error=msg, reply=msg, speak=True,
                        payload={"steps": [s.to_trace() for s in context.steps]},
                        elapsed_ms=(time.time() - started) * 1000,
                    )

                # 最后一步失败: 前面的步骤已成功 —— 如实说「抓到了但收尾失败」
                if is_last:
                    # 前面都成功了, 只是**最后这一步**没成。
                    #
                    # 「已抓到」只在**抓取那步真的成了**时才敢说 ——
                    # 最后一步可能是回安全点 (抓到了但收尾失败), 也可能
                    # 压根没抓到 (比如最后一步是个配错的片段类型)。
                    grasp_step = next(
                        (s for s in context.steps if s.name == "grasp"), None
                    )
                    grabbed = grasp_step is not None and grasp_step.ok
                    if grabbed:
                        reply = (
                            f"抓取{context.target or '物体'}已抓到, "
                            f"但收尾失败: {result.message}"
                        )
                    else:
                        reply = f"抓取{context.target or '物体'}失败: {result.message}"
                    trace["outcome"] = f"grasp_ok_safe_failed:{result.code}"
                    self._write_trace(trace, started)
                    return SkillResult(
                        ok=False, output=reply, error=reply, reply=reply,
                        speak=True,
                        payload={"steps": [s.to_trace() for s in context.steps]},
                        elapsed_ms=(time.time() - started) * 1000,
                    )

                # 中途失败 -> 要不要收尾 (先回安全点)?
                #
                # **只有可能停在半空才收尾** —— 也就是失败的那一段真的
                # 动了机械臂 (arm 段引用了具名点/写了关节角), 或者前面
                # 已经有片段动过。前面就失败 (比如准备点没播成) 的话手还
                # 在原处, 直接报失败即可。
                #
                # **注意**: ``type: arm`` 的片段不一定动 —— 它也可能只是
                # 播动作库里的一个动作 (回准备点)。那种失败手还在原地。
                moved = (
                    self._segment_moves(step)
                    or any(
                        s.detail.get("move") is not None for s in context.steps
                    )
                )
                trace["outcome"] = f"failed:{result.code}"
                # 同上: 播报只说失败原因, 坐标不念出来
                msg = result.message
                if recovery and moved:
                    recover = await self._play_step(
                        recovery, recovery_label, name="recovery"
                    )
                    context.steps.append(recover)
                    trace["steps"].append(recover.to_trace())
                self._write_trace(trace, started)
                return SkillResult(
                    ok=False, output=msg, error=msg, reply=msg, speak=True,
                    payload={"steps": [s.to_trace() for s in context.steps]},
                    elapsed_ms=(time.time() - started) * 1000,
                )

            # ================= 全走完了 =================
            reply = self._build_reply(context, flow)
            # 「机械臂没有动」已经在 _build_success_reply 里说过了, 别再追加
            trace["outcome"] = "planned_only" if context.disabled else "grasp_ok"

            payload: Dict[str, Any] = {
                "target": target,
                "frame": "world",
                "steps": [s.to_trace() for s in context.steps],
                "flow": flow_name,
            }
            if context.target_pos is not None:
                payload["target_pose"] = list(context.target_pos)
            payload.update(self._pose_payload(context))

            state_updates = self._build_state_updates(context)

            self._write_trace(trace, started)
            return SkillResult(
                ok=True, output=reply, reply=reply, speak=True,
                state_updates=state_updates, payload=payload,
                elapsed_ms=(time.time() - started) * 1000,
            )

        except Exception as e:
            logger.exception("物体抓取失败")
            trace["outcome"] = "exception"
            trace["exception"] = repr(e)
            return self._fail(self._get_error_reason(str(e)), trace, started)

    @staticmethod
    def _sync_trace(
        trace: Dict[str, Any], context: FlowContext
    ) -> None:
        """把上下文里的关键信息同步进追溯记录。

        **这些是顶层字段** —— 有分析工具在读它们 (比如
        ``tools/analyze_base_flip.py`` 读 ``steps[]`` 里的 ``move``,
        ``tools/verify_base_transform.py`` 读 ``target_base``)。
        """
        if context.arm_name:
            trace["robot_arm"] = context.arm_name
        if context.target_pos is not None:
            trace["target_pose"] = list(context.target_pos)
        for step in context.steps:
            if step.name == "detect" and step.ok:
                trace["candidates"] = step.detail.get("candidates") or []
                trace["skipped_invalid"] = step.detail.get("skipped", 0)
            pose = step.detail.get("pose")
            if not pose:
                continue
            if step.name == "above":
                trace["above_pose"] = list(pose)
            elif step.name == "grasp":
                trace["grasp_pose"] = list(pose)

    # ------------------------------------------------------------ 流程分流

    def _resolve_flow_name(
        self, target: str, args: Dict[str, Any]
    ) -> Tuple[str, str]:
        """按物品名选流程。

        优先级 (高 -> 低):

        1. **显式 ``flow`` 参数** —— 用户/LLM 明确指定了就用它
           (「用 grasp_cup 流程抓」这种说法必须被尊重)
        2. **routing 映射** —— 查 ``configs/flows/routing.yaml``,
           用 ``target`` (用户说的那个词) 当键
        3. **默认流程** —— 上面都没命中就用 ``default_flow``

        Args:
            target: 要抓的物品名 (LLM 传的 ``target`` 参数)
            args:   工具参数 (里面有 ``flow``)

        Returns:
            ``(流程名, 依据)`` —— 依据是给日志和追溯记录看的:
            ``"explicit"`` / ``"routing:<物品名>"`` / ``"default"``

        **分流在拍照之前就决定** —— 不用等拍完照才知道抓的是什么。
        """
        # 1) 显式指定
        explicit = str(args.get("flow", "") or "").strip()
        if explicit:
            return explicit, "explicit"

        # 2) 查映射
        mapping, default_flow = _load_routing()
        if mapping:
            key = (target or "").strip()
            flow = mapping.get(key)
            if flow is None:
                # 剥掉修饰词再试一次 (「那个瓶子」-> 「瓶子」)
                bare = _strip_modifiers(key)
                if bare != key:
                    flow = mapping.get(bare)
                    if flow:
                        key = bare
            if flow:
                return flow, f"routing:{key}"

        # 3) 默认流程
        return default_flow, "default"

    async def _ensure_flow_exists(
        self, flow_name: str, reason: str
    ) -> str:
        """确认这条流程真的存在, 不存在就回落到默认流程。

        **为什么要有这一步**: routing.yaml 是人手写的, 里面的流程名可能
        写错 (文件还没建就先把映射写上), 或者流程文件被删了。
        拿不存在的流程去跑 -> 机械臂服务直接报错, 用户听到的是
        「流程 xxx 不存在」这种没头没尾的话。

        宁可回落到默认流程 (那是条一定能跑的通用流程), 也不让用户
        卡在这儿。

        **显式指定的流程不回落到默认** —— 用户/LLM 明确要的那条流程
        没了, 就该如实报错, 而不是悄悄换成别的流程 (「我让你抓杯子,
        你怎么抓成别的了」比报错更糟)。
        """
        if reason == "explicit":
            # 显式指定的流程存不存在, 由 _load_flow 报错 —— 不悄悄替换
            return flow_name

        if not self.arm_client:
            return flow_name

        try:
            names = await self.arm_client.flow_names()
        except Exception as exc:
            # 拉不到清单 = 服务不可达 —— 后面 _load_flow 会用兜底流程
            logger.warning("[抓取] 拉流程清单失败, 暂不校验流程名: %s", exc)
            return flow_name

        if not names:
            # 服务返回空清单 (没配任何流程) —— 不做判断
            return flow_name

        if flow_name in names:
            return flow_name

        # 不存在 -> 回落到默认流程
        _, default_flow = _load_routing()
        if default_flow in names:
            logger.warning(
                "[抓取] 流程 %r 不存在 (依据 %s), 回落到默认流程 %r",
                flow_name, reason, default_flow,
            )
            return default_flow

        logger.warning(
            "[抓取] 流程 %r 不存在 (依据 %s), 且默认流程 %r 也不存在",
            flow_name, reason, default_flow,
        )
        return flow_name

    # ------------------------------------------------------------ 流程加载

    async def _load_flow(self, name: str) -> Dict[str, Any]:
        """拿流程定义。

        优先从机械臂服务拿 (``GET /v1/flows/{name}``) —— 那边才有权威的
        流程定义 (``configs/flows/*.yaml``)。拿不到就用兜底的最小流程,
        保证流程还能跑。

        **缓存一次**: 拉到的定义存进 ``self._flow``, 后续步骤直接复用。
        """
        if self._flow is not None and self._flow_name == name:
            return self._flow
        try:
            data = await self.arm_client.flow_definition(name)
            if data and data.get("steps"):
                self._flow = data
                self._flow_name = name
                logger.info(
                    "[抓取] 流程 %r 来自 arm_service: %s",
                    name, " -> ".join(
                        str(s.get("id")) for s in data.get("steps") or []
                    ),
                )
                return data
        except Exception as exc:
            logger.warning("[抓取] 拿流程 %r 定义失败, 用兜底: %s", name, exc)
        self._flow = {
            "name": name,
            "description": "兜底流程",
            "steps": _FALLBACK_STEPS,
            "recovery_action": "安全点",
            "recovery_label": "回安全点",
            # 夹爪动作名 —— 没有它 close/open 步骤不知道播什么
            "defaults": {"close_action": "闭合夹爪", "open_action": "张开夹爪"},
        }
        self._flow_name = name
        return self._flow

    @staticmethod
    def _resolve_approach_distance(
        args: Dict[str, Any], flow: Dict[str, Any]
    ) -> float:
        """上方点的高度 (米)。

        优先级: **调用参数** > 流程里配的 > 兜底 0.10。

        (之前 ``approach_distance`` 参数被读了却没用 —— 实际算上方点时
        用的是流程配置里的值, 用户传什么都被静默忽略。)
        """
        value = args.get("approach_distance")
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
        value = flow.get("approach_distance")
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
        return 0.10

    # ------------------------------------------------------------ 步骤执行

    async def _run_step(
        self, step: Dict[str, Any], context: FlowContext
    ) -> StepResult:
        """执行一步 —— 按 type 分派。

        **未知 type 直接报错**, 不静默跳过 —— 跳过的话流程会「少做
        一步」而没人知道。
        """
        step_id = str(step.get("id") or "")
        step_type = str(step.get("type") or "").strip().lower()
        handler = self._HANDLERS.get(step_type)
        if handler is None:
            return StepResult.failure(
                "unknown_step_type",
                f"流程里有未知的步骤类型 {step_type!r} (步骤 {step_id!r})",
                step_id,
            )
        try:
            return await handler(self, step, context)
        except Exception as exc:
            logger.exception("[抓取] 步骤 %s (%s) 异常", step_id, step_type)
            return StepResult.failure(
                "step_exception", f"{step.get('label') or step_id}失败: {exc}",
                step_id,
            )

    async def _step_arm(
        self, step: Dict[str, Any], context: FlowContext
    ) -> StepResult:
        """关节片段。

        终点有四种来源:

        - ``action`` —— 播动作库里的动作 (回准备点 / 回安全点)
        - ``pose``   —— 直接给关节角 (**不走逆解**)
        - ``point``  —— 引用具名点 -> 走本地逆解到那个点
        - ``base``   —— 基于另一个具名点再偏移

        **动不动由机械臂服务决定** (那边的控制指令总闸)。这里无条件
        请求移动 —— 如果总闸关着, 服务会只算位置、不下发指令, 返回
        code="disabled"。
        """
        step_id = str(step.get("id") or "")
        label = str(step.get("label") or "")

        action = str(step.get("action") or "").strip()
        if action:
            # 动作库里的关节片段
            return await self._play_step(action, label, name=step_id, label=label)

        # ---- 显式关节角 -> 不走逆解 ----
        pose = step.get("pose")
        if isinstance(pose, dict):
            side = str(pose.get("side") or context.side).strip().lower()
            joints = pose.get(side) or pose.get("right") or pose.get("left")
            if not joints:
                return StepResult.failure(
                    "no_joints", f"{label}失败: 片段里没给关节角。", step_id, label,
                )
            duration = self._step_duration(step, context)
            move = await self.arm_client.move_joints(
                side, list(joints), duration=duration,
            )
            return self._move_result(move, step_id, label, pos=None)

        # ---- 引用具名点 -> 走本地逆解 ----
        point = str(step.get("point") or "").strip()
        base = str(step.get("base") or "").strip()
        ref = point or base
        if not ref:
            return StepResult.failure(
                "no_target",
                f"{label}失败: 片段没给终点 (action/pose/point/base)。",
                step_id, label,
            )
        if ref not in context.named_points:
            return StepResult.failure(
                "no_target",
                f"{label}失败: 还没算出具名点 {ref!r} 的位置。", step_id, label,
            )

        offset = step.get("offset") or {}
        offset = {
            axis: float(offset.get(axis, 0.0) or 0.0)
            for axis in ("x", "y", "z")
        }
        bp = context.named_points[ref]
        pos = (
            bp[0] + offset["x"],
            bp[1] + offset["y"],
            bp[2] + offset["z"],
        )

        duration = self._step_duration(step, context)
        # 拍照姿态 = 抓取姿态 —— 传下去, 让机械臂转到那个姿态再抓。
        #
        # **不给 a/b/c 就等于「保持当前姿态」** —— 那正是历史上报
        # unreachable 的原因: 拍照姿态跟抓取姿态不一致, 中间要转一个
        # 根本到不了的姿态。
        abc = (context.orientation or {}).get(context.side) or {}
        move = await self.arm_client.move_to(
            context.side, pos[0], pos[1], pos[2], duration=duration,
            a=abc.get("a"), b=abc.get("b"), c=abc.get("c"),
        )
        return self._move_result(move, step_id, label, pos=list(pos))

    @staticmethod
    def _segment_moves(step: Dict[str, Any]) -> bool:
        """这个片段失败后, 机械臂会不会停在半空?

        - ``arm`` + ``action`` -> 只是播动作库里的动作, 失败的话手还在
          原处 (比如准备点没播成) —— **不用收尾**;
        - ``arm`` + ``pose``/``point``/``base`` -> 真的动了, 可能停在
          半空 —— **要收尾**;
        - 其它类型 -> 不动。
        """
        if str(step.get("type") or "").lower() != "arm":
            return False
        if str(step.get("action") or "").strip():
            return False
        return True

    @staticmethod
    def _step_duration(
        step: Dict[str, Any], context: FlowContext
    ) -> Optional[float]:
        """片段时长: 调用参数 > 片段自己配的 > 流程默认。"""
        if context.duration is not None:
            return context.duration
        value = step.get("duration")
        return float(value) if value not in (None, "") else None

    @staticmethod
    def _move_result(
        move: Dict[str, Any], step_id: str, label: str,
        pos: Optional[List[float]],
    ) -> StepResult:
        """把机械臂服务的移动结果包成 StepResult。"""
        if move.get("code") == "disabled":
            # 总闸关着 —— **不是失败**, 是「算好了但没动」。
            # code 保持 "disabled", 编排代码靠它区分「真失败」和「只算不动」。
            return StepResult(
                True, "disabled", f"{label}已计算",
                {"code_disabled": True, "pose": pos, "move": move},
                step_id, label,
            )
        if not move.get("ok"):
            return StepResult.failure(
                str(move.get("code") or "move_failed"),
                str(move.get("message") or f"移到{label}失败"),
                step_id, label, pose=pos, move=move,
            )
        return StepResult.success(
            f"已到{label}", step_id, label, pose=pos, move=move
        )

    async def _step_hand(
        self, step: Dict[str, Any], context: FlowContext
    ) -> StepResult:
        """手部片段。

        手位有三种来源:

        - ``action``  —— 播动作库里的动作 (闭合夹爪 / 张开夹爪)
        - ``pose``    —— 6 个 0~1000 的手位 (纯 pose 必须给 ``direction``)
        - ``preset``  —— 手势名 (如「一把抓」)

        ``direction`` 只在没给 action/preset 时用来选配置的默认手位。
        """
        step_id = str(step.get("id") or "")
        label = str(step.get("label") or "")
        action = str(step.get("action") or "").strip()
        preset = str(step.get("preset") or "").strip()
        direction = str(step.get("direction") or "").strip().lower()
        pose = step.get("pose")

        # ---- 动作库 ----
        if action:
            return await self._play_step(action, label, name=step_id, label=label)

        # ---- 显式手位 ----
        if isinstance(pose, (list, tuple)) and pose:
            positions = [int(v) for v in pose]
            result = await self.arm_client.hand_command(
                direction or "close", positions=positions,
            )
            if not result.get("ok"):
                return StepResult.failure(
                    str(result.get("code") or "hand_failed"),
                    str(result.get("message") or f"{label}失败"),
                    step_id, label, hand=result,
                )
            return StepResult.success(
                f"{label}完成", step_id, label, hand=result,
            )

        # ---- 手势名 ----
        if preset:
            result = await self.arm_client.hand_command(
                direction or "close", preset=preset,
            )
            if not result.get("ok"):
                return StepResult.failure(
                    str(result.get("code") or "hand_failed"),
                    str(result.get("message") or f"{label}失败"),
                    step_id, label, hand=result,
                )
            return StepResult.success(
                f"{label}完成", step_id, label, hand=result,
            )

        # ---- 只有方向 -> 用配置里的默认动作 ----
        if direction:
            action = str(context.defaults.get(f"{direction}_action") or "")
            if action:
                return await self._play_step(
                    action, label, name=step_id, label=label,
                )

        return StepResult.success(f"跳过{label}", step_id, label, hand=None)

    async def _step_detect(
        self, step: Dict[str, Any], context: FlowContext
    ) -> StepResult:
        """拍照识别。

        **fresh=True 是必须的** —— 视觉服务默认返回上一次检测的缓存,
        物体被挪动了它也不知道。不加这个参数的话, 连续抓同一个物体
        会拿到**一模一样的坐标和置信度** (连 conf 都一样, 那显然不是
        两次真实推理的结果)。
        """
        step_id = str(step.get("id") or "detect")
        label = str(step.get("label") or "拍照识别")
        target = context.target
        result = await self.vision_client.find(
            target, frame="world", fresh=True
        )
        logger.info("[抓取] 视觉查询 target=%r frame=world fresh=True 返回: %s",
                    target, result)
        objects = result.get("objects") or []
        if not result.get("found") or not objects:
            return StepResult.failure(
                "not_found", f"没有找到{target}，请调整物体或相机位置。",
                step_id, label,
            )

        # ---- 机器人位置: 臂基座在世界系下的位置 ----
        kin = await self.arm_client.kinematics(refresh=True)
        arm_name, arm_data = self._pick_arm(kin.get("arms") or {}, context.side)
        base = (arm_data or {}).get("base_frame")
        if not base:
            return StepResult.failure(
                "no_base", "拿不到机械臂基座位置，无法判断哪个物体最近。",
                step_id, label,
            )
        base_pos = (float(base["x"]), float(base["y"]), float(base["z"]))

        # ---- 候选: 深度可信 + 确实是世界系, 按到基座的距离排序 ----
        candidates: List[Dict[str, Any]] = []
        skipped = 0
        for obj in objects:
            pose = obj.get("pose") or {}
            if not pose.get("depth_valid", True) or pose.get("frame") != "world":
                skipped += 1
                continue
            pos = (float(pose["x"]), float(pose["y"]), float(pose["z"]))
            candidates.append(
                {
                    "position": pos,
                    "distance_to_base": self._dist(pos, base_pos),
                    "confidence": float(obj.get("confidence", 0)),
                    "label": obj.get("label_zh") or obj.get("label") or target,
                }
            )
        candidates.sort(key=lambda c: c["distance_to_base"])

        for i, c in enumerate(candidates):
            logger.info(
                "抓取候选 #%d %s pos=(%.4f, %.4f, %.4f) 距基座 %.3fm conf=%.2f",
                i, c["label"], *c["position"], c["distance_to_base"], c["confidence"],
            )

        if not candidates:
            reason = (
                f"看到了{target}，但测不准距离，不能抓。"
                if skipped else
                f"{target}的位置没能换算到世界坐标系，请检查手眼标定。"
            )
            return StepResult.failure("no_valid_candidate", reason, step_id, label)

        # ---- 记下识别结果, 后面的 move 步骤要用 ----
        chosen = candidates[0]
        context.target_pos = tuple(chosen["position"])
        context.arm_name = arm_name

        # ---- 记下拍照姿态 (后面抓取时用同一个姿态) ----
        orientation = step.get("orientation") or {}
        if isinstance(orientation, dict) and orientation:
            context.orientation = {
                str(side).strip().lower(): dict(abc)
                for side, abc in orientation.items()
                if isinstance(abc, dict)
            }
            logger.info("[抓取] 拍照姿态: %s", context.orientation)

        return StepResult.success(
            f"识别到{target}", step_id, label,
            objects=objects,
            candidates=candidates,
            arm_name=arm_name,
            base_pos=base_pos,
            skipped=skipped,
            confidence=chosen["confidence"],
            orientation=context.orientation,
        )

    async def _step_target(
        self, step: Dict[str, Any], context: FlowContext
    ) -> StepResult:
        """具名点声明 —— **纯声明, 不产生任何运动**。

        把点登记进 ``context.named_points``, 供后面的 ``arm`` 片段
        按名字引用 (``point: <点名>``)。

        点的值有三种来源:

        - ``source: detect`` -> 识别结果的位置
        - ``base: <点名>`` + ``offset`` -> 基准点 + 偏移
        - ``pose: {x,y,z}`` -> 写死的坐标

        ``points`` 列表写法一次可以登记多个点。
        """
        step_id = str(step.get("id") or "")
        label = str(step.get("label") or "")

        points_raw = step.get("points")
        if isinstance(points_raw, list) and points_raw:
            registered: List[str] = []
            for item in points_raw:
                if not isinstance(item, dict):
                    continue
                point_id = str(item.get("id") or "").strip()
                if not point_id:
                    continue
                value = self._resolve_point(item, context, point_id)
                if value is None:
                    return StepResult.failure(
                        "no_target",
                        f"{label}失败: 算不出具名点 {point_id!r} 的位置。",
                        step_id, label,
                    )
                context.named_points[point_id] = value
                registered.append(point_id)
            return StepResult.success(
                f"已登记 {len(registered)} 个点", step_id, label,
                points=registered,
            )

        # 单点写法: 步骤 id 就是点名
        point_id = step_id
        value = self._resolve_point(step, context, point_id)
        if value is None:
            return StepResult.failure(
                "no_target",
                f"{label}失败: 算不出具名点 {point_id!r} 的位置。",
                step_id, label,
            )
        context.named_points[point_id] = value
        return StepResult.success(
            f"已登记点 {point_id}", step_id, label,
            point=point_id, pose=list(value),
        )

    def _resolve_point(
        self, spec: Dict[str, Any], context: FlowContext, point_id: str
    ) -> Optional[Tuple[float, float, float]]:
        """算一个具名点的值。算不出来返回 None。"""
        source = str(spec.get("source") or "").strip().lower()
        base = str(spec.get("base") or "").strip()
        pose = spec.get("pose")
        distance = str(spec.get("distance") or "").strip().lower()
        offset = spec.get("offset") or {}
        offset = {
            axis: float(offset.get(axis, 0.0) or 0.0)
            for axis in ("x", "y", "z")
        }

        if source == "detect":
            if context.target_pos is None:
                return None
            anchor = context.target_pos
        elif base:
            if base not in context.named_points:
                return None
            anchor = context.named_points[base]
        elif isinstance(pose, dict):
            anchor = (
                float(pose.get("x", 0.0) or 0.0),
                float(pose.get("y", 0.0) or 0.0),
                float(pose.get("z", 0.0) or 0.0),
            )
        else:
            return None

        dz = offset["z"]
        # ``distance: approach`` —— 高度取流程级的 approach_distance
        # (这样「上方点的高度」是流程的一个设置, 调用参数可以覆盖它)
        if distance == "approach":
            dz += context.approach_distance

        return (
            anchor[0] + offset["x"],
            anchor[1] + offset["y"],
            anchor[2] + dz,
        )

    # ------------------------------------------------------------ 步骤注册表

    #: 片段类型 -> 处理函数
    #:
    #: **加新片段类型要改这里** —— 这是流程通用化的边界。
    #: 见 configs/flows/README.md 的「扩展边界」。
    _HANDLERS: Dict[str, Callable] = {
        "arm": _step_arm,
        "hand": _step_hand,
        "detect": _step_detect,
        "target": _step_target,
    }

    # ------------------------------------------------------------ 序列模式

    async def _run_as_sequence(
        self,
        flow: Dict[str, Any],
        context: FlowContext,
        trace: Dict[str, Any],
        started: float,
    ) -> Optional[SkillResult]:
        """**先识别, 再把剩下的流程合成一条序列执行**。

        这条路径对应用户的需求: 「先进行图像识别, 再将 flow 里的关节和
        夹爪片段组成一个动作序列执行」。

        与逐步骤执行 (``_run_step`` 循环) 的区别:

        - 逐步骤: 每一步单独下发 (每个动作各自入控, 中间有对齐空隙);
        - 序列模式: **识别完之后**, 把剩下的流程一次性合成**一条时间轴**
          (关节片段) + 若干**夹爪时间点**, 一次入控连续播完。

        机械臂服务那边做逆解 (本地 pinocchio), 所以本 skill 只负责
        「识别 -> 交位置过去」。

        Returns:
            走这条路径时返回 :class:`SkillResult`; 走不了这条路时返回
            None (调用方退回逐步骤执行)。
        """
        if not self.arm_client:
            return None
        # 服务端得支持这个接口 —— 旧版本机械臂服务没有
        if not hasattr(self.arm_client, "grasp_sequence"):
            return None

        steps = flow.get("steps") or []
        # 找到 detect 步骤 —— 识别必须先做
        detect_index = -1
        for index, step in enumerate(steps):
            if str(step.get("type") or "").lower() == "detect":
                detect_index = index
                break
        if detect_index < 0:
            return None

        # ---- 1. 先做识别 ----
        detect_result = await self._run_step(steps[detect_index], context)
        context.steps.append(detect_result)
        trace["steps"].append(detect_result.to_trace())
        self._sync_trace(trace, context)

        if not detect_result.ok and detect_result.code != "disabled":
            # 识别失败: 直接返回, 不回安全点 (此时手还在准备点, 那是安全的)
            trace["outcome"] = detect_result.code
            # 播报只说失败原因, 坐标不念出来 (位置细节在 payload/追溯里)
            msg = detect_result.message
            self._write_trace(trace, started)
            return SkillResult(
                ok=False, output=msg, error=msg, reply=msg, speak=True,
                payload={"steps": [s.to_trace() for s in context.steps]},
                elapsed_ms=(time.time() - started) * 1000,
            )

        if context.target_pos is None:
            return None

        # ---- 2. 识别完了, 剩下的流程合成一条序列 ----
        logger.info(
            "[抓取] 识别到 %s 位于 (%.4f, %.4f, %.4f), 合成动作序列",
            context.target, *context.target_pos,
        )
        try:
            result = await self.arm_client.grasp_sequence(
                context.target_pos[0],
                context.target_pos[1],
                context.target_pos[2],
                side=context.side,
                flow=context.flow_name,
                approach_distance=context.approach_distance,
                duration=context.duration,
            )
        except Exception as exc:
            logger.warning("[抓取] 合成动作序列失败: %s", exc)
            return None

        if not result.get("ok"):
            msg = str(result.get("message") or "合成动作序列失败")
            trace["outcome"] = f"sequence_failed:{result.get('code', '?')}"
            trace["sequence_error"] = result
            self._write_trace(trace, started)
            return SkillResult(
                ok=False, output=msg, error=msg, reply=msg, speak=True,
                payload={"steps": [s.to_trace() for s in context.steps]},
                elapsed_ms=(time.time() - started) * 1000,
            )

        # ---- 3. 记进追溯 ----
        sequence = result.get("sequence") or {}
        trace["sequence"] = sequence
        trace["ik"] = result.get("ik") or []
        trace["outcome"] = "grasp_ok"

        # 把序列里的移动点也记成步骤 —— 保留旧字段名, 方便旧分析工具读
        for step_info in sequence.get("steps") or []:
            stype = str(step_info.get("type") or "")
            if stype != "arm":
                continue
            name = str(step_info.get("step") or "")
            context.steps.append(
                StepResult.success(
                    f"已到{step_info.get('label') or name}", name,
                    str(step_info.get("label") or ""),
                    pose=step_info.get("pose"),
                    # 旧字段名 —— 分析工具 (analyze_base_flip.py) 靠它读
                    move=step_info.get("pose"),
                )
            )
            trace["steps"].append(context.steps[-1].to_trace())

        reply = self._build_sequence_reply(context, sequence)
        payload: Dict[str, Any] = {
            "target": context.target,
            "frame": "world",
            "steps": [s.to_trace() for s in context.steps],
            "flow": context.flow_name,
            "sequence": sequence,
        }
        if context.target_pos is not None:
            payload["target_pose"] = list(context.target_pos)
        payload.update(self._pose_payload(context))

        self._write_trace(trace, started)
        return SkillResult(
            ok=True, output=reply, reply=reply, speak=True,
            state_updates=self._build_state_updates(context),
            payload=payload,
            elapsed_ms=(time.time() - started) * 1000,
        )

    def _build_sequence_reply(
        self, context: FlowContext, sequence: Dict[str, Any]
    ) -> str:
        """把序列执行结果拼成一句能念的话。

        **只报结论, 不报过程** —— 播的是「抓取杯子完成」, 而不是
        「目标点 X 0.55, Y -0.09, Z -0.28。已到回准备点。已拍照识别。
        已到移到上方点。已到移到抓取点。手已合拢。已到回安全点」。

        那些逐步的坐标和动作播出来又长又吵, 用户要的是**做完了没**。
        详细过程仍然进 ``payload`` / 追溯记录 —— 排查问题时看那里。

        只有**失败**时才带细节 (哪一步失败、为什么), 因为那时候
        用户需要知道出什么问题。
        """
        return self._build_success_reply(context)

    def _build_success_reply(self, context: FlowContext) -> str:
        """成功时的一句话结论。"""
        target = context.target or "物体"
        if context.disabled:
            # 总闸关着 —— 只算不动, 不能说「抓取完成」
            return f"{target}的位置已算好, 机械臂没有动"
        return f"抓取{target}完成"

    def _build_reply(
        self, context: FlowContext, flow: Dict[str, Any]
    ) -> str:
        """把各步结果拼成一句能念的话。

        播报用 —— **只报结论** (「抓取杯子完成」), 逐步的坐标和动作
        不念出来 (太长太吵), 详细过程进 ``payload`` / 追溯记录。

        失败时例外: 那时要带上失败原因, 用户需要知道出什么问题。
        """
        if context.disabled:
            return self._build_success_reply(context)

        # 失败的那一步 —— 如实说明是哪一步出了问题
        failed = next(
            (s for s in context.steps if not s.ok and s.code != "disabled"),
            None,
        )
        if failed is not None:
            detail = failed.message or f"{failed.label or failed.name}失败"
            return f"抓取{context.target or '物体'}失败: {detail}"

        return self._build_success_reply(context)

    # ------------------------------------------------------------ 播报

    async def _play_step(
        self, action: str, what: str, name: str = "", label: str = ""
    ) -> StepResult:
        """播一个动作并等它播完。

        **总闸关着时不算失败** —— 服务会拒绝播放 (HTTP 409 「机械臂控制
        已关闭」), 这跟「动作播失败」是两回事: 前者是「只算不动」, 后者
        是「真出问题了」。总闸关着时流程要**继续往下走**, 让用户看到
        完整的位置序列。
        """
        try:
            result = await self.arm_client.play_and_wait(action)
        except Exception as exc:
            if self._is_disabled_error(exc):
                return StepResult(
                    True, "disabled", f"{what}（只算不动）",
                    {"code_disabled": True, "action": action}, name, label,
                )
            return StepResult.failure(
                "play_failed", f"{what}失败: {exc}", name, label, action=action
            )
        if not result.get("finished", True):
            return StepResult.failure(
                "play_failed",
                str(result.get("error") or f"{what}失败"),
                name, label, action=action,
            )
        return StepResult.success(f"已{what}", name, label, action=action)

    @staticmethod
    def _is_disabled_error(exc: Exception) -> bool:
        """判断这个异常是不是「控制指令总闸关着」。

        机械臂服务在总闸关着时抛 ``ControllerError("机械臂控制已关闭…")``,
        agent 客户端把它包成 ``ArmError``, HTTP 层是 409。
        """
        text = str(exc)
        return ("控制已关闭" in text) or ("没有动" in text and "关闭" in text)

    def _pose_payload(self, context: FlowContext) -> Dict[str, Any]:
        """把各步的位置整理进 payload。

        保留旧字段名 (``approach_pose`` / ``grasp_pose``) —— 有别的
        代码在读它们 (比如 demo_skill_workflow.py)。
        """
        out: Dict[str, Any] = {}
        for step in context.steps:
            pose = step.detail.get("pose")
            if not pose:
                continue
            if step.name == "above":
                out["approach_pose"] = list(pose)
            elif step.name == "grasp":
                out["grasp_pose"] = list(pose)
        return out

    def _build_state_updates(
        self, context: FlowContext
    ) -> Dict[str, Any]:
        """给会话状态里记的东西 (后续轮次可能用得上)。"""
        if context.target_pos is None:
            return {}
        payload = self._pose_payload(context)
        conf = 0.0
        for step in context.steps:
            if step.name == "detect" and step.ok:
                conf = float(step.detail.get("confidence") or 0.0)
                break
        return {
            "grasp_target": {
                "name": context.target,
                "frame": "world",
                "target_pose": list(context.target_pos),
                "approach_pose": payload.get("approach_pose"),
                "grasp_pose": payload.get("grasp_pose"),
                "confidence": conf,
                "timestamp_ms": int(time.time() * 1000),
            }
        }

    # ------------------------------------------------------------ 辅助

    def _fail(self, message: str, trace: Dict[str, Any], started: float) -> SkillResult:
        """失败收尾: 落盘追溯并返回要念出来的原因。"""
        trace.setdefault("outcome", "failed")
        trace["message"] = message
        self._write_trace(trace, started)
        return SkillResult(
            ok=False,
            output=message,
            error=message,
            reply=message,
            speak=True,
            elapsed_ms=(time.time() - started) * 1000,
        )

    def _write_trace(self, trace: Dict[str, Any], started: float) -> None:
        """追加一行 JSONL。写失败只记日志, 不影响抓取流程。"""
        trace["elapsed_ms"] = int((time.time() - started) * 1000)
        try:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            with self.trace_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(trace, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            logger.warning("写抓取追溯记录失败: %s", exc)

    @staticmethod
    def _dist(a: Tuple[float, ...], b: Tuple[float, ...]) -> float:
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

    @staticmethod
    def _pick_arm(
        arms: Dict[str, Dict[str, Any]], side: str
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """在运动学快照里按左右找臂 (臂名是控制器里的真实名)。"""
        for name, data in arms.items():
            low = name.lower()
            if side in low:
                return name, data
        for name, data in arms.items():
            low = name.lower()
            if (side == "left" and (low.endswith("l") or "_l" in low)) or (
                side == "right" and (low.endswith("r") or "_r" in low)
            ):
                return name, data
        return "", None

    @staticmethod
    def _get_error_reason(error_str: str) -> str:
        """将异常信息转换为用户友好的错误原因。"""
        error_lower = error_str.lower()

        # 相机相关错误
        if "相机" in error_lower or "camera" in error_lower:
            if "没连上" in error_lower:
                return "相机没有连接，无法执行抓取。"
            elif "采帧超时" in error_lower or "timeout" in error_lower:
                return "相机采帧超时，请检查相机状态。"
            elif "没拿到" in error_lower:
                return "无法获取相机图像，请检查相机权限。"
            else:
                return "相机出错，无法执行抓取规划。"

        # 深度相关错误
        if "深度" in error_lower or "depth" in error_lower:
            return "无法获取深度信息，无法计算目标位置。"

        # 检测器相关错误
        if "检测" in error_lower or "detect" in error_lower or "model" in error_lower:
            return "物体检测模型出错，无法识别目标。"

        # 网络相关错误
        if "连接" in error_lower or "connection" in error_lower or "refused" in error_lower:
            return "无法连接到视觉服务，请检查服务是否运行。"

        # 坐标系相关错误
        if "坐标" in error_lower or "frame" in error_lower or "transform" in error_lower:
            return "坐标变换失败，请检查系统标定。"

        # 权限相关错误
        if "权限" in error_lower or "permission" in error_lower:
            return "权限不足，无法访问相机设备。"

        # 默认错误信息
        return f"抓取规划失败: {error_str[:50]}"


__all__ = ["FlowContext", "StepResult", "VisionGraspSkill"]
