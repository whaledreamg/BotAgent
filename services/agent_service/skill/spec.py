"""Skill 规范定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class JsonSchema:
    """简单的 JSON Schema 包装。"""

    type: str = "object"
    properties: Dict[str, Any] = field(default_factory=dict)
    required: List[str] = field(default_factory=list)
    description: str = ""


@dataclass
class SkillSpec:
    """Skill 的完整定义。"""

    # 基础信息
    id: str                             # 唯一标识，如 "vision.recognize", "vision.grasp"
    name: str                           # 显示名，如 "图像识别"
    description: str                    # 给 LLM 看的详细说明
    version: str = "1.0.0"              # 版本

    # 分类
    category: str = "general"           # "vision", "manipulation", "device", "planning" ...
    tags: List[str] = field(default_factory=list)  # ["realtime", "learning", "safety", ...]

    # 接口定义
    parameters: Optional[Dict[str, Any]] = None    # JSON Schema（parameters 段）
    returns: Optional[Dict[str, Any]] = None       # 返回值 schema

    # 行为配置
    kind: str = "query"                 # "query" (输出型) / "action" (执行型)
    speak: bool = False                 # 结果是否要 TTS 播报
    timeout_ms: int = 30000             # 超时（毫秒）
    retryable: bool = False             # 失败是否可重试

    # 来源信息
    source: str = "builtin"             # "builtin" / "http" / "mcp" / "llm"
    source_url: Optional[str] = None    # 如 "http://127.0.0.1:8803" / "stdio:/path/to/mcp"

    # 运行时依赖
    requires: List[str] = field(default_factory=list)       # 依赖的其他 skill
    requires_state: List[str] = field(default_factory=list) # 依赖的上下文字段

    # 安全性与权限
    safety_level: str = "safe"          # "safe" / "caution" / "warning"
    requires_confirmation: bool = False

    # Phase 1 新增：路由和 Tool calling 支持
    branch: str = "tool"                # 分支归属："chat" / "tool" / "device"
    natural_language_examples: List[str] = field(default_factory=list)  # 自然语言示例，供 LLM 学习
    
    # Phase 1 新增：完整 Skill 定义（对应①②③）
    definition: Optional[Dict[str, Any]] = None     # 完整 Skill 定义，包含：
                                                     # - boundary: 职责边界
                                                     # - schema: 输入/输出 schema
                                                     # - detector_class: 底层检测器类名
                                                     # - post_processors: 后处理器列表
                                                     # - documentation: SKILL.md 文档

    # 内部使用
    _factory: Optional[Any] = field(default=None, init=False)  # Skill 工厂函数

    def to_openai_tool(self) -> Dict[str, Any]:
        """转成 OpenAI function calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.id,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }

    def to_tool_call_definition(self) -> Dict[str, Any]:
        """转成 tool calling 格式，包含完整的描述、参数和分支信息。
        
        用于 LLM 的 tool calling，包含：
        - 工具 ID 和名称
        - 完整描述
        - 参数定义
        - 自然语言示例（帮助 LLM 理解）
        """
        tool_def = {
            "type": "function",
            "function": {
                "name": self.id,
                "description": self.description,
                "parameters": self.parameters or {
                    "type": "object",
                    "properties": {},
                    "required": []
                },
            },
        }
        
        # 如果有自然语言示例，加入描述中
        if self.natural_language_examples:
            examples_text = "、".join(f'"{ex}"' for ex in self.natural_language_examples[:3])
            tool_def["function"]["description"] += f"\n\n用法示例: {examples_text}"
        
        return tool_def

    def get_documentation(self) -> str:
        """获取完整的 Skill 文档。
        
        用于调试和 Agent 学习，返回从 definition 中提取的 documentation。
        """
        if not self.definition:
            return f"# {self.name}\n\n{self.description}"
        
        return self.definition.get(
            "documentation",
            f"# {self.name}\n\n{self.description}"
        )

    def get_boundary(self) -> str:
        """获取 Skill 的职责边界说明。"""
        if not self.definition:
            return ""
        return self.definition.get("boundary", "")

    def to_dict(self) -> Dict[str, Any]:
        """转成字典。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "category": self.category,
            "tags": self.tags,
            "parameters": self.parameters,
            "returns": self.returns,
            "kind": self.kind,
            "speak": self.speak,
            "timeout_ms": self.timeout_ms,
            "retryable": self.retryable,
            "source": self.source,
            "source_url": self.source_url,
            "requires": self.requires,
            "requires_state": self.requires_state,
            "safety_level": self.safety_level,
            "requires_confirmation": self.requires_confirmation,
            # Phase 1 新增字段
            "branch": self.branch,
            "natural_language_examples": self.natural_language_examples,
            "definition": self.definition,
        }


__all__ = ["JsonSchema", "SkillSpec"]
