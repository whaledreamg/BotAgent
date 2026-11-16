"""参数提取结果缓存

支持本地内存缓存和 TTL 策略，减少 LLM 调用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

logger = logging.getLogger("agent.param_cache")


class CacheEntry:
    """缓存条目"""

    def __init__(self, value: Any, ttl_seconds: int):
        self.value = value
        self.created_at = time.time()
        self.ttl_seconds = ttl_seconds

    def is_expired(self) -> bool:
        """检查是否过期"""
        if self.ttl_seconds <= 0:
            return False  # TTL <= 0 表示永不过期
        return time.time() - self.created_at > self.ttl_seconds

    def age_seconds(self) -> float:
        """缓存年龄（秒）"""
        return time.time() - self.created_at


class ParameterCache:
    """参数提取结果缓存

    使用内存缓存存储参数提取结果。支持按 Skill 配置 TTL。
    """

    def __init__(self, max_size: int = 10000, default_ttl: int = 1800):
        """初始化缓存

        Args:
            max_size: 最大缓存条数 (超过时采用 LRU 淘汰)
            default_ttl: 默认 TTL (秒)
        """
        self.cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self.max_size = max_size
        self.default_ttl = default_ttl
        self.skill_ttl: Dict[str, int] = {}  # 每个 Skill 的 TTL 配置

        # 统计
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def set_skill_ttl(self, skill_name: str, ttl_seconds: int) -> None:
        """为特定 Skill 设置 TTL

        Args:
            skill_name: Skill 名称
            ttl_seconds: TTL (秒)
        """
        self.skill_ttl[skill_name] = ttl_seconds
        logger.debug(f"[缓存] 设置 {skill_name} 的 TTL: {ttl_seconds}s")

    async def get(
        self, skill: str, query: str
    ) -> Optional[Dict[str, Any]]:
        """获取缓存

        Args:
            skill: Skill 名称
            query: 原始查询文本

        Returns:
            缓存的参数提取结果，或 None 如果未命中
        """
        key = self._generate_key(skill, query)

        if key not in self.cache:
            self.misses += 1
            logger.debug(f"[缓存] 未命中: {skill}")
            return None

        entry = self.cache[key]

        if entry.is_expired():
            logger.debug(f"[缓存] 已过期: {skill}")
            del self.cache[key]
            self.misses += 1
            return None

        # 移到末尾 (LRU)
        self.cache.move_to_end(key)

        self.hits += 1
        age = entry.age_seconds()
        logger.debug(f"[缓存] 命中: {skill} (年龄: {age:.1f}s)")

        return entry.value

    async def set(
        self, skill: str, query: str, value: Dict[str, Any]
    ) -> None:
        """存入缓存

        Args:
            skill: Skill 名称
            query: 原始查询文本
            value: 参数提取结果
        """
        key = self._generate_key(skill, query)

        # 获取该 Skill 的 TTL
        ttl = self.skill_ttl.get(skill, self.default_ttl)

        # 创建缓存条目
        entry = CacheEntry(value, ttl)

        # 检查是否需要淘汰
        if key not in self.cache and len(self.cache) >= self.max_size:
            # 移除最老的条目 (FIFO)
            oldest_key = next(iter(self.cache))
            del self.cache[oldest_key]
            self.evictions += 1
            logger.debug(f"[缓存] LRU 淘汰: {oldest_key}")

        # 存入缓存
        self.cache[key] = entry
        logger.debug(f"[缓存] 存入: {skill} (TTL: {ttl}s)")

    def clear(self) -> None:
        """清空缓存"""
        self.cache.clear()
        logger.info("[缓存] 已清空")

    def clear_skill(self, skill: str) -> None:
        """清空特定 Skill 的缓存"""
        keys_to_delete = [k for k in self.cache.keys() if k.startswith(f"{skill}:")]
        for key in keys_to_delete:
            del self.cache[key]
        logger.info(f"[缓存] 清空 {skill}: {len(keys_to_delete)} 条")

    def cleanup_expired(self) -> int:
        """清理过期的缓存条目

        Returns:
            清理的条目数
        """
        keys_to_delete = [
            k for k, v in self.cache.items()
            if v.is_expired()
        ]

        for key in keys_to_delete:
            del self.cache[key]

        if keys_to_delete:
            logger.info(f"[缓存] 清理过期条目: {len(keys_to_delete)} 条")

        return len(keys_to_delete)

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        total_requests = self.hits + self.misses
        hit_rate = self.hits / total_requests if total_requests > 0 else 0.0

        return {
            "size": len(self.cache),
            "max_size": self.max_size,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": hit_rate,
            "evictions": self.evictions,
            "total_requests": total_requests,
        }

    def _generate_key(self, skill: str, query: str) -> str:
        """生成缓存键

        Args:
            skill: Skill 名称
            query: 原始查询文本

        Returns:
            缓存键字符串
        """
        # 使用 Skill 名称和查询的哈希值作为键
        query_hash = hashlib.md5(query.encode("utf-8")).hexdigest()[:8]
        key = f"{skill}:{query_hash}"
        return key

    def __len__(self) -> int:
        """缓存中的条数"""
        return len(self.cache)

    def __repr__(self) -> str:
        stats = self.get_stats()
        return (
            f"ParameterCache(size={stats['size']}/{stats['max_size']}, "
            f"hit_rate={stats['hit_rate']:.1%}, "
            f"evictions={stats['evictions']})"
        )


class CacheStatCollector:
    """缓存统计收集器

    定期收集缓存的性能指标。
    """

    def __init__(self, cache: ParameterCache, interval_seconds: int = 300):
        """初始化

        Args:
            cache: 参数缓存实例
            interval_seconds: 统计间隔 (秒)
        """
        self.cache = cache
        self.interval_seconds = interval_seconds
        self.last_stats: Optional[Dict[str, Any]] = None
        self.sample_times: list[float] = []

    def collect(self) -> Dict[str, Any]:
        """收集当前统计数据"""
        stats = self.cache.get_stats()

        # 计算这个周期的增量
        if self.last_stats:
            delta_hits = stats["hits"] - self.last_stats["hits"]
            delta_misses = stats["misses"] - self.last_stats["misses"]
        else:
            delta_hits = stats["hits"]
            delta_misses = stats["misses"]

        self.last_stats = stats

        result = {
            "timestamp": time.time(),
            "cache_size": stats["size"],
            "total_hit_rate": stats["hit_rate"],
            "period_hits": delta_hits,
            "period_misses": delta_misses,
            "period_hit_rate": delta_hits / (delta_hits + delta_misses)
            if (delta_hits + delta_misses) > 0
            else 0.0,
            "evictions": stats["evictions"],
        }

        return result
