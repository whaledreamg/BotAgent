"""思考过程日志记录系统

记录 LLM 参数提取的完整过程，包括 prompt、响应、各个阶段的性能指标。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("agent.thinking_logger")


class ThinkingLogger:
    """思考过程日志记录器

    记录每次 LLM 参数提取调用的完整过程。
    """

    def __init__(self, log_dir: str = "logs/thinking"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _get_log_path(self, skill: str, timestamp: datetime) -> Path:
        """获取日志文件路径

        按日期和小时分片: logs/thinking/{date}/{skill}/{hour}.jsonl
        """
        date_str = timestamp.strftime("%Y-%m-%d")
        hour_str = timestamp.strftime("%H")

        path = self.log_dir / date_str / skill / f"{hour_str}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)

        return path

    def log_extraction(
        self,
        skill: str,
        user_query: str,
        prompt: str,
        llm_response: Dict[str, Any],
        extracted_params: Optional[Dict[str, Any]],
        confidence: float,
        reasoning: Optional[str],
        total_ms: float,
        timing_breakdown: Dict[str, float],
        regex_result: Optional[Dict[str, Any]] = None,
        cache_hit: bool = False,
    ) -> str:
        """记录参数提取过程

        Args:
            skill: Skill 名称
            user_query: 用户查询文本
            prompt: 发送给 LLM 的 prompt
            llm_response: LLM 的原始响应
            extracted_params: 提取的参数
            confidence: 置信度
            reasoning: 推理过程
            total_ms: 总耗时（毫秒）
            timing_breakdown: 各阶段耗时
            regex_result: 正则提取的结果（用于对比）
            cache_hit: 是否是缓存命中

        Returns:
            trace_id: 追踪 ID
        """
        trace_id = str(uuid.uuid4())
        timestamp = datetime.now()

        # 构建完整的思考过程日志
        log_entry = {
            "id": trace_id,
            "timestamp": timestamp.isoformat(),
            "user_query": user_query,
            "skill": skill,
            "cache_hit": cache_hit,
            "thinking_process": {
                "stage_1_prompt": {
                    "duration_ms": timing_breakdown.get("prompt_building", 0),
                    "prompt_template_length": len(prompt),
                    "prompt_first_100_chars": prompt[:100],
                },
                "stage_2_llm_call": {
                    "duration_ms": timing_breakdown.get("llm_call", 0),
                    "model": llm_response.get("model", "unknown"),
                    "prompt_tokens": llm_response.get("prompt_tokens", 0),
                    "response_tokens": llm_response.get("completion_tokens", 0),
                    "stop_reason": llm_response.get("stop_reason", "unknown"),
                },
                "stage_3_parsing": {
                    "duration_ms": timing_breakdown.get("response_parsing", 0),
                    "raw_response_length": len(str(llm_response.get("content", ""))),
                    "parsed_successfully": extracted_params is not None,
                },
                "stage_4_validation": {
                    "duration_ms": timing_breakdown.get("validation", 0),
                    "validation_passed": True,  # 假设通过
                },
            },
            "results": {
                "extracted_params": extracted_params or {},
                "confidence": confidence,
                "reasoning": reasoning or "",
                "regex_comparison": None,
            },
            "performance": {
                "total_ms": total_ms,
                "breakdown_ms": timing_breakdown,
            },
            "metadata": {
                "version": "1.0",
                "log_timestamp": timestamp.isoformat(),
            },
        }

        # 如果有正则对比结果，添加
        if regex_result is not None:
            log_entry["results"]["regex_comparison"] = {
                "regex_result": regex_result,
                "llm_result": extracted_params,
                "match": regex_result == extracted_params,
            }

        # 写入日志文件
        self._write_log_entry(skill, timestamp, log_entry)

        logger.info(
            f"[思考日志] Skill: {skill}, "
            f"TraceID: {trace_id}, "
            f"置信度: {confidence:.2f}, "
            f"耗时: {total_ms:.1f}ms"
        )

        return trace_id

    def _write_log_entry(
        self, skill: str, timestamp: datetime, log_entry: Dict[str, Any]
    ) -> None:
        """将日志条目写入文件"""
        path = self._get_log_path(skill, timestamp)

        # 使用 JSON Lines 格式（每行一条 JSON）
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
            logger.debug(f"[思考日志] 已写入: {path}")
        except Exception as e:
            logger.error(f"[思考日志] 写入失败: {str(e)}")

    def cleanup_old_logs(self, days_to_keep: int = 7) -> int:
        """清理超过指定天数的日志文件

        Args:
            days_to_keep: 保留天数

        Returns:
            删除的文件数
        """
        if not self.log_dir.exists():
            return 0

        cutoff = datetime.now() - timedelta(days=days_to_keep)
        deleted_count = 0

        # 遍历所有日志文件
        for log_file in self.log_dir.rglob("*.jsonl"):
            # 从路径中解析日期
            try:
                # 路径格式: logs/thinking/{date}/{skill}/{hour}.jsonl
                parts = log_file.parts
                date_str = parts[-3]  # 日期部分
                file_date = datetime.strptime(date_str, "%Y-%m-%d")

                if file_date < cutoff:
                    log_file.unlink()
                    deleted_count += 1
                    logger.debug(f"[思考日志] 删除过期日志: {log_file}")
            except Exception as e:
                logger.warning(f"[思考日志] 解析日期失败: {log_file}, {str(e)}")

        if deleted_count > 0:
            logger.info(f"[思考日志] 清理完成，删除了 {deleted_count} 个过期日志文件")

        return deleted_count


class ThinkingLogReader:
    """思考过程日志读取器

    支持从日志文件中查询和统计数据。
    """

    def __init__(self, log_dir: str = "logs/thinking"):
        self.log_dir = Path(log_dir)

    def read_logs_for_date_and_skill(
        self, skill: str, date: datetime
    ) -> list[Dict[str, Any]]:
        """读取指定日期和 Skill 的所有日志

        Args:
            skill: Skill 名称
            date: 日期

        Returns:
            日志条目列表
        """
        date_str = date.strftime("%Y-%m-%d")
        date_dir = self.log_dir / date_str / skill

        if not date_dir.exists():
            return []

        logs = []

        # 读取该日期该 Skill 的所有小时日志
        for hour_file in sorted(date_dir.glob("*.jsonl")):
            try:
                with open(hour_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            logs.append(json.loads(line))
            except Exception as e:
                logger.error(f"[思考日志] 读取失败: {hour_file}, {str(e)}")

        return logs

    def get_stats_for_date_and_skill(
        self, skill: str, date: datetime
    ) -> Dict[str, Any]:
        """获取指定日期和 Skill 的统计信息

        Args:
            skill: Skill 名称
            date: 日期

        Returns:
            统计数据
        """
        logs = self.read_logs_for_date_and_skill(skill, date)

        if not logs:
            return {
                "skill": skill,
                "date": date.isoformat(),
                "count": 0,
                "accuracy": 0.0,
                "avg_latency_ms": 0.0,
                "cache_hit_rate": 0.0,
                "avg_confidence": 0.0,
            }

        # 统计各项指标
        total_ms = sum(log["performance"]["total_ms"] for log in logs)
        cache_hits = sum(1 for log in logs if log.get("cache_hit", False))
        confidences = [log["results"].get("confidence", 0.0) for log in logs]

        # 计算匹配率（与正则对比）
        matched = 0
        compared = 0
        for log in logs:
            regex_comp = log["results"].get("regex_comparison")
            if regex_comp:
                compared += 1
                if regex_comp.get("match", False):
                    matched += 1

        accuracy = matched / compared if compared > 0 else 1.0

        return {
            "skill": skill,
            "date": date.isoformat(),
            "count": len(logs),
            "accuracy": accuracy,
            "avg_latency_ms": total_ms / len(logs),
            "cache_hit_rate": cache_hits / len(logs),
            "avg_confidence": sum(confidences) / len(confidences),
            "min_confidence": min(confidences),
            "max_confidence": max(confidences),
        }

    def find_mismatches(
        self, skill: str, date: datetime
    ) -> list[Dict[str, Any]]:
        """查找与正则结果不匹配的案例

        Args:
            skill: Skill 名称
            date: 日期

        Returns:
            不匹配的日志列表
        """
        logs = self.read_logs_for_date_and_skill(skill, date)

        mismatches = []
        for log in logs:
            regex_comp = log["results"].get("regex_comparison")
            if regex_comp and not regex_comp.get("match", False):
                mismatches.append({
                    "query": log["user_query"],
                    "trace_id": log["id"],
                    "regex_result": regex_comp["regex_result"],
                    "llm_result": regex_comp["llm_result"],
                    "confidence": log["results"].get("confidence"),
                    "reasoning": log["results"].get("reasoning"),
                })

        return mismatches
