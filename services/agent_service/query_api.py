"""思考过程日志查询 API

提供 HTTP 接口查询参数提取的思考过程日志。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from thinking_logger import ThinkingLogReader

logger = logging.getLogger("agent.query_api")

router = APIRouter(prefix="/v1/thinking", tags=["thinking"])
reader = ThinkingLogReader()


@router.get("/logs/{trace_id}")
async def get_trace_log(trace_id: str) -> Dict[str, Any]:
    """查询单条思考过程日志

    Args:
        trace_id: 追踪 ID

    Returns:
        完整的思考过程 JSON
    """
    # TODO: 实现基于 trace_id 的精确查询
    # 需要构建索引或遍历日志文件
    return {
        "error": "功能开发中",
        "trace_id": trace_id,
    }


@router.get("/logs")
async def list_logs(
    skill: Optional[str] = Query(None, description="Skill 名称"),
    start_time: Optional[str] = Query(None, description="开始时间 (ISO 8601)"),
    end_time: Optional[str] = Query(None, description="结束时间 (ISO 8601)"),
    confidence_min: float = Query(0.0, description="最小置信度"),
    confidence_max: float = Query(1.0, description="最大置信度"),
    page: int = Query(1, ge=1, description="页码"),
    per_page: int = Query(50, ge=1, le=200, description="每页条数"),
) -> Dict[str, Any]:
    """按条件查询思考日志

    Args:
        skill: Skill 名称（可选）
        start_time: 开始时间（可选）
        end_time: 结束时间（可选）
        confidence_min: 最小置信度
        confidence_max: 最大置信度
        page: 页码
        per_page: 每页条数

    Returns:
        分页的日志查询结果
    """
    # TODO: 实现完整的条件查询和分页
    return {
        "total": 0,
        "page": page,
        "per_page": per_page,
        "results": [],
    }


@router.get("/stats")
async def get_stats(
    skill: Optional[str] = Query(None, description="Skill 名称"),
    group_by: str = Query("hour", description="分组方式: hour/day"),
    start_time: Optional[str] = Query(None, description="开始时间 (ISO 8601)"),
    end_time: Optional[str] = Query(None, description="结束时间 (ISO 8601)"),
) -> Dict[str, Any]:
    """查询统计数据

    Args:
        skill: Skill 名称（可选，不指定则返回全部）
        group_by: 分组方式 (hour/day)
        start_time: 开始时间（可选）
        end_time: 结束时间（可选）

    Returns:
        统计数据
    """
    # 默认查询最近 7 天
    if not end_time:
        end_date = datetime.now()
    else:
        end_date = datetime.fromisoformat(end_time)

    if not start_time:
        start_date = end_date - timedelta(days=7)
    else:
        start_date = datetime.fromisoformat(start_time)

    # 构建结果
    result = {}

    if not skill:
        # 如果没指定 Skill，返回多个 Skill 的统计
        skills = ["weather", "find_object", "joke", "play_music", "set_alarm"]
        for s in skills:
            result[s] = []
    else:
        result[skill] = []

    # TODO: 遍历日期范围，收集统计数据
    # 根据 group_by 按小时或按天分组

    return result


@router.get("/compare")
async def compare_llm_vs_regex(
    skill: Optional[str] = Query(None, description="Skill 名称"),
    date: Optional[str] = Query(None, description="查询日期 (ISO 8601)"),
) -> Dict[str, Any]:
    """LLM vs 正则对比分析

    Args:
        skill: Skill 名称（可选）
        date: 查询日期（默认今天）

    Returns:
        对比分析结果
    """
    if not date:
        compare_date = datetime.now()
    else:
        compare_date = datetime.fromisoformat(date)

    result = {}

    if not skill:
        # 如果没指定 Skill，遍历所有 Skill
        skills = ["weather", "find_object", "joke", "play_music", "set_alarm"]
        for s in skills:
            stats = reader.get_stats_for_date_and_skill(s, compare_date)
            mismatches = reader.find_mismatches(s, compare_date)

            result[s] = {
                "total_samples": stats.get("count", 0),
                "accuracy": stats.get("accuracy", 0.0),
                "mismatch_count": len(mismatches),
                "mismatches": mismatches[:10],  # 返回前 10 个
            }
    else:
        # 指定了 Skill
        stats = reader.get_stats_for_date_and_skill(skill, compare_date)
        mismatches = reader.find_mismatches(skill, compare_date)

        result = {
            "skill": skill,
            "date": compare_date.isoformat(),
            "llm_vs_regex": {
                "total_samples": stats.get("count", 0),
                "match_count": stats.get("count", 0) * stats.get("accuracy", 0.0),
                "mismatch_count": len(mismatches),
                "accuracy": stats.get("accuracy", 0.0),
                "avg_latency_ms": stats.get("avg_latency_ms", 0.0),
                "avg_confidence": stats.get("avg_confidence", 0.0),
                "mismatches": mismatches,
            },
        }

    return result


@router.get("/mismatches")
async def get_mismatches(
    skill: str = Query(..., description="Skill 名称"),
    date: Optional[str] = Query(None, description="查询日期"),
    limit: int = Query(100, ge=1, le=1000, description="返回条数"),
) -> Dict[str, Any]:
    """查询 LLM 和正则不匹配的案例

    Args:
        skill: Skill 名称
        date: 查询日期
        limit: 返回条数

    Returns:
        不匹配的日志列表
    """
    if not date:
        compare_date = datetime.now()
    else:
        compare_date = datetime.fromisoformat(date)

    mismatches = reader.find_mismatches(skill, compare_date)

    return {
        "skill": skill,
        "date": compare_date.isoformat(),
        "total_mismatches": len(mismatches),
        "results": mismatches[:limit],
    }


@router.get("/health")
async def health_check() -> Dict[str, str]:
    """健康检查"""
    return {"status": "ok", "service": "thinking_logger_api"}
