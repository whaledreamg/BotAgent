"""动作库: 加载 JSON 动作文件, 建立可检索的索引。

**一个 JSON 文件 = 一个动作。**

动作名就是文件名 (去掉 .json), 例如:

    挥手.json      -> 动作 "挥手"
    准备点.json    -> 动作 "准备点"
    伸左手.json    -> 动作 "伸左手"

这样做的原因:
- 用户在 ActionPlanner 里编排并保存的就是「一个动作」, 天然对应一个文件;
- 文件名可以直接当语音指令的关键字 (「挥个手」-> 挥手.json), 不需要额外映射表;
- 原来按「片段 label」切分动作单元的做法把一个文件拆成了十几个动作
  (sport.json 的 29 段切出 13 个单元), 语义反而碎了。

文件内部的 segments 是这个动作的**轨迹片段**: 首尾相连构成一条连续运动。
相邻片段之间原有的时间间隙是录制时的编排空等, 拆分时已压掉, 所以片段是连续的。

注意: 片段本身**不落盘起始姿态** —— 每次播放都从机器人的当前实际位置出发,
避免轨迹把机器人拽到某个固定姿态上去。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from timeline.model import JOINT_COUNT, JointState, Segment, SegmentType, TimelineModel

logger = logging.getLogger("arm.library")


class ActionNotFound(LookupError):
    """请求的动作不存在。"""


@dataclass
class ActionUnit:
    """一个可独立播放的动作 = 一个 JSON 文件。"""

    key: str            # 唯一 key, 如 "挥手" (文件名去掉 .json)
    label: str          # 动作名 (同 key), 供上层显示
    source: str         # 来源文件名, 如 "挥手.json"
    segments: List[Segment] = field(default_factory=list)
    #: 单元在时间轴上的起点 (原文件里的绝对时间)
    origin_time: float = 0.0
    #: 单元时长 (秒)
    duration: float = 0.0
    #: 同名动作的序号 (文件名冲突时递增)
    index: int = 1

    def to_timeline(self, origin_pose: Optional[JointState] = None) -> TimelineModel:
        """转成可播放的时间轴。

        片段时间会平移到从 0 开始 —— 原文件里的绝对时间带有编排时的间隔,
        直接拿来播会让开头有一段空等。
        """
        segs = [
            Segment(
                type=s.type,
                label=s.label,
                start_time=s.start_time - self.origin_time,
                duration=s.duration,
                end_joints=s.end_joints.copy(),
                interpolation=s.interpolation,
                left_hand=list(s.left_hand),
                right_hand=list(s.right_hand),
            )
            for s in self.segments
        ]
        model = TimelineModel(segs)
        if origin_pose is not None:
            model.set_origin_pose(origin_pose.copy())
        return model

    def summary(self) -> Dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "source": self.source,
            "index": self.index,
            "duration": round(self.duration, 3),
            "segments": len(self.segments),
        }


def _parse_segment(raw: Dict) -> Segment:
    """从 JSON 的一段构造 Segment。

    与 ActionPlanner 的 loadActionFile 一致:
    - 起始姿态不参与加载 (第一段的起点取自当前实测角)
    - 老文件没有 type 字段时整体按机械臂片段读入
    """
    seg = Segment()

    type_str = raw.get("type", "")
    if type_str == SegmentType.HAND:
        seg.type = SegmentType.HAND
    else:
        seg.type = SegmentType.ARM

    seg.label = str(raw.get("label", ""))
    seg.start_time = float(raw.get("startTime", 0.0))
    seg.duration = float(raw.get("duration", 1.0))

    interp = str(raw.get("interpolation", "") or "")
    seg.interpolation = interp if interp else "cubic"

    if seg.is_arm:
        end_joints = raw.get("endJoints") or {}
        left_end = end_joints.get("leftArm") or []
        right_end = end_joints.get("rightArm") or []
        left = [0.0] * JOINT_COUNT
        right = [0.0] * JOINT_COUNT
        for j in range(JOINT_COUNT):
            if j < len(left_end):
                left[j] = float(left_end[j])
            if j < len(right_end):
                right[j] = float(right_end[j])
        seg.end_joints = JointState(left=left, right=right)

    # 手位: 两类片段都读
    hand = raw.get("hand") or {}
    lh = hand.get("leftHand") or []
    rh = hand.get("rightHand") or []
    seg.left_hand = [
        max(0, min(1000, int(lh[d]))) if d < len(lh) else 0 for d in range(6)
    ]
    seg.right_hand = [
        max(0, min(1000, int(rh[d]))) if d < len(rh) else 0 for d in range(6)
    ]

    return seg


class ActionLibrary:
    """动作库: 一个 JSON 文件一个动作, 按文件名索引。"""

    def __init__(
        self,
        action_dir: Path,
        exclude_files: Optional[List[str]] = None,
    ) -> None:
        self.action_dir = Path(action_dir)
        self._units: Dict[str, ActionUnit] = {}
        self._files: List[str] = []
        self._labels: List[str] = []
        #: 要跳过的文件名 (旧版「一文件装多动作」的大文件, 只是拆分素材)
        self._excluded = {str(x) for x in (exclude_files or [])}

    # ------------------------------------------------------------ 加载

    def load(self) -> int:
        """加载动作目录下所有 JSON。返回动作数量。"""
        self._units.clear()
        self._files.clear()
        self._labels.clear()

        if not self.action_dir.is_dir():
            logger.warning("[动作库] 目录不存在: %s", self.action_dir)
            return 0

        for path in sorted(self.action_dir.glob("*.json")):
            if path.name in self._excluded:
                logger.info("[动作库] 跳过 %s (已排除)", path.name)
                continue
            action = self._load_file(path)
            if action is None:
                continue
            self._files.append(path.name)
            self._labels.append(action.key)

        logger.info(
            "[动作库] 共 %d 个文件 / %d 个动作: %s",
            len(self._files),
            len(self._units),
            ", ".join(self._labels),
        )
        return len(self._units)

    def _load_file(self, path: Path) -> Optional[ActionUnit]:
        """加载一个动作文件。返回 None 表示这个文件不是有效动作。"""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("[动作库] 读取 %s 失败: %s", path.name, exc)
            return None

        raw_segments = data.get("segments") or []
        if not raw_segments:
            logger.warning("[动作库] %s 没有片段, 跳过", path.name)
            return None

        # 动作名 = 文件名 (去掉 .json)
        name = path.stem.strip()
        if not name:
            logger.warning("[动作库] %s 文件名为空, 跳过", path.name)
            return None

        segments = [_parse_segment(raw) for raw in raw_segments]
        # 按时间排序: 原文件的 segments 不保证有序 (实测 sport.json 29 段里有 3 段乱序)
        segments.sort(key=lambda s: s.start_time)

        key = name
        index = 1
        # 文件名冲突 (理论上不会, 同一目录文件名唯一) 时加后缀兜底
        if key in self._units:
            index = len([k for k in self._units if k.startswith(name)]) + 1
            key = f"{name}#{index}"

        origin_time = segments[0].start_time
        end_time = max(s.end_time for s in segments)
        action = ActionUnit(
            key=key,
            label=name,
            source=path.name,
            segments=segments,
            origin_time=origin_time,
            duration=end_time - origin_time,
            index=index,
        )
        self._units[key] = action
        logger.info(
            "[动作库] %s -> 动作 %r (%d 段, %.2fs)",
            path.name,
            key,
            len(segments),
            action.duration,
        )
        return action

    # ------------------------------------------------------------ 检索

    @property
    def count(self) -> int:
        return len(self._units)

    @property
    def files(self) -> List[str]:
        return list(self._files)

    def list_actions(self) -> List[Dict[str, object]]:
        """动作清单 (按动作名排序)。"""
        items = [unit.summary() for unit in self._units.values()]
        items.sort(key=lambda item: str(item["key"]))
        return items

    def labels(self) -> List[str]:
        """所有动作名。"""
        return sorted(self._units.keys())

    def get(self, key: str) -> ActionUnit:
        """按动作名取动作。

        支持:
        - 精确匹配 ("挥手")
        - 忽略大小写与空白 ("挥手 " / " 挥手")
        - 带 .json 后缀 ("挥手.json")
        """
        key = (key or "").strip()
        if not key:
            raise ActionNotFound("动作名为空")

        if key in self._units:
            return self._units[key]

        # 允许带 .json 后缀
        if key.endswith(".json"):
            stem = key[:-5].strip()
            if stem in self._units:
                return self._units[stem]

        # 归一化匹配: 去空格 + 忽略大小写
        normalized = self._normalize(key)
        for name, unit in self._units.items():
            if self._normalize(name) == normalized:
                return unit

        raise ActionNotFound(f"未找到动作: {key}")

    def find(self, key: str) -> Optional[ActionUnit]:
        try:
            return self.get(key)
        except ActionNotFound:
            return None

    @staticmethod
    def _normalize(name: str) -> str:
        return re.sub(r"\s+", "", name).lower()
