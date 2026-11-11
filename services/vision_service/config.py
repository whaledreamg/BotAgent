"""视觉服务配置加载。

配置来源优先级 (后者覆盖前者):
1. 本文件内的默认值
2. configs/vision.yaml
3. 环境变量 (VISION_*)

只依赖 PyYAML, 缺失时退化为内置默认值, 保证服务仍可启动。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


#: 项目根目录 (Bot Agent/)
ROOT = Path(__file__).resolve().parents[2]
VISION_CONFIG_PATH = ROOT / "configs" / "vision.yaml"


DEFAULTS: Dict[str, Any] = {
    "vision": {
        "server": {"host": "0.0.0.0", "port": 8803},
        "camera": {
            "serial_number": "",
            "width": 0,
            "height": 0,
            "fps": 0,
            "frame_timeout_ms": 2000,
            "align_depth_to_color": True,
            "calibrated_intrinsics": {},
        },
        "detector": {
            "weights": "",
            "pose_weights": "",
            "conf": 0.35,
            "find_conf": 0.15,
            "iou": 0.5,
            "device": "auto",
            "imgsz": 640,
            "max_objects": 20,
        },
        "solver": {
            "sample_grid": 5,
            "min_valid_ratio": 0.05,
            "depth_min": 0.1,
            "depth_max": 5.0,
            "erode_iterations": 1,
        },
        "recognition": {
            "mode": "live",
            "conf": 0.05,
            "frames": 5,
        },
        "extrinsic": {
            "enabled": False,
            "matrix": [],
            "rotation": [],
            "translation": [0.0, 0.0, 0.0],
            "flip_axes": False,
            "target_frame": "arm_base",
            "dynamic": False,
            "matrix_frame": "world",
            "arm": "auto",
            "arm_service_url": "http://127.0.0.1:8802",
        },
    }
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8803


def _parse_calibrated_intrinsics(raw: Any) -> Optional[Dict[str, float]]:
    """解析配置里的标定内参。

    全是 0 或者缺字段 -> 返回 None (表示「没标定, 用 SDK 内参」)。
    """
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        out = {str(k): float(v) for k, v in raw.items()}
    except (TypeError, ValueError):
        return None
    if out.get("fx", 0.0) <= 0 or out.get("fy", 0.0) <= 0:
        return None
    return out


@dataclass
class CameraConfig:
    """Orbbec 相机配置。"""

    #: 序列号; 空 = 用枚举到的第一个设备
    serial_number: str = ""
    width: int = 0
    height: int = 0
    fps: int = 0
    frame_timeout_ms: int = 2000
    align_depth_to_color: bool = True
    #: 标定好的彩色内参 (覆盖 SDK 标称值)。
    #: 由 tools/calib_intrinsics.py 标定后填进来。
    #: 形如 {"fx":.., "fy":.., "cx":.., "cy":.., "width":.., "height":..}
    calibrated_intrinsics: Optional[Dict[str, float]] = None


@dataclass
class DetectorConfig:
    """YOLO 检测器配置。"""

    weights: str = ""
    pose_weights: str = ""
    conf: float = 0.35
    #: 按名字查找物体时的置信度阈值 (比 conf 宽松)
    find_conf: float = 0.15
    iou: float = 0.5
    #: auto / cpu / "0" (GPU 序号)
    device: str = "auto"
    imgsz: int = 640
    max_objects: int = 20


@dataclass
class SolverConfig:
    """深度反算参数。"""

    sample_grid: int = 5
    min_valid_ratio: float = 0.05
    depth_min: float = 0.1
    depth_max: float = 5.0
    erode_iterations: int = 1


@dataclass
class RecognitionConfig:
    """识别测试工具 (tools/check_recognition.py) 的配置。

    **mode** 决定测试脚本怎么拿画面:
    - ``live``  直接开相机跑 YOLO, 并把标注图存到 logs/。
      **相机是独占的** —— 视觉服务在跑的时候这个模式会开相机失败
      (报 ``uvc_open failed -6``), 脚本会自动回退到 ``http``。
    - ``http`` 走视觉服务的 HTTP 接口 (``/v1/detect``), 不碰相机。

    默认用 ``live``: 视觉服务平时是空着的, 相机闲着也是闲着, 而且
    live 模式会存标注图, 框画得对不对肉眼看最快。
    """

    mode: str = "live"
    #: 测试用的置信度阈值。比服务默认的 ``detector.conf`` 松 ——
    #: 实测桌面场景的杯子/刀常落在 0.15~0.30。
    conf: float = 0.05
    #: live 模式最多试几帧, 取检测到东西最多的那帧
    frames: int = 5
    #: 标注图 / 原图的保存目录 (相对项目根)。留空 = logs/
    output_dir: str = "logs"


@dataclass
class ExtrinsicConfig:
    """手眼外参: 相机系 -> 目标坐标系。

    没标定时 enabled=False, 服务只输出相机坐标系。

    **target_frame** 说明目标系是哪个:
    - ``arm_base`` 机械臂基座系 —— 相机固定在躯干上时用这个,
      静态外参即可 (升降柱升降不影响相机与臂基座的相对关系)
    - ``world``    世界系 —— 需要 ``dynamic: true``, 运行时从
      ``arm_service /v1/kinematics`` 拿基座的动态位姿做补偿
    - ``left_base`` / ``right_base``  左/右臂各自的基座系 (双臂)

    **matrix_frame** 说明标定出来的 ``matrix`` 描述的是「相机 -> 哪个系」:
    - ``world``    标定时直接解出的 T_world_cam (眼在手外, 板挂工具)
    - ``head``     T_head_cam (相机 -> 头座); 头会动时用, 运行时补上
      T_world_head(q_head) 才得到 T_world_cam
    - ``arm_base`` T_base_cam (相机 -> 某条臂基座), 静态量
    """

    enabled: bool = False
    #: 4x4 齐次矩阵 (行主序); 非空时优先于 rotation/translation
    matrix: List[List[float]] = field(default_factory=list)
    #: 3x3 旋转矩阵 (行主序)
    rotation: List[List[float]] = field(default_factory=list)
    #: 3 维平移 (米)
    translation: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    #: 乘外参前先做相机系 -> 基座系的**轴置换** (X右Y下Z前 -> X前Y左Z上)。
    #:
    #: 推荐做法是把这个置换直接烘进 ``matrix``, 然后这里置 false ——
    #: 一切都在矩阵里, 不会有隐式变换把标定结果带偏。
    flip_axes: bool = False
    #: 目标坐标系: arm_base / world / left_base / right_base
    target_frame: str = "arm_base"
    #: 标定矩阵描述的是「相机 -> 哪个系」: world / head / arm_base
    matrix_frame: str = "world"
    #: 目标系为 arm_base 时用哪条臂: left / right / auto
    arm: str = "auto"
    #: 是否需要叠加基座的动态偏移 (双臂分档 / 头部补偿时需要)
    dynamic: bool = False
    #: arm_service 的地址 (动态外参时用来拿基座位姿)
    arm_service_url: str = "http://127.0.0.1:8802"

    def is_configured(self) -> bool:
        """外参是否真的填了。"""
        if self.matrix and len(self.matrix) == 4:
            return True
        if self.rotation and len(self.rotation) == 3:
            return True
        return False


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    solver: SolverConfig = field(default_factory=SolverConfig)
    extrinsic: ExtrinsicConfig = field(default_factory=ExtrinsicConfig)
    recognition: RecognitionConfig = field(default_factory=RecognitionConfig)


def load_config(path: Optional[Path] = None) -> Config:
    """加载配置: 默认值 -> yaml -> 环境变量。"""
    # DEFAULTS 的内容就是 vision 这一节的内部结构
    merged: Dict[str, Any] = dict(DEFAULTS["vision"])

    cfg_path = Path(path) if path else VISION_CONFIG_PATH
    if yaml is not None and cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            # 顶层是 "vision:" 还是直接就是内容
            section = loaded.get("vision", loaded)
            merged = _deep_merge(merged, section)
        except Exception as exc:  # pragma: no cover - 配置坏了不该让服务起不来
            print(f"[vision] 配置读取失败, 用默认值: {exc}")

    v = merged

    s = v.get("server", {})
    server = ServerConfig(
        host=os.environ.get(
            "VISION_SERVER_HOST", str(s.get("host", "0.0.0.0"))
        ),
        port=int(os.environ.get("VISION_SERVER_PORT", s.get("port", 8803))),
    )

    c = v.get("camera", {})
    cal_intr = c.get("calibrated_intrinsics") or c.get("intrinsics") or None
    camera = CameraConfig(
        serial_number=os.environ.get(
            "VISION_CAMERA_SERIAL", str(c.get("serial_number", "") or "")
        ),
        width=int(c.get("width", 0) or 0),
        height=int(c.get("height", 0) or 0),
        fps=int(c.get("fps", 0) or 0),
        frame_timeout_ms=int(c.get("frame_timeout_ms", 2000)),
        align_depth_to_color=bool(c.get("align_depth_to_color", True)),
        calibrated_intrinsics=_parse_calibrated_intrinsics(cal_intr),
    )

    d = v.get("detector", {})
    detector = DetectorConfig(
        weights=os.environ.get(
            "VISION_WEIGHTS", str(d.get("weights", "") or "")
        ),
        pose_weights=os.environ.get(
            "VISION_POSE_WEIGHTS", str(d.get("pose_weights", "") or "")
        ),
        conf=float(d.get("conf", 0.35)),
        find_conf=float(d.get("find_conf", 0.15)),
        iou=float(d.get("iou", 0.5)),
        device=os.environ.get("VISION_DEVICE", str(d.get("device", "auto"))),
        imgsz=int(d.get("imgsz", 640) or 640),
        max_objects=int(d.get("max_objects", 20)),
    )

    so = v.get("solver", {})
    solver = SolverConfig(
        sample_grid=int(so.get("sample_grid", 5)),
        min_valid_ratio=float(so.get("min_valid_ratio", 0.05)),
        depth_min=float(so.get("depth_min", 0.1)),
        depth_max=float(so.get("depth_max", 5.0)),
        erode_iterations=int(so.get("erode_iterations", 1)),
    )

    rc = v.get("recognition", {})
    recognition = RecognitionConfig(
        mode=str(rc.get("mode", "live") or "live").strip().lower(),
        conf=float(rc.get("conf", 0.05)),
        frames=int(rc.get("frames", 5) or 5),
        output_dir=str(rc.get("output_dir", "logs") or "logs"),
    )

    e = v.get("extrinsic", {})
    extrinsic = ExtrinsicConfig(
        enabled=bool(e.get("enabled", False)),
        matrix=list(e.get("matrix", []) or []),
        rotation=list(e.get("rotation", []) or []),
        translation=list(e.get("translation", [0.0, 0.0, 0.0]) or [0.0, 0.0, 0.0]),
        flip_axes=bool(e.get("flip_axes", False)),
        target_frame=str(e.get("target_frame", "arm_base") or "arm_base"),
        matrix_frame=str(e.get("matrix_frame", "world") or "world"),
        arm=str(e.get("arm", "auto") or "auto"),
        dynamic=bool(e.get("dynamic", False)),
        arm_service_url=str(
            e.get("arm_service_url", "http://127.0.0.1:8802")
            or "http://127.0.0.1:8802"
        ),
    )

    return Config(
        server=server,
        camera=camera,
        detector=detector,
        solver=solver,
        extrinsic=extrinsic,
        recognition=recognition,
    )


__all__ = [
    "CameraConfig",
    "Config",
    "DetectorConfig",
    "ExtrinsicConfig",
    "RecognitionConfig",
    "ServerConfig",
    "SolverConfig",
    "load_config",
]
