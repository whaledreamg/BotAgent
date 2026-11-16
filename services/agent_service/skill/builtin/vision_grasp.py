"""物体抓取 skill。

识别目标物体，计算位姿和预抓取位置。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

from skill.base import Skill, SkillError
from skill.result import SkillResult
from skill.spec import SkillSpec

logger = logging.getLogger("skill.vision_grasp")


class VisionGraspSkill(Skill):
    """物体抓取 Skill。"""

    def __init__(
        self,
        spec: SkillSpec,
        ctx: Optional[Any] = None,
        vision_client: Optional[Any] = None,
    ):
        super().__init__(spec, ctx)
        self.vision_client = vision_client

    @staticmethod
    def spec() -> SkillSpec:
        """返回该 skill 的规范。"""
        return SkillSpec(
            id="vision.grasp",
            name="物体抓取",
            description="识别指定物体并计算机械臂抓取的目标位姿和预抓取路点。",
            category="vision",
            tags=["manipulation", "planning"],
            kind="query",
            speak=True,
            timeout_ms=15000,
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
                "boundary": "仅计算抓取位姿，不执行实际的机械臂动作；依赖 vision.recognize 的准确性",
                "detector_class": "VisionGraspDetector",
                "post_processors": ["pose_formatter", "approach_calculator"],
                "documentation": """
# vision.grasp - 物体抓取规划

## 职责边界
- ✓ 识别指定物体
- ✓ 计算抓取位姿
- ✓ 计算预抓取路点
- ✓ 支持多种坐标系输出
- ✗ 不执行实际机械臂动作
- ✗ 不处理碰撞检测

## 输入参数
- target (必需): 要抓取的物体名，如「杯子」
- frame (可选): 输出坐标系，默认 arm_base
- approach_distance (可选): 预抓取距离，默认 0.1m

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
                    "frame": {
                        "type": "string",
                        "description": "输出坐标系: camera / world / arm_base",
                        "enum": ["camera", "world", "arm_base"],
                        "default": "arm_base",
                    },
                    "approach_distance": {
                        "type": "number",
                        "description": "预抓取距离（米），默认 0.1",
                        "default": 0.1,
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
        """执行物体抓取规划。"""
        started = time.time()

        # 参数校验
        valid, error_msg = await self.validate_args(args)
        if not valid:
            return SkillResult(
                ok=False,
                error=error_msg,
                elapsed_ms=(time.time() - started) * 1000,
            )

        target = str(args.get("target", "")).strip()
        frame = str(args.get("frame", "arm_base")).strip()
        approach_distance = float(args.get("approach_distance", 0.1))

        if not target:
            return SkillResult(
                ok=False,
                error="目标物体名不能为空",
                elapsed_ms=(time.time() - started) * 1000,
            )

        if frame not in ("camera", "world", "arm_base"):
            frame = "arm_base"

        try:
            # 调用视觉服务查找物体
            if not self.vision_client:
                error_msg = "视觉服务客户端未配置"
                return SkillResult(
                    ok=False,
                    error=error_msg,
                    reply=error_msg,
                    speak=True,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            result = await self.vision_client.find(target, frame=frame)

            if not result.get("found"):
                reply = f"没有找到 {target}，请移动或调整相机位置。"
                return SkillResult(
                    ok=False,
                    output=f"没有找到 {target}。",
                    reply=reply,
                    speak=True,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            # 获取识别结果
            objects = result.get("objects", [])
            if not objects:
                reply = "搜索返回结果为空，请重试。"
                return SkillResult(
                    ok=False,
                    output=f"没有找到 {target}。",
                    reply=reply,
                    speak=True,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            # 取第一个（通常是置信度最高的）
            obj = objects[0]
            pose = obj.get("pose", {})

            # 计算预抓取点
            target_pos = (float(pose.get("x", 0)), float(pose.get("y", 0)), float(pose.get("z", 0)))
            approach_pos = self._compute_approach_point(
                target_pos, approach_distance, frame
            )

            # 构造回复
            reply = (
                f"找到 {target}。\n"
                f"目标位置: ({target_pos[0]:.3f}, {target_pos[1]:.3f}, {target_pos[2]:.3f}) m\n"
                f"预抓取位置: ({approach_pos[0]:.3f}, {approach_pos[1]:.3f}, {approach_pos[2]:.3f}) m\n"
                f"坐标系: {frame}"
            )

            state_updates = {
                "grasp_target": {
                    "name": target,
                    "frame": frame,
                    "target_pose": target_pos,
                    "approach_pose": approach_pos,
                    "confidence": float(obj.get("confidence", 0)),
                    "timestamp_ms": int(time.time() * 1000),
                }
            }

            return SkillResult(
                ok=True,
                output=reply,
                reply=reply,
                speak=True,
                state_updates=state_updates,
                elapsed_ms=(time.time() - started) * 1000,
                payload={
                    "target": target,
                    "target_pose": target_pos,
                    "approach_pose": approach_pos,
                    "frame": frame,
                    "confidence": float(obj.get("confidence", 0)),
                },
            )

        except Exception as e:
            logger.exception("物体抓取规划失败")
            # 生成用户友好的错误提示
            error_reason = self._get_error_reason(str(e))
            return SkillResult(
                ok=False,
                error=error_reason,
                reply=error_reason,
                speak=True,
                elapsed_ms=(time.time() - started) * 1000,
            )

    @staticmethod
    def _compute_approach_point(
        target_pos: Tuple[float, float, float],
        approach_distance: float,
        frame: str,
    ) -> Tuple[float, float, float]:
        """
        计算预抓取点（沿 Z 轴方向后退）。

        在所有坐标系下，沿 Z 轴（深度轴）增加距离值即为后退。
        """
        x, y, z = target_pos
        # 沿 Z 轴增加距离（后退）
        z_approach = z + approach_distance
        return (x, y, z_approach)

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
