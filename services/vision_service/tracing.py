"""视觉识别追溯系统。

功能:
  - 保存每次识别后的图像 (带标注的检测框)
  - 记录每个识别到的物体信息 (类别、置信度、位置等)
  - 提供历史追溯查询接口

目录结构:
  traces/
    ├── images/           # 保存的图像 (IMG_20240101_120530_000.jpg)
    ├── records.jsonl     # 结构化记录 (每行一条检测记录)
    └── index.json        # 索引和元数据
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger("vision.tracing")


@dataclass
class DetectionRecord:
    """单个物体的识别记录。"""

    #: 记录唯一 ID (Unix 时间戳毫秒 + 序号)
    record_id: str
    #: 识别的时间戳 (ISO 8601)
    timestamp: str
    #: 图像文件名
    image_filename: str
    #: 物体类别
    label: str
    #: 置信度 (0~1)
    confidence: float
    #: 像素框 (x1, y1, x2, y2)
    bbox: Tuple[float, float, float, float]
    #: 3D 位置 (如果有)
    pose: Optional[Dict[str, Any]] = None
    #: 坐标系
    frame: str = "camera"
    #: 补充信息
    extra: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典 (用于 JSONL 保存)。"""
        d = asdict(self)
        if self.bbox:
            d["bbox"] = list(self.bbox)
        return d


class DetectionTracer:
    """识别追溯记录器。

    线程安全。支持异步写入 (可选)。
    """

    def __init__(self, trace_root: str | Path = "traces") -> None:
        self.trace_root = Path(trace_root)
        self.image_dir = self.trace_root / "images"
        self.records_file = self.trace_root / "records.jsonl"
        self.index_file = self.trace_root / "index.json"

        self._lock = threading.Lock()
        self._record_count = 0
        self._image_count = 0
        self._index_data = {"created_at": datetime.now().isoformat()}

        self._setup()

    def _setup(self) -> None:
        """初始化目录结构。"""
        try:
            self.image_dir.mkdir(parents=True, exist_ok=True)
            logger.info("追溯系统已初始化: %s", self.trace_root)
        except Exception as exc:
            logger.error("初始化追溯系统失败: %s", exc)

    def record(
        self,
        label: str,
        confidence: float,
        bbox: Tuple[float, float, float, float],
        image: np.ndarray,
        pose: Optional[Dict[str, Any]] = None,
        frame: str = "camera",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[DetectionRecord]:
        """记录一次识别结果。

        Args:
            label: 物体类别
            confidence: 置信度
            bbox: 像素框 (x1, y1, x2, y2)
            image: 彩色图像 (BGR)
            pose: 3D 位置信息
            frame: 坐标系名
            extra: 额外信息

        Returns:
            记录对象; 保存失败时返回 None
        """
        try:
            # 生成记录 ID 和文件名
            ts_ms = int(time.time() * 1000)
            with self._lock:
                self._record_count += 1
                seq = self._record_count
            record_id = f"{ts_ms}_{seq:04d}"

            # 保存图像
            image_filename = None
            if image is not None and image.size > 0:
                image_filename = self._save_image(image, record_id, bbox)

            # 创建记录
            record = DetectionRecord(
                record_id=record_id,
                timestamp=datetime.now().isoformat(),
                image_filename=image_filename or "",
                label=label,
                confidence=float(confidence),
                bbox=bbox,
                pose=pose,
                frame=frame,
                extra=extra,
            )

            # 追加到 JSONL
            self._append_record(record)

            # 更新索引
            self._update_index(record)

            return record
        except Exception as exc:
            logger.error("记录识别结果失败: %s", exc)
            return None

    def _save_image(
        self, image: np.ndarray, record_id: str, bbox: Tuple[float, float, float, float]
    ) -> Optional[str]:
        """保存图像并在其上绘制检测框。

        Returns:
            文件名; 失败时返回 None
        """
        try:
            # 绘制检测框
            img_copy = image.copy()
            x1, y1, x2, y2 = [int(v) for v in bbox]
            x1 = max(0, x1)
            y1 = max(0, y1)
            x2 = min(img_copy.shape[1], x2)
            y2 = min(img_copy.shape[0], y2)

            cv2.rectangle(img_copy, (x1, y1), (x2, y2), (0, 255, 0), 2)

            # 生成文件名 (包含时间戳)
            now = datetime.now()
            filename = now.strftime(f"%Y%m%d_%H%M%S_{record_id}.jpg")
            filepath = self.image_dir / filename

            # 保存
            cv2.imwrite(str(filepath), img_copy)
            logger.debug("已保存识别图像: %s", filename)
            return filename
        except Exception as exc:
            logger.warning("保存图像失败 (%s): %s", record_id, exc)
            return None

    def _append_record(self, record: DetectionRecord) -> None:
        """追加记录到 JSONL 文件。"""
        try:
            with open(self.records_file, "a", encoding="utf-8") as f:
                json.dump(record.to_dict(), f, ensure_ascii=False)
                f.write("\n")
            logger.debug("已记录: %s", record.record_id)
        except Exception as exc:
            logger.error("追加记录失败: %s", exc)

    def _update_index(self, record: DetectionRecord) -> None:
        """更新索引文件。"""
        try:
            with self._lock:
                # 按类别统计
                if "labels" not in self._index_data:
                    self._index_data["labels"] = {}
                label_stats = self._index_data["labels"].setdefault(record.label, {})
                label_stats["count"] = label_stats.get("count", 0) + 1
                label_stats["last_record_id"] = record.record_id
                label_stats["last_confidence"] = record.confidence

                # 总统计
                self._index_data["total_records"] = self._record_count
                self._index_data["last_timestamp"] = record.timestamp

            # 定期写入文件 (每 5 条记录 或最后一条)
            if self._record_count % 5 == 0 or self._record_count <= 3:
                with open(self.index_file, "w", encoding="utf-8") as f:
                    json.dump(self._index_data, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.debug("更新索引失败: %s", exc)

    def query_by_label(self, label: str, limit: int = 100) -> List[Dict[str, Any]]:
        """按类别查询最近的记录。"""
        results = []
        try:
            if not self.records_file.exists():
                return []

            with open(self.records_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if record.get("label", "").lower() == label.lower():
                        results.append(record)

            # 返回最近的 N 条
            return results[-limit:] if limit > 0 else results
        except Exception as exc:
            logger.error("查询记录失败: %s", exc)
            return []

    def query_by_time_range(
        self, start_time: str, end_time: str
    ) -> List[Dict[str, Any]]:
        """按时间范围查询记录。

        Args:
            start_time: ISO 8601 格式的开始时间
            end_time: ISO 8601 格式的结束时间

        Returns:
            在该时间范围内的记录列表
        """
        results = []
        try:
            if not self.records_file.exists():
                return []

            with open(self.records_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    ts = record.get("timestamp", "")
                    if start_time <= ts <= end_time:
                        results.append(record)
            return results
        except Exception as exc:
            logger.error("按时间范围查询失败: %s", exc)
            return []

    def get_stats(self) -> Dict[str, Any]:
        """获取追溯统计信息。"""
        try:
            if self.index_file.exists():
                with open(self.index_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as exc:
            logger.debug("读取索引失败: %s", exc)

        return {
            "total_records": self._record_count,
            "created_at": self._index_data.get("created_at", ""),
        }

    def get_image_path(self, filename: str) -> Optional[Path]:
        """获取图像文件的完整路径。"""
        if not filename:
            return None
        filepath = self.image_dir / filename
        if filepath.exists():
            return filepath
        return None


__all__ = ["DetectionRecord", "DetectionTracer"]
