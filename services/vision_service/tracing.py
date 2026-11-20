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

    目录结构 (每次拍照一个文件夹):
        traces/
            ├── 20261117_110830_123456/       # 一次拍照 = 一个文件夹
            │   ├── mouse_01.jpg              # 每个物体一张图, 各画各的框
            │   ├── cup_02.jpg
            │   └── frame.jpg                 # 整帧原图 (可选)
            ├── records.jsonl                 # 结构化记录
            └── index.json                    # 索引和元数据

    线程安全。
    """

    def __init__(self, trace_root: str | Path = "traces") -> None:
        self.trace_root = Path(trace_root)
        self.sessions_dir = self.trace_root / "sessions"
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
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            logger.info("追溯系统已初始化: %s", self.trace_root)
        except Exception as exc:
            logger.error("初始化追溯系统失败: %s", exc)

    def new_session(self) -> str:
        """开一个新的拍照会话 (一个文件夹), 返回文件夹名。

        每次拍照调一次 —— 这一拍的图片都放进这个文件夹。
        """
        now = datetime.now()
        name = now.strftime("%Y%m%d_%H%M%S_%f")[:-3]
        try:
            (self.sessions_dir / name).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("创建追溯文件夹失败 (%s): %s", name, exc)
        return name

    def record(
        self,
        label: str,
        confidence: float,
        bbox: Tuple[float, float, float, float],
        image: np.ndarray,
        pose: Optional[Dict[str, Any]] = None,
        frame: str = "camera",
        extra: Optional[Dict[str, Any]] = None,
        session: Optional[str] = None,
        index: int = 0,
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
            session: 所属的拍照会话 (文件夹名); 不给就自己开一个
            index: 该物体在这次拍照里的序号 (决定文件名, 避免同名覆盖)

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

            if not session:
                session = self.new_session()

            # 保存图像 —— 每个物体一张, 各画各的框
            image_filename = None
            if image is not None and image.size > 0:
                image_filename = self._save_image(
                    image, session, label, index, bbox, confidence, pose,
                )

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
        self,
        image: np.ndarray,
        session: str,
        label: str,
        index: int,
        bbox: Tuple[float, float, float, float],
        confidence: float,
        pose: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """保存图像并在其上绘制该物体的检测框。

        文件名: ``<类别>_<序号>.jpg`` (放在本次拍照的文件夹里)

        Returns:
            相对 trace_root 的路径; 失败时返回 None
        """
        try:
            session_dir = self.sessions_dir / session
            session_dir.mkdir(parents=True, exist_ok=True)

            # 文件名: 类别 + 序号, 同一类别出现多次也不会互相覆盖
            safe_label = "".join(
                ch for ch in str(label or "obj") if ch.isalnum() or ch in "-_"
            ) or "obj"
            filename = f"{safe_label}_{index:02d}.jpg"

            img_copy = image.copy()
            self._draw_box(img_copy, bbox, label, confidence, pose)

            filepath = session_dir / filename
            cv2.imwrite(str(filepath), img_copy)
            with self._lock:
                self._image_count += 1
            logger.debug("已保存识别图像: %s/%s", session, filename)
            return str(Path(session) / filename)
        except Exception as exc:
            logger.warning("保存图像失败 (%s): %s", label, exc)
            return None

    #: 不同类别配不同颜色, 免得满屏都是绿框看不出区别
    _BOX_COLORS = (
        (0, 255, 0),    # 绿
        (0, 165, 255),  # 橙
        (255, 128, 0),  # 蓝
        (0, 255, 255),  # 黄
        (255, 0, 255),  # 紫
        (0, 255, 128),  # 青
        (255, 255, 0),  # 蓝绿
    )

    def _draw_box(
        self,
        img: np.ndarray,
        bbox: Tuple[float, float, float, float],
        label: str,
        confidence: float,
        pose: Optional[Dict[str, Any]] = None,
    ) -> None:
        """在图上画一个检测框 + 标签。"""
        try:
            x1, y1, x2, y2 = [int(v) for v in bbox]
            h, w = img.shape[:2]
            x1 = max(0, min(w - 1, x1))
            y1 = max(0, min(h - 1, y1))
            x2 = max(0, min(w - 1, x2))
            y2 = max(0, min(h - 1, y2))
            if x2 <= x1 or y2 <= y1:
                return

            # 按类别取色: 同一个类别同一个颜色
            color = self._BOX_COLORS[hash(str(label)) % len(self._BOX_COLORS)]

            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)

            # ---- 标签文字 ----
            #
            # 画在框上方; 顶到画面边缘就画到框里面, 别把字画到画面外。
            text = str(label)
            try:
                text = f"{text} {float(confidence):.0%}"
            except (TypeError, ValueError):
                pass
            if pose:
                try:
                    z = pose.get("z")
                    if z is not None:
                        text += f" {float(z):.2f}m"
                except (TypeError, ValueError, AttributeError):
                    pass

            font = cv2.FONT_HERSHEY_SIMPLEX
            scale = 0.55
            thickness = 1
            (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)

            pad = 3
            box_h = th + baseline + pad * 2
            ty1 = y1 - box_h if y1 - box_h >= 0 else y1
            ty2 = ty1 + box_h

            # 标签底色 (半透明, 文字才看得清)
            overlay = img.copy()
            cv2.rectangle(overlay, (x1, ty1), (x1 + tw + pad * 2, ty2), color, -1)
            cv2.addWeighted(overlay, 0.65, img, 0.35, 0, img)

            cv2.putText(
                img, text, (x1 + pad, ty2 - baseline - pad),
                font, scale, (0, 0, 0), thickness + 1, cv2.LINE_AA,
            )
            cv2.putText(
                img, text, (x1 + pad, ty2 - baseline - pad),
                font, scale, (255, 255, 255), thickness, cv2.LINE_AA,
            )
        except Exception as exc:
            logger.debug("画框失败: %s", exc)

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
        """获取图像文件的完整路径。

        ``filename`` 的格式取决于数据新旧:
        - 新: ``20261117_110830_123/mouse_01.jpg`` (在 sessions/ 下)
        - 旧: ``20260717_110830_123.jpg`` (在 images/ 下)
        """
        if not filename:
            return None
        # 新格式: sessions/<会话>/<类别>_<序号>.jpg
        filepath = self.sessions_dir / filename
        if filepath.exists():
            return filepath
        # 旧格式: images/<文件名>.jpg
        filepath = self.trace_root / "images" / filename
        if filepath.exists():
            return filepath
        # 兜底: 直接在 trace_root 下
        filepath = self.trace_root / filename
        if filepath.exists():
            return filepath
        return None


__all__ = ["DetectionRecord", "DetectionTracer"]
