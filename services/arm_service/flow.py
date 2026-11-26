"""流程定义 —— 一条流程由哪些**片段**、按什么顺序组成。

流程文件在 ``configs/flows/*.yaml``, 每个文件一条流程, 文件名即流程名。

**为什么要这一层**: 原来抓取流程是写死在 ``vision_grasp.py`` 里的固定
5 步 —— 加一步、调顺序、换动作名都得改代码。搬到这里之后, 改流程
只改 YAML。

**片段语义**: 流程里的每一步都直接对应时间轴上的一段, 并带自己的时长。
四种片段类型:

- ``arm``   —— 关节片段。终点可以引用动作库 (``action``)、写死关节角
  (``pose``)、或引用一个具名点 (``point``)。
- ``hand``  —— 手部片段。手位来自动作库 / 显式手位 / 手势名。
- ``detect``—— 识别片段。产出识别结果, 期间机械臂保持不动。
- ``target``—— **具名点声明**。登记一个点 (值来自识别结果 / 另一个
  具名点 + 偏移 / 显式坐标), 供别的片段按名字引用。

扩展边界 (哪些改动不用碰代码):

- 片段**内容**与**参数** -> 只改 YAML
- 片段**类型** -> 要改 Python (见 :data:`STEP_TYPES`)

流程文件里出现**未知的片段类型**会直接抛错 —— 不能静默跳过, 那样
流程会「少做一步」而没人知道。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("arm.flow")

#: 已知的片段类型
#:
#: 执行器 (agent 侧) 按这些类型分派。**加新类型要改 Python** ——
#: 这是流程通用化的边界, 见 configs/flows/README.md。
STEP_TYPES = ("arm", "hand", "detect", "target")

#: ``target`` 片段的取值来源
#:
#: ``detect`` —— 取识别结果 (必须排在 detect 片段之后)
TARGET_SOURCES = ("detect",)

#: ``target`` 片段的 ``distance`` 取值
#:
#: ``approach`` —— 偏移的高度取流程级的 ``approach_distance``
#: (「上方点的高度」是流程的一个设置, 调用参数可以覆盖它)
POINT_DISTANCES = ("approach",)

#: ``hand`` 片段的夹爪方向
#:
#: 纯 ``pose`` 手位时必须给方向 —— 6 个数字看不出是合还是开。
HAND_DIRECTIONS = ("close", "open")

#: 默认流程名 (缺省用哪个)
DEFAULT_FLOW = "grasp"

#: 流程目录里**不是流程**的文件名 (跳过)
#:
#: ``routing.yaml`` 是「物品名 -> 流程名」的**分流表** —— 它跟流程
#: 文件放在一起 (一眼能看到有哪些流程、怎么分流), 但它本身不是流程。
#:
#: 不排除它的话, ``load_flows()`` 扫目录时会把它当流程解析, 然后因为
#: 「steps 必须是非空列表」而报错 —— 虽��被跳过, 但日志里天天刷错误。
NON_FLOW_FILES = ("routing.yaml",)

#: 关节数
NDOF = 7

#: 灵巧手自由度
HAND_DOF = 6


class FlowError(ValueError):
    """流程定义有问题 (格式错、类型未知、必填字段缺失)。"""


@dataclass
class FlowPoint:
    """``target`` 片段里声明的一个具名点。

    Attributes:
        id:      点名 (在流程内唯一) —— 别的片段用 ``point: <id>`` 引用
        label:   播报用的说法
        source:  取值来源 —— ``detect`` (识别结果) 或空 (用 base/pose)
        base:    基准具名点 id (基于它再偏移)
        offset:  相对基准点的偏移 (米), 按轴分
        distance: ``approach`` —— 偏移的高度取流程级的 ``approach_distance``
                 (这样「上方点的高度」是流程的一个设置, 调用参数可以覆盖)
        pose:    显式坐标 {"x":..,"y":..,"z":..}
    """

    id: str
    label: str = ""
    source: str = ""
    base: str = ""
    offset: Dict[str, float] = field(default_factory=dict)
    distance: str = ""
    pose: Optional[Dict[str, float]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id, "label": self.label}
        if self.source:
            out["source"] = self.source
        if self.base:
            out["base"] = self.base
        if self.offset:
            out["offset"] = dict(self.offset)
        if self.distance:
            out["distance"] = self.distance
        if self.pose is not None:
            out["pose"] = dict(self.pose)
        return out


@dataclass
class FlowStep:
    """流程里的一个片段。

    Attributes:
        id:      片段标识 (同一流程内唯一), 追溯记录里用它当 ``name``
        type:    片段类型, 必须是 :data:`STEP_TYPES` 里的
        label:   播报用的说法 (如「上方点」)
        params:  类型相关的参数
        on_failure: 这一步失败时怎么办 —— ``recover`` (先收回安全点再
            报失败) 或 ``abort`` (直接报失败)。留空 = 按类型取默认值。
    """

    id: str
    type: str
    label: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    on_failure: str = ""

    # ---- 参数访问 ----

    @property
    def action(self) -> str:
        """动作库里的动作名 (``arm``/``hand`` 用)。"""
        return str(self.params.get("action") or "")

    @property
    def pose(self) -> Any:
        """显式位姿。

        - ``arm``: ``{"left": [7 个], "right": [7 个]}`` —— 关节角
        - ``hand``: 6 个 0~1000 的手位
        - ``target``: ``{"x":..,"y":..,"z":..}`` —— 笛卡尔坐标
        """
        return self.params.get("pose")

    @property
    def preset(self) -> str:
        """手势名 (``hand`` 用), 如「一把抓」。"""
        return str(self.params.get("preset") or "")

    @property
    def direction(self) -> str:
        """夹爪方向 ``close``/``open`` (``hand`` 片段)。"""
        return str(self.params.get("direction") or "")

    @property
    def point(self) -> str:
        """引用的具名点 id (``arm`` 用)。"""
        return str(self.params.get("point") or "")

    @property
    def source(self) -> str:
        """取值来源 (``target`` 用) —— ``detect``。"""
        return str(self.params.get("source") or "")

    @property
    def base(self) -> str:
        """基准具名点 id (``target`` 用)。"""
        return str(self.params.get("base") or "")

    @property
    def offset(self) -> Dict[str, float]:
        """偏移 (米), 按轴分。"""
        raw = self.params.get("offset") or {}
        if not isinstance(raw, dict):
            return {"x": 0.0, "y": 0.0, "z": 0.0}
        out = {}
        for axis in ("x", "y", "z"):
            try:
                out[axis] = float(raw.get(axis, 0.0) or 0.0)
            except (TypeError, ValueError):
                out[axis] = 0.0
        return out

    @property
    def duration(self) -> Optional[float]:
        """片段时长 (秒)。没给 = 用默认值。"""
        value = self.params.get("duration")
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def orientation(self) -> Dict[str, Dict[str, float]]:
        """抓取姿态 (各臂的 a/b/c 欧拉角, rad)。

        **只对 ``detect`` 片段有意义** —— 它规定「拍照时手是什么姿态」,
        也就等于规定了**从那个姿态出发去抓**时的抓取姿态。

        为什么要放在流程里而不是只放 configs/arm.yaml:
        拍照姿态必须和抓取姿态**一致** —— 否则机械臂得先从拍照姿态转到
        另一个姿态, 而那个姿态在目标点很可能根本不可达 (历史上就是这么
        报 ``unreachable`` 的)。

        格式::

            orientation:
              right: {a: 1.6683, b: 0.6821, c: 1.9317}
              left:  {a: -1.6900, b: 0.5548, c: -1.9033}
        """
        raw = self.params.get("orientation") or {}
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, Dict[str, float]] = {}
        for side, abc in raw.items():
            if not isinstance(abc, dict):
                continue
            try:
                out[str(side).strip().lower()] = {
                    "a": float(abc["a"]),
                    "b": float(abc["b"]),
                    "c": float(abc["c"]),
                }
            except (KeyError, TypeError, ValueError):
                continue
        return out

    @property
    def points(self) -> List[FlowPoint]:
        """``target`` 片段声明的具名点列表。"""
        raw = self.params.get("points") or []
        out: List[FlowPoint] = []
        for item in raw:
            if isinstance(item, FlowPoint):
                out.append(item)
                continue
            if not isinstance(item, dict):
                continue
            offset = item.get("offset") or {}
            if not isinstance(offset, dict):
                offset = {}
            pose = item.get("pose")
            if not isinstance(pose, dict):
                pose = None
            out.append(
                FlowPoint(
                    id=str(item.get("id") or "").strip(),
                    label=str(item.get("label") or "").strip(),
                    source=str(item.get("source") or "").strip(),
                    base=str(item.get("base") or "").strip(),
                    offset={
                        axis: _num(offset.get(axis, 0.0)) for axis in ("x", "y", "z")
                    },
                    distance=str(item.get("distance") or "").strip().lower(),
                    pose=(
                        {axis: _num(pose.get(axis, 0.0)) for axis in ("x", "y", "z")}
                        if pose is not None else None
                    ),
                )
            )
        return out

    def to_dict(self) -> Dict[str, Any]:
        """转成可以 JSON 化的字典 (给 HTTP 接口用)。"""
        out: Dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "label": self.label,
        }
        for key, value in self.params.items():
            if key == "points":
                out["points"] = [
                    p.to_dict() if isinstance(p, FlowPoint) else p
                    for p in (value or [])
                ]
            else:
                out[key] = value
        return out


@dataclass
class FlowDef:
    """一条完整的流程。"""

    name: str
    description: str = ""
    steps: List[FlowStep] = field(default_factory=list)
    #: 这个流程从哪来的 —— 文件路径, 或 "config"(由旧配置合成)
    source: str = ""
    #: 某一步失败时的收尾动作 (先播它, 再报失败)
    #:
    #: 留空 = 不做收尾。手停在半空是不行的 —— 所以默认给「安全点」。
    recovery_action: str = ""
    recovery_label: str = ""

    def step(self, step_id: str) -> Optional[FlowStep]:
        """按 id 找一步。找不到返回 None。"""
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    @property
    def step_ids(self) -> List[str]:
        return [s.id for s in self.steps]

    def named_points(self) -> Dict[str, FlowPoint]:
        """流程里声明的所有具名点, 按点名索引。

        具名点可以用 ``target`` 片段的 ``points`` 列表声明, 也可以用
        一个 ``target`` 片段声明一个点 (步骤 id 就是点名)。
        """
        out: Dict[str, FlowPoint] = {}
        for step in self.steps:
            if step.type != "target":
                continue
            points = step.points
            if points:
                for point in points:
                    out[point.id] = point
                continue
            # 单点写法: 步骤 id 就是点名
            out[step.id] = FlowPoint(
                id=step.id,
                label=step.label,
                source=step.source,
                base=step.base,
                offset=step.offset,
                distance=str(step.params.get("distance") or ""),
                pose=step.pose if isinstance(step.pose, dict) else None,
            )
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "steps": [s.to_dict() for s in self.steps],
        }


# ---------------------------------------------------------------- 加载

def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_offset(raw: Any, where: str) -> Dict[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise FlowError(f"{where} 的 offset 必须是字典")
    return {axis: _num(raw.get(axis, 0.0)) for axis in ("x", "y", "z")}


def _parse_joint_pose(raw: Any, where: str) -> Dict[str, List[float]]:
    """解析 ``arm`` 片段的显式关节角。

    必须是 ``{"left": [7 个], "right": [7 个]}`` —— 只给一条臂也行,
    另一条臂保持不动。
    """
    if not isinstance(raw, dict):
        raise FlowError(f"{where} 的 pose 必须是字典 (left/right 的关节角)")
    out: Dict[str, List[float]] = {}
    for side in ("left", "right"):
        values = raw.get(side)
        if values is None:
            continue
        if not isinstance(values, (list, tuple)):
            raise FlowError(
                f"{where} 的 pose.{side} 必须是 {NDOF} 个关节角的列表"
            )
        if len(values) != NDOF:
            raise FlowError(
                f"{where} 的 pose.{side} 必须正好 {NDOF} 个关节角 "
                f"(给了 {len(values)} 个)"
            )
        out[side] = [_num(v) for v in values]
    if not out:
        raise FlowError(f"{where} 的 pose 必须至少给 left 或 right")
    return out


def _parse_hand_pose(raw: Any, where: str) -> List[int]:
    """解析 ``hand`` 片段的显式手位 (6 个 0~1000)。"""
    if not isinstance(raw, (list, tuple)):
        raise FlowError(f"{where} 的 pose 必须是 {HAND_DOF} 个数的列表")
    if len(raw) != HAND_DOF:
        raise FlowError(
            f"{where} 的 pose 必须正好 {HAND_DOF} 个数 (给了 {len(raw)} 个)"
        )
    out: List[int] = []
    for value in raw:
        number = _num(value, -1.0)
        if number < 0 or number > 1000:
            raise FlowError(
                f"{where} 的 pose 值必须在 0~1000 之间 (得到 {value!r})"
            )
        out.append(int(round(number)))
    return out


def _parse_xyz(raw: Any, where: str) -> Dict[str, float]:
    if not isinstance(raw, dict):
        raise FlowError(f"{where} 的 pose 必须是含 x/y/z 的字典")
    return {axis: _num(raw.get(axis, 0.0)) for axis in ("x", "y", "z")}


def _parse_orientation(raw: Any, where: str) -> Dict[str, Dict[str, float]]:
    """解析 ``detect`` 片段的拍照姿态。

    格式::

        orientation:
          right: {a: 1.6683, b: 0.6821, c: 1.9317}
          left:  {a: -1.6900, b: 0.5548, c: -1.9033}

    **必须是字典** —— 写成 ``right: {1.6683, 0.6821, 1.9317}`` (YAML 的
    集合语法) 会被 YAML 解析成 ``{1.6683: null, ...}``, 姿态会被静默
    丢掉。所以这里显式报错, 而不是放行一个空姿态。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise FlowError(
            f"{where} 的 orientation 必须是字典 (按 left/right 分, "
            f"每条臂给 a/b/c 三个欧拉角)"
        )
    out: Dict[str, Dict[str, float]] = {}
    for side, abc in raw.items():
        side_name = str(side).strip().lower()
        if side_name not in ("left", "right"):
            raise FlowError(
                f"{where} 的 orientation 只能有 left / right (得到 {side!r})"
            )
        if not isinstance(abc, dict):
            raise FlowError(
                f"{where} 的 orientation.{side_name} 必须是含 a/b/c 的字典"
            )
        try:
            out[side_name] = {
                "a": float(abc["a"]),
                "b": float(abc["b"]),
                "c": float(abc["c"]),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise FlowError(
                f"{where} 的 orientation.{side_name} 必须给出 a/b/c 三个欧拉角"
            ) from exc
    return out


def parse_flow(raw: Dict[str, Any], *, source: str = "") -> FlowDef:
    """把一个 dict 解析成 :class:`FlowDef`, 顺便校验。

    Raises:
        FlowError: 格式不对 / 类型未知 / 必填字段缺失 / 引用不合法
    """
    if not isinstance(raw, dict):
        raise FlowError(f"流程定义必须是字典 (来自 {source or '?'})")

    name = str(raw.get("name") or "").strip()
    if not name:
        raise FlowError(f"流程缺少 name (来自 {source or '?'})")

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise FlowError(f"流程 {name!r} 的 steps 必须是非空列表")

    steps: List[FlowStep] = []
    seen_ids: set = set()
    for index, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            raise FlowError(f"流程 {name!r} 第 {index + 1} 步不是字典")

        step_id = str(item.get("id") or "").strip()
        if not step_id:
            raise FlowError(f"流程 {name!r} 第 {index + 1} 步缺少 id")
        if step_id in seen_ids:
            raise FlowError(f"流程 {name!r} 里有重复的步骤 id {step_id!r}")
        seen_ids.add(step_id)

        step_type = str(item.get("type") or "").strip().lower()
        if not step_type:
            raise FlowError(f"流程 {name!r} 的步骤 {step_id!r} 缺少 type")
        if step_type not in STEP_TYPES:
            raise FlowError(
                f"流程 {name!r} 的步骤 {step_id!r} 用了未知的 type "
                f"{step_type!r} (已知: {', '.join(STEP_TYPES)})"
            )

        where = f"流程 {name!r} 的步骤 {step_id!r}"
        params: Dict[str, Any] = {}

        if step_type == "arm":
            _parse_arm_params(where, item, params)
        elif step_type == "hand":
            _parse_hand_params(where, item, params)
        elif step_type == "detect":
            duration = item.get("duration")
            if duration not in (None, ""):
                params["duration"] = _num(duration, 0.0)
            orientation = _parse_orientation(item.get("orientation"), where)
            if orientation:
                params["orientation"] = orientation
        elif step_type == "target":
            _parse_target_params(where, item, params)

        on_failure = str(item.get("on_failure") or "").strip().lower()
        if on_failure and on_failure not in ("recover", "abort"):
            raise FlowError(
                f"{where} 的 on_failure 只能是 recover 或 abort (得到 {on_failure!r})"
            )

        steps.append(
            FlowStep(
                id=step_id,
                type=step_type,
                label=str(item.get("label") or "").strip(),
                params=params,
                on_failure=on_failure,
            )
        )

    _validate_references(name, steps)
    return FlowDef(
        name=name,
        description=str(raw.get("description") or "").strip(),
        steps=steps,
        source=source,
        recovery_action=str(raw.get("recovery_action") or "").strip(),
        recovery_label=str(raw.get("recovery_label") or "").strip(),
    )


def _parse_arm_params(where: str, item: Dict[str, Any],
                      params: Dict[str, Any]) -> None:
    """解析 ``arm`` 片段的参数。

    终点有四种来源, **四选一**: 动作库 / 显式关节角 / 具名点 / 基准点。
    """
    action = str(item.get("action") or "").strip()
    pose = item.get("pose")
    point = str(item.get("point") or "").strip()
    base = str(item.get("base") or "").strip()

    given = [
        name for name, value in (
            ("action", action), ("pose", pose),
            ("point", point), ("base", base),
        ) if value
    ]
    if not given:
        raise FlowError(
            f"{where} 必须给出终点: action (动作库) / pose (显式关节角) / "
            f"point (具名点) / base (基准点+offset) 四选一"
        )
    if len(given) > 1:
        raise FlowError(
            f"{where} 的终点只能给一个, 但给了 {', '.join(given)}"
        )

    if action:
        params["action"] = action
    elif pose is not None:
        params["pose"] = _parse_joint_pose(pose, where)
    elif point:
        params["point"] = point
        offset = _parse_offset(item.get("offset"), where)
        if offset:
            params["offset"] = offset
    elif base:
        params["base"] = base
        params["offset"] = _parse_offset(item.get("offset"), where)

    duration = item.get("duration")
    if duration not in (None, ""):
        params["duration"] = _num(duration, 0.0)


def _parse_hand_params(where: str, item: Dict[str, Any],
                       params: Dict[str, Any]) -> None:
    """解析 ``hand`` 片段的参数。

    手位有三种来源: 动作库 / 显式手位 / 手势名。至少给一个。
    纯 ``pose`` 时必须给 ``direction`` —— 6 个数字看不出是合还是开。
    """
    action = str(item.get("action") or "").strip()
    pose = item.get("pose")
    preset = str(item.get("preset") or "").strip()
    direction = str(item.get("direction") or "").strip().lower()

    given = [
        name for name, value in (
            ("action", action), ("pose", pose), ("preset", preset),
        ) if value
    ]
    if not given:
        raise FlowError(
            f"{where} 必须给出手位: action (动作库) / pose (显式手位) / "
            f"preset (手势名) 至少一个"
        )
    if len(given) > 1:
        raise FlowError(
            f"{where} 的手位只能给一个, 但给了 {', '.join(given)}"
        )

    if action:
        params["action"] = action
    elif pose is not None:
        params["pose"] = _parse_hand_pose(pose, where)
    elif preset:
        params["preset"] = preset

    if direction:
        if direction not in HAND_DIRECTIONS:
            raise FlowError(
                f"{where} 的 direction 只能是 close 或 open (得到 {direction!r})"
            )
        params["direction"] = direction
    elif pose is not None and not action:
        # 纯 pose: 必须知道是合还是开
        raise FlowError(
            f"{where} 用了显式 pose, 必须给 direction (close 或 open) —— "
            f"6 个数字看不出是合还是开"
        )

    duration = item.get("duration")
    if duration not in (None, ""):
        params["duration"] = _num(duration, 0.0)


def _parse_target_params(where: str, item: Dict[str, Any],
                         params: Dict[str, Any]) -> None:
    """解析 ``target`` 片段的参数。

    三种取值方式三选一: 识别结果 / 基准点+偏移 / 显式坐标。
    """
    source = str(item.get("source") or "").strip().lower()
    base = str(item.get("base") or "").strip()
    pose = item.get("pose")
    points_raw = item.get("points")

    given = [
        name for name, value in (
            ("source", source), ("base", base), ("pose", pose),
        ) if value
    ]
    if points_raw is not None:
        given.append("points")
    if not given:
        raise FlowError(
            f"{where} 必须给出取值方式: source: detect / base+offset / "
            f"pose 三选一"
        )
    if len(given) > 1:
        raise FlowError(
            f"{where} 的取值方式只能给一个, 但给了 {', '.join(given)}"
        )

    if source:
        if source not in TARGET_SOURCES:
            raise FlowError(
                f"{where} 的 source 只能是 {', '.join(TARGET_SOURCES)} "
                f"(得到 {source!r})"
            )
        params["source"] = source
    elif base:
        params["base"] = base
        params["offset"] = _parse_offset(item.get("offset"), where)
    elif pose is not None:
        params["pose"] = _parse_xyz(pose, where)

    distance = str(item.get("distance") or "").strip().lower()
    if distance:
        if distance not in POINT_DISTANCES:
            raise FlowError(
                f"{where} 的 distance 只能是 {', '.join(POINT_DISTANCES)} "
                f"(得到 {distance!r})"
            )
        params["distance"] = distance

    if points_raw is not None:
        if not isinstance(points_raw, list) or not points_raw:
            raise FlowError(f"{where} 的 points 必须是非空列表")
        params["points"] = _parse_points(where, points_raw)


def _parse_points(where: str, raw_points: Sequence[Any]) -> List[FlowPoint]:
    """解析 ``target`` 片段的 points 列表。"""
    out: List[FlowPoint] = []
    seen: set = set()
    for index, item in enumerate(raw_points):
        if not isinstance(item, dict):
            raise FlowError(f"{where} 的 points[{index}] 必须是字典")
        point_id = str(item.get("id") or "").strip()
        if not point_id:
            raise FlowError(f"{where} 的 points[{index}] 缺少 id")
        if point_id in seen:
            raise FlowError(f"{where} 里有重复的点名 {point_id!r}")
        seen.add(point_id)

        source = str(item.get("source") or "").strip().lower()
        base = str(item.get("base") or "").strip()
        pose = item.get("pose")
        distance = str(item.get("distance") or "").strip().lower()
        given = [
            name for name, value in (
                ("source", source), ("base", base), ("pose", pose),
            ) if value
        ]
        if not given:
            raise FlowError(
                f"{where} 的点 {point_id!r} 必须给出取值方式: "
                f"source: detect / base+offset / pose 三选一"
            )
        if len(given) > 1:
            raise FlowError(
                f"{where} 的点 {point_id!r} 的取值方式只能给一个, "
                f"但给了 {', '.join(given)}"
            )
        if source and source not in TARGET_SOURCES:
            raise FlowError(
                f"{where} 的点 {point_id!r} 的 source 只能是 "
                f"{', '.join(TARGET_SOURCES)} (得到 {source!r})"
            )
        if distance and distance not in POINT_DISTANCES:
            raise FlowError(
                f"{where} 的点 {point_id!r} 的 distance 只能是 "
                f"{', '.join(POINT_DISTANCES)} (得到 {distance!r})"
            )

        out.append(
            FlowPoint(
                id=point_id,
                label=str(item.get("label") or "").strip(),
                source=source,
                base=base,
                offset=_parse_offset(item.get("offset"), f"{where} 的点 {point_id!r}"),
                distance=distance,
                pose=(
                    _parse_xyz(pose, f"{where} 的点 {point_id!r}")
                    if pose is not None else None
                ),
            )
        )
    return out


def _validate_references(name: str, steps: List[FlowStep]) -> None:
    """校验片段之间的引用与顺序。

    三类约束:

    1. **引用必须能解析** —— 不管声明顺序, 引用不存在的点/步骤要报错;
    2. **引用必须指向 target 片段** —— 不能引用一个 arm 片段;
    3. **顺序约束** —— 取识别结果的点必须排在 detect 之后; 基于另一个
       点的点必须排在被引用的点之后 (值要算得出来)。
    """
    # ---- 收集所有具名点 ----
    points: Dict[str, FlowPoint] = {}
    point_order: Dict[str, int] = {}
    detect_positions: List[int] = []

    for index, step in enumerate(steps):
        if step.type == "detect":
            detect_positions.append(index)
            continue
        if step.type != "target":
            continue
        if step.points:
            for point in step.points:
                if point.id in points:
                    raise FlowError(
                        f"流程 {name!r} 里有重复的点名 {point.id!r}"
                    )
                points[point.id] = point
                point_order[point.id] = index
            continue
        # 单点写法: 步骤 id 就是点名
        if step.id in points:
            raise FlowError(f"流程 {name!r} 里有重复的点名 {step.id!r}")
        points[step.id] = FlowPoint(
            id=step.id,
            label=step.label,
            source=step.source,
            base=step.base,
            offset=step.offset,
            pose=step.pose if isinstance(step.pose, dict) else None,
        )
        point_order[step.id] = index

    if not detect_positions:
        # 没有 detect 片段 —— 但**引用校验还是要做**。
        # 「引用了不存在的具名点」跟有没有识别步骤无关。
        last_detect = -1
    else:
        last_detect = detect_positions[-1]

    def _check_point(point_id: str, where: str) -> None:
        if point_id not in points:
            raise FlowError(
                f"流程 {name!r} 的 {where} 引用了不存在的具名点 {point_id!r}"
            )

    for index, step in enumerate(steps):
        if step.type == "arm":
            for key in ("point", "base"):
                ref = str(step.params.get(key) or "").strip()
                if ref:
                    _check_point(ref, f"步骤 {step.id!r} 的 {key}")
            continue
        if step.type != "target":
            continue

        def _check_one(point: FlowPoint, owner: str) -> None:
            if point.source == "detect":
                # 识别之前拿不到值 —— 必须排在 detect 之后
                if index <= last_detect:
                    raise FlowError(
                        f"流程 {name!r} 的 {owner} 取自识别结果, "
                        f"必须排在 detect 片段之后"
                    )
            if point.base:
                _check_point(point.base, f"{owner} 的 base")
                if point_order[point.base] >= index:
                    raise FlowError(
                        f"流程 {name!r} 的 {owner} 基于具名点 "
                        f"{point.base!r}, 必须排在它之后"
                    )

        if step.points:
            for point in step.points:
                _check_one(point, f"点 {point.id!r}")
            continue
        _check_one(
            FlowPoint(
                id=step.id,
                source=step.source,
                base=step.base,
            ),
            f"步骤 {step.id!r}",
        )

    # 循环引用
    for point_id, point in points.items():
        if not point.base:
            continue
        chain = {point_id}
        cursor = point.base
        while cursor:
            if cursor in chain:
                raise FlowError(
                    f"流程 {name!r} 的具名点 {point_id!r} 有循环引用 "
                    f"({cursor})"
                )
            chain.add(cursor)
            nxt = points.get(cursor)
            cursor = nxt.base if nxt else ""


def load_flow_file(path: Path) -> FlowDef:
    """读一个流程文件。"""
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise FlowError(f"流程文件 {path} 解析失败: {exc}") from exc
    if raw is None:
        raise FlowError(f"流程文件 {path} 是空的")
    # 文件名即流程名; yaml 里没写 name 时用文件名兜底
    if isinstance(raw, dict) and not raw.get("name"):
        raw = dict(raw)
        raw["name"] = path.stem
    return parse_flow(raw, source=str(path))


def flows_signature(directory: Path) -> Tuple[Tuple[str, int, int], ...]:
    """流程目录的**指纹** —— 用来判断文件有没有变。

    每个 yaml 记 ``(文件名, mtime, 大小)``。任何一个变了, 指纹就变。

    **为什么用 mtime 而不是只比内容**: 内容比对要读全文件, mtime 只要
    一次 ``stat()`` —— 目录里通常就一两个文件, 这个开销可以忽略, 但
    每次抓取都做一次还是值得避免的。

    **为什么连大小一起比**: 有些文件系统 mtime 精度只到秒, 同一秒内
    改两次会漏掉。加个大小能多一道保险 (极端情况下大小相同且同秒,
    才会漏 —— 那就重启服务)。

    文件被删了/新增了, 指纹也变 (元组长度或内容不同)。

    Returns:
        排序后的元组; 目录不存在返回空元组
    """
    if not directory.is_dir():
        return ()
    out: List[Tuple[str, int, int]] = []
    try:
        for path in sorted(directory.glob("*.yaml")):
            if path.name in NON_FLOW_FILES:
                continue
            try:
                stat = path.stat()
            except OSError:
                # 文件刚被删掉 —— 记个哨兵, 让指纹跟之前不一样
                out.append((path.name, -1, -1))
                continue
            out.append((path.name, int(stat.st_mtime_ns), int(stat.st_size)))
    except OSError:
        return ()
    return tuple(out)


def load_flows(directory: Path) -> Dict[str, FlowDef]:
    """扫描目录, 读出所有流程。

    **一个文件坏了不影响别的** —— 出错的流程跳过并记日志, 但会记下
    错误, 让调用方能报出来。

    ``NON_FLOW_FILES`` 里的文件 (分流表 ``routing.yaml``) 直接跳过 ——
    它不是流程。
    """
    import yaml

    flows: Dict[str, FlowDef] = {}
    if not directory.is_dir():
        logger.info("[流程] 目录不存在, 没有额外流程: %s", directory)
        return flows

    for path in sorted(directory.glob("*.yaml")):
        if path.name in NON_FLOW_FILES:
            continue
        try:
            flow = load_flow_file(path)
        except FlowError as exc:
            logger.error("[流程] %s 加载失败, 跳过: %s", path.name, exc)
            continue
        flows[flow.name] = flow
        logger.info(
            "[流程] 载入 %s (%d 步): %s",
            flow.name, len(flow.steps), " -> ".join(flow.step_ids),
        )
    return flows


# ---------------------------------------------------------------- 兜底合成


def _detect_orientation_params(cfg: Any) -> Dict[str, Any]:
    """兜底流程里 detect 片段的拍照姿态参数。

    取自 ``configs/arm.yaml`` 的 ``grasp.orientation`` —— 那是「准备点」
    的 TCP 姿态, 也就是**拍照姿态 = 抓取姿态**。
    """
    orientation = getattr(cfg, "orientation", None) or {}
    out: Dict[str, Dict[str, float]] = {}
    for side in ("left", "right"):
        abc = orientation.get(side)
        if not abc:
            continue
        try:
            out[side] = {
                "a": float(abc["a"]),
                "b": float(abc["b"]),
                "c": float(abc["c"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return {"orientation": out} if out else {}


def flow_from_legacy(cfg: Any) -> FlowDef:
    """从旧的 ``configs/arm.yaml`` 的 ``grasp.flow`` 合成一条等价流程。

    **这是升级期的兜底** —— 万一 ``configs/flows/`` 里没有 ``grasp.yaml``,
    就用旧配置拼一条出来, 保证服务还能跑。

    合成出来的步骤 id 与旧流程语义一一对应:
    ``ready`` / ``detect`` / ``above`` / ``grasp`` / ``close`` / ``safe``。
    """
    flow = FlowDef(
        name=DEFAULT_FLOW,
        description="标准抓取 (由 configs/arm.yaml 的 grasp.flow 合成)",
        source="config",
        recovery_action=str(cfg.safe_action or ""),
        recovery_label="回安全点",
    )

    def _arm(step_id: str, action: str, label: str) -> FlowStep:
        params = {"action": action} if action else {}
        return FlowStep(id=step_id, type="arm", label=label, params=params)

    flow.steps.append(
        _arm("ready", str(cfg.ready_action or ""), "回准备点")
    )
    flow.steps.append(
        FlowStep(
            id="detect",
            type="detect",
            label="拍照识别",
            params=_detect_orientation_params(cfg),
        )
    )

    # 具名点: object = 识别结果; above_point = object 正上方
    flow.steps.append(
        FlowStep(
            id="object",
            type="target",
            label="目标物体",
            params={"source": "detect"},
        )
    )
    flow.steps.append(
        FlowStep(
            id="above_point",
            type="target",
            label="目标上方",
            params={
                "base": "object",
                "distance": "approach",
                "offset": {"x": 0.0, "y": 0.0, "z": 0.0},
            },
        )
    )

    flow.steps.append(
        FlowStep(
            id="above",
            type="arm",
            label="移到上方点",
            params={
                "point": "above_point",
                "duration": float(cfg.above_duration),
            },
        )
    )
    flow.steps.append(
        FlowStep(
            id="grasp",
            type="arm",
            label="移到抓取点",
            params={
                "point": "object",
                "offset": {
                    "x": float(cfg.grasp_offset_x),
                    "y": float(cfg.grasp_offset_y),
                    "z": float(cfg.grasp_offset_z),
                },
                "duration": float(cfg.grasp_duration),
            },
        )
    )
    flow.steps.append(
        FlowStep(id="close", type="hand", label="闭合夹爪")
    )
    flow.steps.append(
        _arm("safe", str(cfg.safe_action or ""), "回安全点")
    )
    return flow


__all__ = [
    "DEFAULT_FLOW",
    "FlowDef",
    "FlowError",
    "FlowPoint",
    "FlowStep",
    "HAND_DIRECTIONS",
    "STEP_TYPES",
    "TARGET_SOURCES",
    "flow_from_legacy",
    "flows_signature",
    "load_flow_file",
    "load_flows",
    "parse_flow",
]
