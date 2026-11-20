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

    @staticmethod
    def spec() -> SkillSpec:
        """返回该 skill 的规范。"""
        return SkillSpec(
            id="vision.grasp",
            name="物体抓取",
            description="识别指定物体, 选离机器人最近的一个, 经安全门阀后控制机械臂移动到它上方的预抓取点。",
            category="vision",
            tags=["manipulation", "planning"],
            kind="action",
            speak=True,
            timeout_ms=45000,
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
                "boundary": "识别并移动到预抓取点，不闭合夹爪；移动必经 arm_service 的安全门阀；依赖手眼标定精度",
                "detector_class": "VisionGraspDetector",
                "post_processors": ["pose_formatter", "approach_calculator"],
                "documentation": """
# vision.grasp - 物体抓取规划

## 职责边界
- ✓ 识别指定物体 (世界系)
- ✓ 多个同类物体时取离机器人最近的一个
- ✓ 套上示教的固定抓取姿态, 经安全门阀与臂展检查后移动到预抓取点
- ✗ 不闭合夹爪
- ✗ 不处理碰撞检测

## 输入参数
- target (必需): 要抓取的物体名，如「杯子」
- arm (可选): left / right，默认 right
- approach_distance (可选): 预抓取点在物体上方的距离，默认 0.1m
- duration (可选): 运动时长，默认取 configs/arm.yaml 的 grasp.duration (3 秒)
- execute (可选): false 时只规划不动，默认 true

## 输出格式
```json
{
    "found": true,
    "target_pose": {"x": 0.5, "y": 0.2, "z": 0.8, "qx": 0, "qy": 0, "qz": 0, "qw": 1},
    "approach_pose": {"x": 0.5, "y": 0.2, "z": 0.9},
    "confidence": 0.95
}
```

## 使用示例
- "抓杯子" → 计算杯子的抓取位姿
- "帮我拿水瓶" → 计算瓶子的位置和抓取方向
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
                    "execute": {
                        "type": "boolean",
                        "description": "false 时只规划不移动",
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
                    "approach_pose": {"type": "object", "description": "预抓取位置"},
                    "confidence": {"type": "number", "description": "识别置信度"},
                },
            },
        )

    async def execute(self, args: Dict[str, Any]) -> SkillResult:
        """识别 -> 取离机器人最近的 -> 经门阀移动到预抓取点 -> 落盘追溯。"""
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
        do_execute = args.get("execute", True) not in (False, "false", "False", 0)

        if not target:
            return SkillResult(
                ok=False,
                error="目标物体名不能为空",
                elapsed_ms=(time.time() - started) * 1000,
            )

        # 追溯记录: 每一步都往里填, 无论成功失败最后都落盘
        trace: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "target": target,
            "arm": side,
            "approach_distance": approach_distance,
            "execute": do_execute,
        }

        try:
            if not self.vision_client:
                return self._fail("视觉服务客户端未配置", trace, started)
            if not self.arm_client:
                return self._fail("机械臂服务客户端未配置", trace, started)

            # ---- 1. 视觉: 一律请求世界系 ----
            #
            # 离机器人多远、机械臂要去哪, 都在同一个世界系里算。
            result = await self.vision_client.find(target, frame="world")
            logger.info("[抓取] 视觉查询 target=%r frame=world 返回: %s", target, result)
            objects = result.get("objects") or []
            if not result.get("found") or not objects:
                trace["outcome"] = "not_found"
                return self._fail(
                    f"没有找到{target}，请调整物体或相机位置。", trace, started
                )

            # ---- 2. 机器人位置: 臂基座在世界系下的位置 ----
            kin = await self.arm_client.kinematics(refresh=True)
            arm_name, arm_data = self._pick_arm(kin.get("arms") or {}, side)
            base = (arm_data or {}).get("base_frame")
            if not base:
                return self._fail(
                    "拿不到机械臂基座位置，无法判断哪个物体最近。", trace, started
                )
            base_pos = (float(base["x"]), float(base["y"]), float(base["z"]))
            trace["robot_arm"] = arm_name
            trace["robot_base_world"] = base_pos

            # ---- 3. 候选: 深度可信 + 确实是世界系, 按到基座的距离排序 ----
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
            trace["candidates"] = candidates
            trace["skipped_invalid"] = skipped
            for i, c in enumerate(candidates):
                logger.info(
                    "抓取候选 #%d %s pos=(%.4f, %.4f, %.4f) 距基座 %.3fm conf=%.2f",
                    i, c["label"], *c["position"], c["distance_to_base"], c["confidence"],
                )

            if not candidates:
                trace["outcome"] = "no_valid_candidate"
                reason = (
                    f"看到了{target}，但测不准距离，不能抓。"
                    if skipped else
                    f"{target}的位置没能换算到世界坐标系，请检查手眼标定。"
                )
                return self._fail(reason, trace, started)

            chosen = candidates[0]
            target_pos = chosen["position"]
            approach_pos = self._compute_approach_point(
                target_pos, approach_distance, "world"
            )
            trace["chosen_index"] = 0
            trace["target_pose"] = target_pos
            trace["approach_pose"] = approach_pos
            logger.info(
                "抓取规划 target=%s arm=%s 共%d个候选, 选最近 距基座 %.3fm "
                "target_pose=(%.4f, %.4f, %.4f) approach_pose=(%.4f, %.4f, %.4f)",
                target, arm_name, len(candidates), chosen["distance_to_base"],
                *target_pos, *approach_pos,
            )

            state_updates = {
                "grasp_target": {
                    "name": target,
                    "frame": "world",
                    "target_pose": target_pos,
                    "approach_pose": approach_pos,
                    "confidence": chosen["confidence"],
                    "timestamp_ms": int(time.time() * 1000),
                }
            }
            payload = {
                "target": target,
                "target_pose": target_pos,
                "approach_pose": approach_pos,
                "frame": "world",
                "confidence": chosen["confidence"],
                "candidates": len(candidates),
            }

            # ---- 4. 只规划不动 ----
            if not do_execute:
                trace["outcome"] = "planned_only"
                reply = (
                    f"找到{target}，离机械臂 {chosen['distance_to_base']:.2f} 米。"
                    f"预抓取点 {approach_pos[0]:.3f}, {approach_pos[1]:.3f}, "
                    f"{approach_pos[2]:.3f}。"
                )
                self._write_trace(trace, started)
                return SkillResult(
                    ok=True, output=reply, reply=reply, speak=True,
                    state_updates=state_updates, payload=payload,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            # ---- 5. 移动: 先到物体上方的预抓取点, 不直接去物体表面 ----
            move = await self.arm_client.move_to(
                side, approach_pos[0], approach_pos[1], approach_pos[2],
                duration=duration,
            )
            trace["move"] = move
            trace["outcome"] = "moved" if move.get("ok") else f"rejected:{move.get('code')}"

            payload["move"] = {
                k: move.get(k) for k in ("ok", "code", "stage", "message", "error_m")
            }
            if not move.get("ok"):
                msg = str(move.get("message") or "机械臂没有移动")
                self._write_trace(trace, started)
                return SkillResult(
                    ok=False, output=msg, error=msg, reply=msg, speak=True,
                    state_updates=state_updates, payload=payload,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            reply = f"已移动到{target}上方，离目标{move.get('error_m', 0) * 100:.1f}厘米。"
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
