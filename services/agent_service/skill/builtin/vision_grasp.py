"""物体抓取 skill。

识别目标物体，计算位姿和预抓取位置。
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from skill.base import Skill, SkillError
from skill.result import SkillResult
from skill.spec import SkillSpec

logger = logging.getLogger("skill.vision_grasp")

#: 抓取追溯记录 (JSONL, 每次抓取一行)。logs/ 已被 .gitignore 忽略
GRASP_TRACE_PATH = Path(__file__).resolve().parents[2] / "logs" / "grasp_trace.jsonl"


class _FlowConfig:
    """抓取流程的 5 步配置。

    值来自 ``configs/arm.yaml`` 的 ``arm.grasp.flow``。skill 启动时从
    机械臂服务拉一次 (``GET /v1/flow``) —— 那边才有权威的配置。

    这里带一套**兜底默认值**, 拉不到配置也能跑。
    """

    __slots__ = (
        "ready_action", "safe_action", "above_distance",
        "grasp_offset_x", "grasp_offset_y", "grasp_offset_z",
        "close_action", "open_action",
        "above_duration", "grasp_duration",
    )

    def __init__(self, **kw: Any) -> None:
        self.ready_action: str = kw.get("ready_action", "准备点")
        self.safe_action: str = kw.get("safe_action", "安全点")
        self.above_distance: float = float(kw.get("above_distance", 0.10))
        self.grasp_offset_x: float = float(kw.get("grasp_offset_x", 0.0))
        self.grasp_offset_y: float = float(kw.get("grasp_offset_y", 0.0))
        self.grasp_offset_z: float = float(kw.get("grasp_offset_z", 0.0))
        self.close_action: str = kw.get("close_action", "闭合夹爪")
        self.open_action: str = kw.get("open_action", "张开夹爪")
        self.above_duration: float = float(kw.get("above_duration", 3.0))
        self.grasp_duration: float = float(kw.get("grasp_duration", 2.0))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "_FlowConfig":
        return cls(**{k: v for k, v in (data or {}).items() if k in cls.__slots__})


class StepResult:
    """抓取流程里**一步**的结果。

    5 步流程的每一步都返回这个 —— 编排代码只看 ``ok`` 和 ``code``,
    不关心具体细节, 细节都在 ``detail`` 里 (进追溯记录)。

    Attributes:
        ok:      这步成功了吗
        code:    机器可读的原因 ("ok" / "not_found" / "disabled" / ...)
        message: 能直接念给用户听的话
        detail:  进追溯记录的结构化细节
    """

    __slots__ = ("ok", "code", "message", "detail", "name")

    def __init__(
        self,
        ok: bool,
        code: str = "",
        message: str = "",
        detail: Optional[Dict[str, Any]] = None,
        name: str = "",
    ) -> None:
        self.ok = ok
        self.code = code
        self.message = message
        self.detail = detail or {}
        #: 这一步的名字 (ready / detect / above / grasp / safe)
        self.name = name

    @classmethod
    def success(cls, message: str = "", name: str = "", **detail: Any) -> "StepResult":
        return cls(True, "ok", message, detail, name)

    @classmethod
    def failure(
        cls, code: str, message: str, name: str = "", **detail: Any
    ) -> "StepResult":
        return cls(False, code, message, detail, name)

    def to_trace(self) -> Dict[str, Any]:
        """转成追溯记录里的一行。"""
        out = {"ok": self.ok, "code": self.code}
        if self.name:
            out["name"] = self.name
        out.update(self.detail)
        if self.message:
            out["message"] = self.message
        return out


class VisionGraspSkill(Skill):
    """物体抓取 Skill。"""

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
        #: 抓取流程配置 (5 步), 由 ``_flow_config()`` 拉取后填
        #:
        #: **必须是 None 而不是 _FlowConfig()** —— ``_flow_config()`` 用
        #: 「self._flow is not None」判断「已经拉过了」, 如果这里预填了
        #: 默认值, 那个判断永远成立, ``GET /v1/flow`` 就**永远不会被调用**
        #: —— configs/arm.yaml 里的 above_distance / grasp_offset /
        #: ready_action 全部被静默忽略, 机械臂一直用代码里写死的默认值。
        self._flow: Optional[_FlowConfig] = None

    @staticmethod
    def spec() -> SkillSpec:
        """返回该 skill 的规范。"""
        return SkillSpec(
            id="vision.grasp",
            name="物体抓取",
            description="按 5 步抓取: 回准备点 -> 拍照识别 -> 移到上方点 -> 移到抓取点并闭合夹爪 -> 回安全点。",
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
                "boundary": "5 步抓取流程 (准备点→拍照→上方点→抓取点→安全点)；移动必经 arm_service 的安全门阀；依赖手眼标定精度",
                "detector_class": "VisionGraspDetector",
                "post_processors": ["pose_formatter", "approach_calculator"],
                "documentation": """
# vision.grasp - 物体抓取 (5 步流程)

## 流程
```
1. 回准备点      播动作库的「准备点」, 保证拍照视角一致
2. 拍照识别      视觉服务 fresh=True (强制重新拍, 不走缓存)
3. 移到上方点    目标正上方 above_distance 米
4. 移到抓取点    目标 + 配置的偏移, 然后闭合夹爪
5. 回安全点      播动作库的「安全点」
```

各步的动作名/距离/时长都在 `configs/arm.yaml` 的 `arm.grasp.flow` 里。

## 职责边界
- ✓ 识别指定物体 (世界系)
- ✓ 多个同类物体时取离机器人最近的一个
- ✓ 5 步顺序执行, 每一步失败都有安全收尾
- ✗ 不处理碰撞检测

## 输入参数
- target (必需): 要抓取的物体名，如「杯子」
- arm (可选): left / right，默认 right
- approach_distance (可选): 上方点在物体上方的距离，默认取配置的 0.1m
- duration (可选): 运动时长，不给则用配置默认值

## 动不动由谁决定
**由机械臂服务决定, 不是这个 skill**。configs/arm.yaml 的 `arm.enabled`
是「控制指令总闸」: 设为 false 时, arm_service 不向控制器下发任何控制
指令, 只把算好的位置报出来 (move_to 返回 code="disabled")。

## 输出格式
```json
{
    "found": true,
    "target_pose": {"x": 0.5, "y": 0.2, "z": 0.8, "qx": 0, "qy": 0, "qz": 0, "qw": 1},
    "approach_pose": {"x": 0.5, "y": 0.2, "z": 0.9},
    "grasp_pose": {"x": 0.5, "y": 0.2, "z": 0.8},
    "confidence": 0.95,
    "steps": [{"ok": true, "code": "ok"}, {"ok": true, "code": "ok"}]
}
```

## 失败收尾
- 第 1 步失败 -> 直接返回, 不拍照
- 第 2 步失败 (没找到) -> 返回, 不回准备点 (已经在准备点了)
- 第 3/4 步失败 -> **先回安全点**再报失败, 否则手停在半空
- 第 5 步失败 -> 抓取已成功, 如实说「抓到了但回安全点失败」

## 使用示例
- "抓杯子" → 走完 5 步
- "帮我拿水瓶" → 走完 5 步
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
                        "description": "预抓取点在物体上方的距离（米），默认 0.1",
                        "default": 0.1,
                    },
                    "duration": {
                        "type": "number",
                        "description": "运动时长（秒），不给则用配置默认值（3 秒）",
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
                        "description": "5 步流程每一步的结果",
                    },
                },
            },
        )

    async def execute(self, args: Dict[str, Any]) -> SkillResult:
        """按 5 步跑完一次抓取: 回准备点 -> 拍照 -> 上方点 -> 抓取点 -> 安全点。

        **动不动由机械臂服务决定** —— 这个 skill 只管把位置算对,
        然后调 ``move_to``/``play``; 机械臂服务那边有个「控制指令总闸」
        (``configs/arm.yaml`` 的 ``arm.enabled``), 关着就只算位置、
        不下发任何控制指令。

        **总闸关着的时候 5 步全走完** (只是不动) —— 用户要看的正是
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
        approach_distance = float(args.get("approach_distance", 0.1))
        duration = args.get("duration")
        duration = float(duration) if duration not in (None, "") else None

        if not target:
            return SkillResult(
                ok=False,
                error="目标物体名不能为空",
                elapsed_ms=(time.time() - started) * 1000,
            )

        #: 这次用哪条臂 —— 第 2 步挑基座要用
        self._side = side
        flow = await self._flow_config()
        self._flow = flow

        # 追溯记录: 每一步都往里填, 无论成功失败最后都落盘
        trace: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "target": target,
            "arm": side,
            "approach_distance": approach_distance,
            "steps": [],
        }
        #: 每一步的结果, 按顺序
        steps: List[StepResult] = []

        def record(step: StepResult) -> StepResult:
            steps.append(step)
            trace["steps"].append(step.to_trace())
            return step

        try:
            if not self.vision_client:
                return self._fail("视觉服务客户端未配置", trace, started)
            if not self.arm_client:
                return self._fail("机械臂服务客户端未配置", trace, started)

            # ================= 第 1 步: 回准备点 =================
            #
            # 拍照前把手臂摆到固定姿态 —— 这样每次拍的视角一致, 标定
            # 才对得上。动作名留空 = 跳过这一步。
            ready = await self._step_ready(flow)
            record(ready)
            if not ready.ok and ready.code != "disabled":
                trace["outcome"] = f"failed:{ready.code}"
                return self._fail(ready.message, trace, started)

            # ================= 第 2 步: 拍照识别 =================
            detect = await self._step_detect(target)
            record(detect)
            if not detect.ok:
                # outcome 用 detect 的 code 本身 (not_found / no_valid_candidate),
                # 这样事后看追溯一眼就知道是「没找到」还是「测不准」
                trace["outcome"] = detect.code
                # 没找到 -> 就在准备点待着, 不用回安全点 (准备点本身就是安全的)
                return self._fail(detect.message, trace, started)

            objects = detect.detail.get("objects") or []
            candidates = detect.detail.get("candidates") or []
            chosen = candidates[0]
            target_pos = chosen["position"]
            arm_name = detect.detail.get("arm_name", "")

            # 候选也进追溯 —— 出问题时得知道「当时看到了几个、为什么选这个」
            trace["candidates"] = candidates
            trace["skipped_invalid"] = detect.detail.get("skipped", 0)

            # 上方点: 目标正上方 above_distance 米
            above_pos = self._compute_approach_point(
                target_pos, flow.above_distance, "world"
            )
            # 抓取点: 目标 + 配置的偏移
            grasp_pos = (
                target_pos[0] + flow.grasp_offset_x,
                target_pos[1] + flow.grasp_offset_y,
                target_pos[2] + flow.grasp_offset_z,
            )
            trace["robot_arm"] = arm_name
            trace["target_pose"] = target_pos
            trace["above_pose"] = above_pos
            trace["grasp_pose"] = grasp_pos

            state_updates = {
                "grasp_target": {
                    "name": target,
                    "frame": "world",
                    "target_pose": target_pos,
                    "approach_pose": above_pos,
                    "grasp_pose": grasp_pos,
                    "confidence": chosen["confidence"],
                    "timestamp_ms": int(time.time() * 1000),
                }
            }
            payload: Dict[str, Any] = {
                "target": target,
                "target_pose": target_pos,
                "approach_pose": above_pos,
                "grasp_pose": grasp_pos,
                "frame": "world",
                "confidence": chosen["confidence"],
                "candidates": len(candidates),
                "steps": [s.to_trace() for s in steps],
            }

            # ================= 第 3 步: 移到上方点 =================
            # ``duration`` 显式给了就用它 (用户可能想要更慢/更快)
            above_dur = duration if duration is not None else flow.above_duration
            above = await self._step_move(
                side, above_pos, above_dur, "上方点", name="above"
            )
            record(above)
            if not above.ok and above.code != "disabled":
                # 手停在半空 -> **必须先回安全点**, 再报失败
                trace["outcome"] = f"failed:{above.code}"
                safe = await self._step_safe(flow)
                record(safe)
                payload["steps"] = [s.to_trace() for s in steps]
                msg = f"{above.message}。{self._describe_pose(target_pos, '目标点')}"
                self._write_trace(trace, started)
                return SkillResult(
                    ok=False, output=msg, error=msg, reply=msg, speak=True,
                    state_updates=state_updates, payload=payload,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            # ================= 第 4 步: 移到抓取点 + 闭夹爪 =================
            grasp = await self._step_move(
                side, grasp_pos, flow.grasp_duration, "抓取点", name="grasp"
            )
            if grasp.ok:
                # 夹爪闭合 —— **第 4 步的后半**, 记在同一个 step 里
                # (5 步流程里夹爪不是单独一步)
                close = await self._step_close(flow)
                grasp.detail["close"] = close.to_trace()
                grasp.detail["close_ok"] = close.ok
                grasp.detail["close_action"] = close.detail.get("action", "")
                close = close  # 播报要用
            else:
                close = StepResult.failure(grasp.code, grasp.message, name="close")
            record(grasp)

            if not grasp.ok and grasp.code != "disabled":
                trace["outcome"] = f"failed:{grasp.code}"
                safe = await self._step_safe(flow)
                record(safe)
                payload["steps"] = [s.to_trace() for s in steps]
                msg = f"{grasp.message}。{self._describe_pose(target_pos, '目标点')}"
                self._write_trace(trace, started)
                return SkillResult(
                    ok=False, output=msg, error=msg, reply=msg, speak=True,
                    state_updates=state_updates, payload=payload,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            # ================= 第 5 步: 回安全点 =================
            safe = await self._step_safe(flow)
            record(safe)
            payload["steps"] = [s.to_trace() for s in steps]

            # ---- 组播报 ----
            only_planned = above.code == "disabled" or grasp.code == "disabled"
            parts: List[str] = []
            if ready.detail.get("action"):
                parts.append(
                    f"准备点已就位" if only_planned else "已回准备点"
                )
            parts.append(
                f"识别到{target}，置信度 {chosen['confidence'] * 100:.0f}%"
            )
            # 目标点也播 —— 用户要看的正是「它算出来在哪」
            parts.append(self._describe_pose(target_pos, "目标点"))
            if above.detail.get("pose"):
                parts.append(
                    f"{self._describe_pose(above_pos, '上方点')}"
                    + ("，已到达" if not only_planned else "")
                )
            if grasp.detail.get("pose"):
                parts.append(
                    f"{self._describe_pose(grasp_pos, '抓取点')}"
                    + ("，已到达" if not only_planned else "")
                )
            if grasp.detail.get("close_action"):
                parts.append(
                    "夹爪已闭合" if not only_planned else "夹爪将闭合"
                )
            if safe.detail.get("action"):
                parts.append("已回安全点" if not only_planned else "将回安全点")

            if only_planned:
                parts.append("机械臂没有动")
                trace["outcome"] = "planned_only"
                reply = "。".join(parts) + "。"
                self._write_trace(trace, started)
                return SkillResult(
                    ok=True, output=reply, reply=reply, speak=True,
                    state_updates=state_updates, payload=payload,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            # 第 5 步失败: 抓取已经成功了, 但没回到安全点 —— 如实说清楚
            if not safe.ok:
                trace["outcome"] = f"grasp_ok_safe_failed:{safe.code}"
                reply = (
                    "。".join(parts[:-1]) + f"。{safe.message}"
                )
                self._write_trace(trace, started)
                return SkillResult(
                    ok=False, output=reply, error=reply, reply=reply, speak=True,
                    state_updates=state_updates, payload=payload,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            trace["outcome"] = "grasp_ok"
            reply = "。".join(parts) + "。"
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

    # ------------------------------------------------------------ 5 个步骤

    async def _flow_config(self) -> Any:
        """拿抓取流程配置。

        优先从机械臂服务拿 (``GET /v1/flow``) —— 那边才有权威的配置
        (``configs/arm.yaml`` 的 ``arm.grasp.flow``)。拿不到就用本地
        兜底, 保证流程还能跑。

        **缓存一次**: 拉到的配置存进 ``self._flow``, 后续步骤直接复用。
        """
        if self._flow is not None:
            return self._flow
        try:
            data = await self.arm_client.flow_config()
            if data:
                self._flow = _FlowConfig.from_dict(data)
                logger.info(
                    "[抓取] 流程配置来自 arm_service: ready=%s safe=%s "
                    "above=%s offset=(%s, %s, %s)",
                    self._flow.ready_action, self._flow.safe_action,
                    self._flow.above_distance, self._flow.grasp_offset_x,
                    self._flow.grasp_offset_y, self._flow.grasp_offset_z,
                )
                return self._flow
        except Exception as exc:
            logger.warning("[抓取] 拿流程配置失败, 用默认值: %s", exc)
        self._flow = _FlowConfig()
        return self._flow

    async def _step_ready(self, flow: Any) -> StepResult:
        """第 1 步: 回准备点。"""
        action = flow.ready_action
        if not action:
            return StepResult.success("跳过准备点", name="ready", action="")
        return await self._play_step(action, "回准备点", name="ready")

    async def _play_step(self, action: str, what: str, name: str = "") -> StepResult:
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
                    {"code_disabled": True, "action": action}, name,
                )
            return StepResult.failure(
                "play_failed", f"{what}失败: {exc}", name, action=action
            )
        if not result.get("finished", True):
            return StepResult.failure(
                "play_failed",
                str(result.get("error") or f"{what}失败"),
                name, action=action,
            )
        return StepResult.success(f"已{what}", name, action=action)

    @staticmethod
    def _is_disabled_error(exc: Exception) -> bool:
        """判断这个异常是不是「控制指令总闸关着」。

        机械臂服务在总闸关着时抛 ``ControllerError("机械臂控制已关闭…")``,
        agent 客户端把它包成 ``ArmError``, HTTP 层是 409。
        """
        text = str(exc)
        return ("控制已关闭" in text) or ("没有动" in text and "关闭" in text)

    async def _step_detect(self, target: str) -> StepResult:
        """第 2 步: 拍照识别。

        **fresh=True 是必须的** —— 视觉服务默认返回上一次检测的缓存,
        物体被挪动了它也不知道。不加这个参数的话, 连续抓同一个物体
        会拿到**一模一样的坐标和置信度** (连 conf 都一样, 那显然不是
        两次真实推理的结果)。
        """
        result = await self.vision_client.find(
            target, frame="world", fresh=True
        )
        logger.info("[抓取] 视觉查询 target=%r frame=world fresh=True 返回: %s",
                    target, result)
        objects = result.get("objects") or []
        if not result.get("found") or not objects:
            return StepResult.failure(
                "not_found", f"没有找到{target}，请调整物体或相机位置。",
                name="detect",
            )

        # ---- 机器人位置: 臂基座在世界系下的位置 ----
        kin = await self.arm_client.kinematics(refresh=True)
        arm_name, arm_data = self._pick_arm(kin.get("arms") or {}, self._side)
        base = (arm_data or {}).get("base_frame")
        if not base:
            return StepResult.failure(
                "no_base", "拿不到机械臂基座位置，无法判断哪个物体最近。",
                name="detect",
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
            return StepResult.failure(
                "no_valid_candidate", reason, name="detect"
            )

        return StepResult.success(
            f"识别到{target}", name="detect",
            objects=objects,
            candidates=candidates,
            arm_name=arm_name,
            base_pos=base_pos,
            skipped=skipped,
        )

    async def _step_move(
        self,
        side: str,
        pos: Tuple[float, float, float],
        duration: float,
        label: str,
        name: str = "",
    ) -> StepResult:
        """第 3/4 步: 移到某个点。

        **动不动由机械臂服务决定** (那边的控制指令总闸)。这里无条件
        请求移动 —— 如果总闸关着, 服务会只算位置、不下发指令, 返回
        code="disabled"。
        """
        move = await self.arm_client.move_to(
            side, pos[0], pos[1], pos[2], duration=duration,
        )
        if move.get("code") == "disabled":
            # 总闸关着 —— **不是失败**, 是「算好了但没动」。
            # code 保持 "disabled", 编排代码靠它区分「真失败」和「只算不动」。
            return StepResult(
                True, "disabled", f"{label}已计算",
                {"code_disabled": True, "pose": list(pos), "move": move}, name,
            )
        if not move.get("ok"):
            return StepResult.failure(
                str(move.get("code") or "move_failed"),
                str(move.get("message") or f"移到{label}失败"),
                name, pose=list(pos), move=move,
            )
        return StepResult.success(
            f"已到{label}", name, pose=list(pos), move=move
        )

    async def _step_close(self, flow: Any) -> StepResult:
        """第 4 步的后半: 闭合夹爪。"""
        action = flow.close_action
        if not action:
            return StepResult.success("跳过夹爪", name="close", action="")
        return await self._play_step(action, "闭合夹爪", name="close")

    async def _step_safe(self, flow: Any) -> StepResult:
        """第 5 步: 回安全点。"""
        action = flow.safe_action
        if not action:
            return StepResult.success("跳过安全点", name="safe", action="")
        return await self._play_step(action, "回安全点", name="safe")

    # ------------------------------------------------------------ 辅助

    @staticmethod
    def _describe_pose(
        pos: Tuple[float, float, float], label: str
    ) -> str:
        """把一个位姿念成一句人话。

        播报用 —— 机械臂到底要去哪, 得让人听得出来。

        坐标是世界系 (米)。念成「X 零点五八, Y 零点零三, Z 负零点三六」
        这种比念数字好懂一点, 尤其 Z 常常是负的 (物体在基座下方)。

        Args:
            pos:   (x, y, z)
            label: 这句话的开头, 如「目标点」
        """
        return (
            f"{label} X {pos[0]:.2f}, Y {pos[1]:.2f}, Z {pos[2]:.2f}"
        )

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
    def _compute_approach_point(
        target_pos: Tuple[float, float, float],
        approach_distance: float,
        frame: str,
    ) -> Tuple[float, float, float]:
        """预抓取点: 在目标正上方 ``approach_distance`` 米。

        世界系 Z 向上, 所以加在 z 上。(相机系的 Z 是深度, 加距离是后退,
        数学上也是 z + d。)
        """
        x, y, z = target_pos
        return (x, y, z + approach_distance)

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


__all__ = ["VisionGraspSkill"]
