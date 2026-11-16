"""图像识别 skill。

从相机画面中识别物体，支持：
- 「图像中有什么」-> 按类别汇总
- 「有没有 XXX」-> 是/否判断
- 「有几个 XXX」-> 计数
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from skill.base import Skill, SkillError
from skill.result import SkillResult
from skill.spec import SkillSpec

logger = logging.getLogger("skill.vision_recognize")


class VisionRecognizeSkill(Skill):
    """图像识别 Skill。"""

    def __init__(self, spec: SkillSpec, ctx: Optional[Any] = None, vision_client: Optional[Any] = None):
        super().__init__(spec, ctx)
        self.vision_client = vision_client

    @staticmethod
    def spec() -> SkillSpec:
        """返回该 skill 的规范。"""
        return SkillSpec(
            id="vision.recognize",
            name="图像识别",
            description="分析相机画面中的物体。支持「有什么」、「有没有」、「有几个」等提问方式。",
            category="vision",
            tags=["realtime", "perception"],
            kind="query",
            speak=True,
            timeout_ms=15000,
            source="builtin",
            # Phase 1 新增：动态路由信息
            branch="tool",
            natural_language_examples=[
                "看到了什么",
                "你前面有什么",
                "有什么物体",
                "看看有没有杯子",
                "有几个瓶子",
                "有没有人",
                "识别一下"
            ],
            # Phase 1 新增：完整 Skill 定义
            definition={
                "boundary": "仅处理单帧图像识别，不处理视频流或持续追踪；无法识别人脸",
                "detector_class": "VisionRecognizeDetector",
                "post_processors": ["confidence_filter", "object_formatter"],
                "documentation": """
# vision.recognize - 图像识别

## 职责边界
- ✓ 单帧图像物体识别
- ✓ 返回物体类别和置信度
- ✓ 支持置信度阈值过滤
- ✗ 不支持视频流处理
- ✗ 不支持人脸识别
- ✗ 不支持持续追踪

## 输入参数
- question (必需): 用户的提问，如「有什么物体」「有没有杯子」
- conf (可选): 置信度阈值，默认 0.15，范围 0.0-1.0

## 输出格式
```json
{
    "summary": "识别到了 3 个物体",
    "objects": [
        {"label": "cup", "confidence": 0.95},
        {"label": "phone", "confidence": 0.87}
    ],
    "object_count": 3
}
```

## 使用示例
- "看到了什么" → 列举所有识别到的物体
- "有没有杯子" → 返回是否看到杯子
- "有几个瓶子" → 返回瓶子的数量
"""
            },
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "用户的问题，如「图像中有什么」「有没有杯子」「有几个瓶子」",
                    },
                    "conf": {
                        "type": "number",
                        "description": "置信度阈值，默认 0.15",
                        "default": 0.15,
                    },
                },
                "required": ["question"],
            },
            returns={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "识别结果摘要"},
                    "objects": {
                        "type": "array",
                        "description": "识别到的物体列表",
                    },
                    "object_count": {"type": "integer", "description": "物体总数"},
                },
            },
        )

    async def execute(self, args: Dict[str, Any]) -> SkillResult:
        """执行图像识别。"""
        started = time.time()

        # 参数校验
        valid, error_msg = await self.validate_args(args)
        if not valid:
            return SkillResult(
                ok=False,
                error=error_msg,
                elapsed_ms=(time.time() - started) * 1000,
            )

        question = str(args.get("question", "")).strip()
        conf = float(args.get("conf", 0.15))

        if not question:
            return SkillResult(
                ok=False,
                error="问题不能为空",
                elapsed_ms=(time.time() - started) * 1000,
            )

        try:
            # 调用视觉服务
            if not self.vision_client:
                error_msg = "视觉服务客户端未配置"
                return SkillResult(
                    ok=False,
                    error=error_msg,
                    reply=error_msg,
                    speak=True,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            result = await self.vision_client.detect(conf=conf)
            objects = result.get("objects", [])

            # 根据问题类型组织回复
            reply = self._format_reply(question, objects)

            # 汇总统计
            summary_by_class: Dict[str, int] = {}
            for obj in objects:
                label = obj.get("label", "未知")
                summary_by_class[label] = summary_by_class.get(label, 0) + 1

            # 构造输出
            output = reply
            state_updates = {
                "last_recognition": {
                    "question": question,
                    "object_count": len(objects),
                    "summary": summary_by_class,
                    "timestamp_ms": int(time.time() * 1000),
                }
            }

            return SkillResult(
                ok=True,
                output=output,
                reply=output,
                speak=True,
                state_updates=state_updates,
                perception_updates={"detected_objects": objects},
                elapsed_ms=(time.time() - started) * 1000,
                payload={
                    "object_count": len(objects),
                    "summary": summary_by_class,
                    "objects": objects,
                },
            )

        except Exception as e:
            logger.exception("图像识别失败")
            # 生成用户友好的错误提示
            error_reason = self._get_error_reason(str(e))
            return SkillResult(
                ok=False,
                error=error_reason,
                reply=error_reason,
                speak=True,
                elapsed_ms=(time.time() - started) * 1000,
            )

    def _format_reply(self, question: str, objects: list) -> str:
        """根据问题类型格式化回复。"""
        if not objects:
            return "没有识别到任何物体。"

        # 统计
        summary: Dict[str, int] = {}
        for obj in objects:
            label = obj.get("label", "未知")
            summary[label] = summary.get(label, 0) + 1

        # 「有什么」- 列表
        if any(kw in question for kw in ["有什么", "有哪些", "看到", "识别", "检测"]):
            items = [f"{count}个{name}" for name, count in summary.items()]
            return f"看到{len(objects)}个物体: {', '.join(items)}"

        # 「有没有」- 是/否
        if "有没有" in question or "有没" in question:
            # 提取查询的物体名
            for obj in objects:
                label = obj.get("label", "")
                if label:
                    return f"有，看到{summary.get(label, 0)}个{label}。"
            return "没有。"

        # 「有几个」- 计数
        if "有几个" in question or "多少个" in question:
            # 按总数返回
            return f"一共有{len(objects)}个物体。"

        # 默认 - 列表
        items = [f"{count}个{name}" for name, count in summary.items()]
        return f"识别到: {', '.join(items)}"

    def _get_error_reason(self, error_str: str) -> str:
        """将异常信息转换为用户友好的错误原因。"""
        error_lower = error_str.lower()
        
        # 相机相关错误
        if "相机" in error_lower or "camera" in error_lower:
            if "没连上" in error_lower:
                return "相机没有连接，请检查相机设备。"
            elif "采帧超时" in error_lower or "timeout" in error_lower:
                return "相机采帧超时，请检查相机是否正常工作。"
            elif "没拿到" in error_lower or "no color" in error_lower:
                return "无法获取相机图像，请检查相机权限。"
            else:
                return "相机出错，请检查相机连接和设置。"
        
        # 深度相关错误
        if "深度" in error_lower or "depth" in error_lower:
            return "深度信息采集失败，无法测量距离。"
        
        # 检测器相关错误
        if "检测" in error_lower or "detect" in error_lower or "model" in error_lower:
            return "物体检测模型加载失败，请检查模型文件。"
        
        # 网络相关错误
        if "连接" in error_lower or "connection" in error_lower or "refused" in error_lower:
            return "无法连接到视觉服务，请检查视觉服务是否运行。"
        
        # 权限相关错误
        if "权限" in error_lower or "permission" in error_lower or "denied" in error_lower:
            return "没有权限访问相机设备。"
        
        # 硬件相关错误
        if "硬件" in error_lower or "hardware" in error_lower:
            return "相机硬件故障，请检查设备。"
        
        # 默认错误信息
        return f"识别失败: {error_str[:50]}"


__all__ = ["VisionRecognizeSkill"]
