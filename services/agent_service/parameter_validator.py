"""参数验证和提取模块。

负责：
1. 验证 LLM 返回的参数是否满足约束
2. 从原文提取缺失的参数
3. 处理参数补全失败的情况
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("agent.parameter_validator")


@dataclass
class ParamConstraint:
    """参数约束定义。"""

    #: 参数名
    name: str = ""
    #: 是否必填
    required: bool = False
    #: 参数类型 (str, int, float, bool, list)
    param_type: str = "str"
    #: 验证函数：接收参数值，返回 (is_valid, error_msg)
    #: 例如：lambda x: (len(x) <= 50, "参数过长")
    validator: Optional[callable] = None
    #: 参数的正则提取模式 (用于从原文补全)
    extraction_pattern: Optional[str] = None
    #: 混合提取器类型（用于从原文混合提取）
    #: 如果为空，默认使用参数名作为提取器类型
    extraction_type: Optional[str] = None
    #: 参数的示例值
    example: str = ""
    #: 参数描述
    description: str = ""


@dataclass
class ValidationResult:
    """参数验证结果。"""

    #: 是否所有参数都有效
    is_valid: bool = False
    #: 完整的参数字典 (已补全)
    params: Dict[str, Any] = None
    #: 缺失的参数列表
    missing_params: List[str] = None
    #: 验证失败的参数及其错误
    errors: Dict[str, str] = None

    def __post_init__(self):
        if self.params is None:
            self.params = {}
        if self.missing_params is None:
            self.missing_params = []
        if self.errors is None:
            self.errors = {}


class ParameterValidator:
    """参数验证器。"""

    def __init__(self, extraction_manager: Optional[Any] = None):
        """初始化验证器。

        Args:
            extraction_manager: 混合参数提取管理器（可选）
        """
        self.constraints: Dict[str, List[ParamConstraint]] = {}
        self.extraction_manager = extraction_manager

    def register_skill(self, skill_name: str, constraints: List[ParamConstraint]):
        """注册一个 Skill 的参数约束。

        Args:
            skill_name: Skill 名称 (如 'weather', 'time')
            constraints: 参数约束列表
        """
        self.constraints[skill_name] = constraints
        logger.info(f"[参数验证器] 注册 Skill {skill_name}: {len(constraints)} 个参数")

    def validate(
        self,
        skill_name: str,
        params: Dict[str, Any],
        raw_text: Optional[str] = None,
    ) -> ValidationResult:
        """验证并补全参数。

        Args:
            skill_name: Skill 名称
            params: LLM 返回的参数
            raw_text: 原始用户输入 (用于参数提取)

        Returns:
            ValidationResult 对象
        """
        result = ValidationResult()

        if skill_name not in self.constraints:
            # 未知 Skill，直接返回
            result.is_valid = True
            result.params = dict(params) if params else {}
            return result

        constraints = self.constraints[skill_name]
        result.params = dict(params) if params else {}

        # 第 1 步：验证已有参数
        for constraint in constraints:
            param_value = result.params.get(constraint.name)

            # 参数存在时的验证
            if param_value is not None:
                # 类型检查
                if not self._check_type(param_value, constraint.param_type):
                    result.errors[constraint.name] = (
                        f"参数类型不匹配，期望 {constraint.param_type}，"
                        f"实际 {type(param_value).__name__}"
                    )
                    continue

                # 自定义验证器
                if constraint.validator:
                    is_valid, error_msg = constraint.validator(param_value)
                    if not is_valid:
                        result.errors[constraint.name] = error_msg
                        continue
            else:
                # 参数不存在
                # 情况 1：尝试从原文提取
                if raw_text and constraint.extraction_pattern:
                    extracted_value = self._extract_param(
                        raw_text, constraint.extraction_pattern
                    )
                    if extracted_value is not None:
                        result.params[constraint.name] = extracted_value
                        logger.info(
                            f"[参数提取] {skill_name}.{constraint.name} "
                            f"从原文提取: {extracted_value}"
                        )
                        continue

                # 情况 2：是必填参数
                if constraint.required:
                    result.missing_params.append(constraint.name)

        # 第 2 步：检查验证结果
        result.is_valid = not result.errors and not result.missing_params

        if not result.is_valid:
            logger.warning(
                f"[参数验证] {skill_name} 验证失败: "
                f"缺失={result.missing_params}, 错误={list(result.errors.keys())}"
            )
        else:
            logger.info(f"[参数验证] {skill_name} 验证通过")

        return result

    @staticmethod
    def _check_type(value: Any, expected_type: str) -> bool:
        """检查值的类型是否匹配。"""
        type_map = {
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "list": list,
            "dict": dict,
        }
        expected = type_map.get(expected_type)
        if expected is None:
            return True
        return isinstance(value, expected)

    @staticmethod
    def _extract_param(raw_text: str, pattern: str) -> Optional[Any]:
        """从原文中使用正则提取参数。

        Args:
            raw_text: 原始文本
            pattern: 正则表达式模式

        Returns:
            提取的值，或 None
        """
        if not pattern:
            return None

        try:
            match = re.search(pattern, raw_text)
            if match:
                if match.groups():
                    # 如果有捕获组，返回第一个
                    return match.group(1)
                else:
                    # 否则返回整个匹配
                    return match.group(0)
        except re.error as e:
            logger.error(f"[参数提取] 正则错误: {e}")

        return None

    async def async_validate(
        self,
        skill_name: str,
        params: Dict[str, Any],
        raw_text: Optional[str] = None,
    ) -> ValidationResult:
        """异步版本的参数验证和补全。

        支持混合参数提取（正则 + LLM 降级）。

        Args:
            skill_name: Skill 名称
            params: LLM 返回的参数
            raw_text: 原始用户输入

        Returns:
            ValidationResult 对象
        """
        result = ValidationResult()

        if skill_name not in self.constraints:
            # 未知 Skill，直接返回
            result.is_valid = True
            result.params = dict(params) if params else {}
            return result

        constraints = self.constraints[skill_name]
        result.params = dict(params) if params else {}

        # 第 1 步：验证已有参数
        for constraint in constraints:
            param_value = result.params.get(constraint.name)

            # 参数存在时的验证
            if param_value is not None:
                # 类型检查
                if not self._check_type(param_value, constraint.param_type):
                    result.errors[constraint.name] = (
                        f"参数类型不匹配，期望 {constraint.param_type}，"
                        f"实际 {type(param_value).__name__}"
                    )
                    continue

                # 自定义验证器
                if constraint.validator:
                    is_valid, error_msg = constraint.validator(param_value)
                    if not is_valid:
                        result.errors[constraint.name] = error_msg
                        continue
            else:
                # 参数不存在
                # 情况 1：尝试从原文提取（支持混合提取）
                if raw_text and self.extraction_manager:
                    # 使用混合提取管理器
                    # 优先使用 extraction_type，如果不存在则使用参数名
                    extractor_type = constraint.extraction_type or constraint.name
                    extracted_value = await self.extraction_manager.hybrid_extract(
                        extractor_type,
                        raw_text,
                        fallback_to_llm=True,
                    )
                    if extracted_value:
                        result.params[constraint.name] = extracted_value
                        logger.info(
                            f"[参数验证] {skill_name}.{constraint.name} "
                            f"混合提取成功: {extracted_value}"
                        )
                        continue
                elif raw_text and constraint.extraction_pattern:
                    # 回退到正则提取
                    extracted_value = self._extract_param(
                        raw_text, constraint.extraction_pattern
                    )
                    if extracted_value is not None:
                        result.params[constraint.name] = extracted_value
                        logger.info(
                            f"[参数验证] {skill_name}.{constraint.name} "
                            f"正则提取成功: {extracted_value}"
                        )
                        continue

                # 情况 2：是必填参数
                if constraint.required:
                    result.missing_params.append(constraint.name)

        # 第 2 步：检查验证结果
        result.is_valid = not result.errors and not result.missing_params

        if not result.is_valid:
            logger.warning(
                f"[参数验证] {skill_name} 验证失败: "
                f"缺失={result.missing_params}, 错误={list(result.errors.keys())}"
            )
        else:
            logger.info(f"[参数验证] {skill_name} 验证通过")

        return result


def create_default_validator(
    extraction_manager: Optional[Any] = None,
) -> ParameterValidator:
    """创建默认的参数验证器，包含常见 Skill 的约束。

    Args:
        extraction_manager: 混合参数提取管理器（可选）

    Returns:
        ParameterValidator 实例
    """
    validator = ParameterValidator(extraction_manager=extraction_manager)

    # 天气查询
    weather_constraints = [
        ParamConstraint(
            name="city",
            required=False,
            param_type="str",
            extraction_pattern=r"([\u4e00-\u9fff]+市|[\u4e00-\u9fff]+州)",  # 中文城市名
            example="北京",
            description="城市名称",
            validator=lambda x: (
                len(str(x).strip()) > 0 and len(str(x).strip()) <= 50,
                "城市名无效或过长",
            ),
        ),
    ]
    validator.register_skill("weather", weather_constraints)

    # 时间查询
    time_constraints = []
    validator.register_skill("time", time_constraints)

    # 日期查询
    date_constraints = []
    validator.register_skill("date", date_constraints)

    # 查找物体
    find_object_constraints = [
        ParamConstraint(
            name="name",
            required=True,
            param_type="str",
            extraction_type="object",  # 使用 object 提取器
            example="手机",
            description="物体名称",
            validator=lambda x: (
                len(str(x).strip()) > 0 and len(str(x).strip()) <= 50,
                "物体名无效",
            ),
        ),
    ]
    validator.register_skill("find_object", find_object_constraints)

    return validator
