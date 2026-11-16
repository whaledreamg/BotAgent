"""通用 LLM 参数提取器

使用 LLM 从用户查询中提取参数，支持所有 Skill 类型。
这是与正则提取器平行的可靠路径。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from parameter_validator import ParameterValidator

logger = logging.getLogger("agent.llm_extractor")


class ParameterSpec:
    """参数规格定义"""

    def __init__(
        self,
        name: str,
        description: str,
        param_type: str = "string",
        required: bool = False,
        examples: Optional[List[str]] = None,
        enum_values: Optional[List[str]] = None,
        default: Optional[str] = None,
        notes: Optional[str] = None,
    ):
        self.name = name
        self.description = description
        self.param_type = param_type
        self.required = required
        self.examples = examples or []
        self.enum_values = enum_values
        self.default = default
        self.notes = notes

    def to_prompt_description(self) -> str:
        """生成用于 prompt 的参数描述"""
        parts = []

        # 基本描述
        required_text = "(必填)" if self.required else "(可选)"
        parts.append(f"- {self.name} {required_text}: {self.description}")

        # 类型信息
        if self.enum_values:
            parts.append(f"  类型: 必须是以下之一: {', '.join(self.enum_values)}")
        else:
            parts.append(f"  类型: {self.param_type}")

        # 例子
        if self.examples:
            parts.append(f"  例如: {', '.join(self.examples[:3])}")

        # 备注
        if self.notes:
            parts.append(f"  备注: {self.notes}")

        # 默认值
        if self.default:
            parts.append(f"  默认值: {self.default}")

        return "\n".join(parts)


class SkillSpec:
    """Skill 规格定义"""

    def __init__(
        self,
        name: str,
        label: str,
        description: str,
        category: str,
        parameters: Optional[List[ParameterSpec]] = None,
    ):
        self.name = name
        self.label = label
        self.description = description
        self.category = category
        self.parameters = parameters or []

    def get_required_params(self) -> List[ParameterSpec]:
        """获取必填参数"""
        return [p for p in self.parameters if p.required]

    def get_optional_params(self) -> List[ParameterSpec]:
        """获取可选参数"""
        return [p for p in self.parameters if not p.required]


class LLMParameterExtractor:
    """通用 LLM 参数提取器

    为任意 Skill 和参数规格从用户查询中提取参数。
    不依赖特定领域知识，只依赖 Skill 的参数定义。
    """

    def __init__(self, llm: LLM, validator: Optional[ParameterValidator] = None):
        self.llm = llm
        self.validator = validator

    async def extract(
        self, text: str, skill_spec: SkillSpec
    ) -> Tuple[Optional[Dict[str, Any]], float, Optional[str]]:
        """从文本中提取参数

        Args:
            text: 用户查询文本
            skill_spec: Skill 规格定义

        Returns:
            (提取的参数字典, 置信度 0.0-1.0, 推理过程或错误信息)
        """
        start_time = time.time()

        try:
            # 1. 构建 prompt
            prompt = self._build_prompt(text, skill_spec)
            logger.debug(f"[参数提取] Skill: {skill_spec.name}, 构建 prompt 成功")

            # 2. 调用 LLM
            # 将 prompt 转换为 messages 格式
            messages = [
                {"role": "user", "content": prompt}
            ]
            response = await self.llm.json_completion(
                messages,
                role="tool",  # 使用 tool 角色获取配置中的模型
                temperature=0.0,
                max_tokens=200,
            )

            # 3. 解析响应
            result = self._parse_response(response, skill_spec)

            # 4. 验证结果
            if self.validator:
                result["extracted_params"] = await self.validator.validate(
                    result.get("extracted_params", {}), skill_spec
                )

            # 5. 计算执行时间
            elapsed_ms = (time.time() - start_time) * 1000

            logger.info(
                f"[参数提取] Skill: {skill_spec.name}, "
                f"参数: {result.get('extracted_params')}, "
                f"置信度: {result.get('confidence', 0.0):.2f}, "
                f"耗时: {elapsed_ms:.1f}ms"
            )

            return (
                result.get("extracted_params"),
                result.get("confidence", 0.0),
                result.get("reasoning"),
            )

        except Exception as e:
            logger.error(
                f"[参数提取] Skill: {skill_spec.name}, 提取失败: {str(e)}"
            )
            return None, 0.0, f"提取失败: {str(e)}"

    def _build_prompt(self, text: str, skill_spec: SkillSpec) -> str:
        """构建 LLM 的 prompt"""

        # 构建参数说明
        if not skill_spec.parameters:
            param_description = "（该技能不需要参数）"
        else:
            required_params = skill_spec.get_required_params()
            optional_params = skill_spec.get_optional_params()

            parts = []

            if required_params:
                parts.append("必填参数:")
                for param in required_params:
                    parts.append(param.to_prompt_description())

            if optional_params:
                parts.append("\n可选参数:")
                for param in optional_params:
                    parts.append(param.to_prompt_description())

            param_description = "\n".join(parts)

        # 构建完整 prompt
        prompt = f"""你是一个参数提取专家。请从用户查询中提取出指定技能所需的参数。

技能名: {skill_spec.label}
技能说明: {skill_spec.description}

参数要求:
{param_description}

用户查询: "{text}"

请分析用户的查询，提取出所有能确定的参数。

返回格式: JSON 对象，包含:
- "extracted_params": 提取的参数字典 (参数名 -> 值)
- "confidence": 置信度，0.0-1.0 之间的浮点数，表示提取的准确度
- "reasoning": 简要的推理说明，解释为什么这样提取

例如:
{{
  "extracted_params": {{"city": "北京"}},
  "confidence": 0.95,
  "reasoning": "用户明确说了查北京天气，所以 city = 北京"
}}

如果某个必填参数无法从查询中提取，请在 reasoning 中说明。
请确保返回的是有效的 JSON 格式。"""

        return prompt

    def _parse_response(self, response: Dict[str, Any], skill_spec: SkillSpec) -> Dict[str, Any]:
        """解析 LLM 的响应"""

        # 提取 JSON 中的关键字段
        extracted_params = response.get("extracted_params", {})
        confidence = response.get("confidence", 0.0)
        reasoning = response.get("reasoning", "")

        # 确保信心度在 0-1 之间
        if isinstance(confidence, (int, float)):
            confidence = max(0.0, min(1.0, float(confidence)))
        else:
            confidence = 0.0

        # 清理参数（移除空值）
        cleaned_params = {k: v for k, v in extracted_params.items() if v is not None and v != ""}

        logger.debug(
            f"[参数提取] 解析响应: params={cleaned_params}, confidence={confidence}"
        )

        return {
            "extracted_params": cleaned_params,
            "confidence": confidence,
            "reasoning": reasoning,
        }


class MultiSkillExtractor:
    """多 Skill 提取器

    从用户查询中识别出多个 Skill 意图，并为每个 Skill 提取参数。
    用于处理复杂的多意图查询如 "先查南京天气然后讲个笑话"。
    """

    def __init__(self, llm: LLM, skill_specs: Dict[str, SkillSpec]):
        self.llm = llm
        self.skill_specs = skill_specs
        self.extractor = LLMParameterExtractor(llm)

    async def extract_multiple(
        self, text: str
    ) -> List[Dict[str, Any]]:
        """从查询中提取多个 Skill 的参数

        Args:
            text: 用户查询文本

        Returns:
            [{skill, params, confidence, reasoning}, ...]
        """

        # 1. 先识别 Skill 意图
        skills = await self._identify_skills(text)

        if not skills:
            logger.warning(f"[多 Skill 提取] 无法识别 Skill 意图: {text}")
            return []

        logger.info(f"[多 Skill 提取] 识别到 {len(skills)} 个 Skill: {[s['name'] for s in skills]}")

        # 2. 为每个 Skill 提取参数
        results = []
        for skill_info in skills:
            skill_name = skill_info["name"]
            if skill_name not in self.skill_specs:
                logger.warning(f"[多 Skill 提取] 未知的 Skill: {skill_name}")
                continue

            skill_spec = self.skill_specs[skill_name]

            # 提取参数
            params, confidence, reasoning = await self.extractor.extract(text, skill_spec)

            results.append({
                "skill": skill_name,
                "extracted_params": params or {},
                "confidence": confidence,
                "reasoning": reasoning,
            })

        return results

    async def _identify_skills(self, text: str) -> List[Dict[str, str]]:
        """识别用户查询中的 Skill 意图

        Args:
            text: 用户查询文本

        Returns:
            [{name, description}, ...]
        """

        # 构建 Skill 列表
        skills_desc = "\n".join([
            f"- {name}: {spec.description}"
            for name, spec in self.skill_specs.items()
        ])

        prompt = f"""从用户查询中识别出所有的 Skill 意图。

可用的 Skill:
{skills_desc}

用户查询: "{text}"

请识别用户想要执行的所有 Skill，并返回 JSON 数组:
[
  {{"name": "weather", "reason": "用户想查天气"}},
  {{"name": "joke", "reason": "用户想听笑话"}}
]

如果查询不包含任何已知 Skill，返回空数组 []。
"""

        try:
            # 将 prompt 转换为 messages 格式
            messages = [
                {"role": "user", "content": prompt}
            ]
            response = await self.llm.json_completion(
                messages,
                role="tool",  # 使用 tool 角色获取配置中的模型
                temperature=0.0,
                max_tokens=100,
            )

            # 响应可能是数组或对象，需要处理
            if isinstance(response, list):
                return response
            elif isinstance(response, dict) and "skills" in response:
                return response["skills"]
            else:
                return []

        except Exception as e:
            logger.error(f"[多 Skill 提取] 识别 Skill 失败: {str(e)}")
            return []
