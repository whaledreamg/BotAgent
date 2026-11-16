"""混合参数提取管理器：LLM-First + 缓存 + 正则降级。

新架构（LLM-First）:
- 第 1 层: 缓存（快速路径，1ms）
- 第 2 层: LLM（可靠路径，100ms）
- 第 3 层: 正则（降级路径，1ms）

保留对旧的正则-优先方案的支持（hybrid_extract），用于对比。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

from llm_parameter_extractor import LLMParameterExtractor, SkillSpec
from parameter_cache import ParameterCache
from parameter_extractors import ParameterExtractorRegistry, get_global_registry
from thinking_logger import ThinkingLogger

logger = logging.getLogger("agent.parameter_extraction_manager")


class ParameterExtractionManager:
    """改进的混合参数提取管理器。
    
    支持两种模式：
    1. LLM-First（新）: 缓存 → LLM → 正则
    2. Hybrid（旧）: 正则 → LLM
    """
    
    def __init__(
        self,
        llm: Optional[Any] = None,
        registry: Optional[ParameterExtractorRegistry] = None,
        cache: Optional[ParameterCache] = None,
        llm_extractor: Optional[LLMParameterExtractor] = None,
        thinking_logger: Optional[ThinkingLogger] = None,
    ):
        """初始化参数提取管理器。

        Args:
            llm: LLM 实例（可选）
            registry: 提取器注册表（可选，使用全局注册表）
            cache: 参数缓存（可选）
            llm_extractor: LLM 参数提取器（可选）
            thinking_logger: 思考过程日志记录器（可选）
        """
        self.llm = llm
        self.registry = registry or get_global_registry()
        self.cache = cache or ParameterCache()
        self.llm_extractor = llm_extractor or (LLMParameterExtractor(llm) if llm else None)
        self.thinking_logger = thinking_logger or ThinkingLogger()
        
        # 统计信息
        self.stats = {
            "cache_hits": 0,
            "llm_calls": 0,
            "regex_fallbacks": 0,
            "llm_vs_regex_matches": 0,
            "llm_vs_regex_mismatches": 0,
        }

    async def llm_first_extract(
        self,
        text: str,
        skill_spec: SkillSpec,
    ) -> Tuple[Optional[Dict[str, Any]], float, Optional[str]]:
        """LLM-First 提取：缓存 → LLM → 正则
        
        这是新的推荐方法，提供更高的可靠性。

        Args:
            text: 用户查询文本
            skill_spec: Skill 规格定义

        Returns:
            (提取的参数, 置信度, 推理过程或错误信息)
        """
        start_time = time.time()
        timing_breakdown = {}

        # 第 1 层：缓存查询
        cache_start = time.time()
        cached_result = await self.cache.get(skill_spec.name, text)
        timing_breakdown["cache_check"] = (time.time() - cache_start) * 1000

        if cached_result:
            self.stats["cache_hits"] += 1
            logger.info(
                f"[LLM-First] {skill_spec.name} 缓存命中: {cached_result.get('extracted_params')}"
            )
            return (
                cached_result.get("extracted_params"),
                cached_result.get("confidence", 1.0),
                "缓存命中",
            )

        # 第 2 层：LLM 提取
        if self.llm_extractor:
            llm_start = time.time()
            try:
                params, confidence, reasoning = await self.llm_extractor.extract(
                    text, skill_spec
                )
                timing_breakdown["llm_extraction"] = (time.time() - llm_start) * 1000

                if params is not None:
                    self.stats["llm_calls"] += 1

                    # 存入缓存
                    cache_store_start = time.time()
                    await self.cache.set(
                        skill_spec.name,
                        text,
                        {
                            "extracted_params": params,
                            "confidence": confidence,
                            "reasoning": reasoning,
                        },
                    )
                    timing_breakdown["cache_store"] = (time.time() - cache_store_start) * 1000

                    # 对比正则结果（用于监控）
                    regex_result = None
                    if skill_spec.name in ["weather", "find_object"]:
                        regex_result = await self._extract_with_regex(
                            skill_spec.name, text
                        )

                    # 记录思考过程
                    total_ms = (time.time() - start_time) * 1000
                    if self.thinking_logger:
                        # 获取实际使用的模型名
                        model_name = "unknown"
                        if self.llm_extractor and self.llm_extractor.llm:
                            try:
                                targets = self.llm_extractor.llm.resolve(role="tool")
                                if targets and hasattr(targets[0], 'model'):
                                    model_name = targets[0].model
                            except:
                                pass
                        
                        self.thinking_logger.log_extraction(
                            skill=skill_spec.name,
                            user_query=text,
                            prompt="[LLM 提取 prompt]",
                            llm_response={
                                "model": model_name,
                                "content": reasoning or "",
                            },
                            extracted_params=params,
                            confidence=confidence,
                            reasoning=reasoning,
                            total_ms=total_ms,
                            timing_breakdown=timing_breakdown,
                            regex_result=regex_result,
                            cache_hit=False,
                        )

                    # 对比 LLM vs 正则
                    if regex_result and regex_result != params:
                        self.stats["llm_vs_regex_mismatches"] += 1
                        logger.warning(
                            f"[LLM-First] {skill_spec.name} LLM vs 正则不匹配: "
                            f"LLM={params}, 正则={regex_result}"
                        )
                    elif regex_result:
                        self.stats["llm_vs_regex_matches"] += 1

                    return params, confidence, reasoning

            except Exception as e:
                timing_breakdown["llm_extraction"] = (time.time() - llm_start) * 1000
                logger.warning(
                    f"[LLM-First] {skill_spec.name} LLM 提取失败: {str(e)}"
                )

        # 第 3 层：正则降级
        logger.info(f"[LLM-First] {skill_spec.name} 降级到正则...")
        regex_start = time.time()
        regex_result = await self._extract_with_regex(skill_spec.name, text)
        timing_breakdown["regex_fallback"] = (time.time() - regex_start) * 1000

        if regex_result:
            self.stats["regex_fallbacks"] += 1
            await self.cache.set(
                skill_spec.name,
                text,
                {
                    "extracted_params": regex_result,
                    "confidence": 0.7,
                    "reasoning": "正则降级",
                },
            )
            return regex_result, 0.7, "正则降级"

        return None, 0.0, "参数提取失败"

    async def _extract_with_regex(
        self, skill_name: str, text: str
    ) -> Optional[Dict[str, Any]]:
        """使用正则提取（用于降级和对比）"""
        try:
            if skill_name == "weather":
                city = self.registry.extract("city", text)
                return {"city": city} if city else None
            elif skill_name == "find_object":
                obj = self.registry.extract("object", text)
                return {"name": obj} if obj else None
        except Exception as e:
            logger.debug(f"[LLM-First] 正则提取异常: {str(e)}")
        return None

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        cache_stats = self.cache.get_stats()
        return {
            "cache": cache_stats,
            "extraction": {
                "cache_hits": self.stats["cache_hits"],
                "llm_calls": self.stats["llm_calls"],
                "regex_fallbacks": self.stats["regex_fallbacks"],
                "llm_vs_regex_matches": self.stats["llm_vs_regex_matches"],
                "llm_vs_regex_mismatches": self.stats["llm_vs_regex_mismatches"],
            },
        }

    async def hybrid_extract(
        self,
        param_type: str,
        text: str,
        fallback_to_llm: bool = True,
    ) -> Optional[str]:
        """混合提取参数：正则优先 + LLM 降级（旧方法，保留用于对比）。

        Args:
            param_type: 参数类型（如 "city", "object"）
            text: 输入文本
            fallback_to_llm: 是否在正则失败时降级到 LLM

        Returns:
            提取的参数值，如果提取失败返回 None
        """
        # 第 1 步：尝试正则提取（快速路径）
        result = self.registry.extract(param_type, text)

        # 第 2 步：验证结果有效性
        if result and self.registry.is_valid_result(param_type, result):
            logger.info(
                f"[混合提取] {param_type} 正则提取成功（快速路径）: {result}"
            )
            return result

        # 第 3 步：正则失败或结果异常，降级到 LLM（精准路径）
        if fallback_to_llm and self.llm:
            logger.info(f"[混合提取] {param_type} 正则失败，启用 LLM 降级...")
            llm_result = await self._extract_with_llm(param_type, text)
            if llm_result:
                logger.info(
                    f"[混合提取] {param_type} LLM 提取成功（精准路径）: {llm_result}"
                )
                return llm_result

        logger.warning(f"[混合提取] {param_type} 提取失败: {text[:50]}")
        return None

    async def _extract_with_llm(self, param_type: str, text: str) -> Optional[str]:
        """使用 LLM 提取参数（旧方法）。

        Args:
            param_type: 参数类型
            text: 输入文本

        Returns:
            提取的参数值，如果提取失败返回 None
        """
        if not self.llm:
            return None

        try:
            prompts = {
                "city": "从这句话里提取城市名。返回只包含城市名，不要其他文字。如果没有城市，返回空。",
                "object": "从这句话里提取物体名。返回只包含物体名，不要其他文字。如果没有物体，返回空。",
                "location": "从这句话里提取位置/地点名。返回只包含位置名，不要其他文字。如果没有位置，返回空。",
            }
            system_prompt = prompts.get(
                param_type,
                f"从这句话里提取{param_type}。返回只包含{param_type}，不要其他文字。",
            )

            prompt = f"""{system_prompt}

句子: "{text}"

{param_type}:"""

            response = await self.llm.complete(
                prompt,
                max_tokens=20,
                temperature=0,
            )

            result = response.strip()

            if (
                result
                and 1 <= len(result) <= 10
                and all("\u4e00" <= c <= "\u9fff" or c.isalnum() for c in result)
            ):
                return result
            else:
                logger.debug(f"[混合提取] LLM 提取结果无效: {result}")
                return None

        except Exception as e:
            logger.debug(f"[混合提取] LLM 提取失败: {e}")
            return None

    def extract_without_llm(self, param_type: str, text: str) -> Optional[str]:
        """仅使用正则提取参数（不调用 LLM）。

        Args:
            param_type: 参数类型
            text: 输入文本

        Returns:
            提取的参数值，如果提取失败返回 None
        """
        return self.registry.extract(param_type, text)

    def is_valid_result(self, param_type: str, value: str) -> bool:
        """检查提取结果是否有效。

        Args:
            param_type: 参数类型
            value: 提取的值

        Returns:
            是否有效
        """
        return self.registry.is_valid_result(param_type, value)


# 全局混合提取管理器实例
_global_manager: Optional[ParameterExtractionManager] = None


def get_global_manager() -> ParameterExtractionManager:
    """获取全局混合提取管理器。

    Returns:
        全局 ParameterExtractionManager 实例
    """
    global _global_manager
    if _global_manager is None:
        _global_manager = ParameterExtractionManager()
    return _global_manager


def set_global_manager(manager: ParameterExtractionManager):
    """设置全局混合提取管理器。

    Args:
        manager: ParameterExtractionManager 实例
    """
    global _global_manager
    _global_manager = manager
    logger.info("[混合提取] 全局管理器已设置")


__all__ = [
    "ParameterExtractionManager",
    "get_global_manager",
    "set_global_manager",
]
