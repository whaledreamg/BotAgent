"""Orbbec 相机封装。

只做「采帧 + 内参」这一层, 不含任何推理逻辑 —— 推理在 detector.py。

**pyorbbecsdk 的 API 是 snake_case** (跟 C++ SDK 的驼峰不一样):
    ctx.query_devices() / pipeline.wait_for_frames() / frame.get_data()

几个必须注意的点:
1. 深度帧的 `get_data()` 是 **uint16 原始值**, 必须乘 `get_depth_scale()`
   才是米。漏乘的话距离会差 1000 倍。
2. `enable_video_stream` 有两个重载: 传 `OBSensorType` 或 `OBStreamType`。
   这里用前者, 因为它能同时带上 format。
3. 彩色流是 RGB 排列 (不是 OpenCV 习惯的 BGR), 转 numpy 后要换通道。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("vision.camera")


class CameraError(RuntimeError):
    """相机不可用或采帧失败。"""


# ------------------------------------------------------------ DeviceInfo
#
# DeviceInfo 不是 dict, 而是一组 get_xxx() 方法:
#     get_name() / get_serial_number() / get_pid() / get_vid()
#
# 不同版本的 pybind 上方法名可能略有出入, 所以这里做一层兼容 ——
# 拿不到就返回空值, 不让整个枚举流程挂掉。

def _info_call(info: Any, *names: str) -> str:
    """依次尝试几个方法名, 返回第一个能用的结果。"""
    for name in names:
        fn = getattr(info, name, None)
        if fn is None:
            continue
        try:
            value = fn() if callable(fn) else fn
        except Exception:
            continue
        if value is None:
            continue
        return str(value)
    return ""


def _info_name(info: Any) -> str:
    return _info_call(info, "get_name", "name")


def _info_serial(info: Any) -> str:
    return _info_call(info, "get_serial_number", "get_serialNumber", "serial_number")


def _info_pid(info: Any) -> str:
    return _info_call(info, "get_pid", "pid")


def _as_int(value: Any, default: int = 0) -> int:
    """把 pid/vid 这类「可能是字符串的整数」转成 int。

    SDK 的 `get_pid()` 在不同版本里返回 int 或 str 都有 —— 直接拿去做
    `%04x` 格式化会在字符串上报 `TypeError: %x format: an integer is
    required, not str`, 而 logging 报错会把整条日志搅乱。
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        # "0x0807" / "2055" 都能吃
        return int(str(value).strip(), 0)
    except (TypeError, ValueError):
        return default


def _info_vid(info: Any) -> str:
    return _info_call(info, "get_vid", "vid")


@dataclass
class Intrinsics:
    """相机内参 (针孔模型)。

    3D 反算用: X = (u - cx) * Z / fx, Y = (v - cy) * Z / fy
    """

    fx: float = 0.0
    fy: float = 0.0
    cx: float = 0.0
    cy: float = 0.0
    width: int = 0
    height: int = 0

    @property
    def valid(self) -> bool:
        return self.fx > 0 and self.fy > 0 and self.width > 0 and self.height > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fx": round(self.fx, 4),
            "fy": round(self.fy, 4),
            "cx": round(self.cx, 4),
            "cy": round(self.cy, 4),
            "width": self.width,
            "height": self.height,
        }


@dataclass
class Frame:
    """一次采集的结果。"""

    #: 彩色图, BGR (OpenCV 习惯)
    color: Optional[np.ndarray] = None
    #: 深度图, 原始 uint16 值 (未乘 scale)
    depth_raw: Optional[np.ndarray] = None
    #: 深度值 -> 米的换算系数
    depth_scale: float = 0.001
    #: 彩色相机内参
    intrinsics: Intrinsics = field(default_factory=Intrinsics)
    #: 深度相机内参 (深度图自己的分辨率)
    depth_intrinsics: Intrinsics = field(default_factory=Intrinsics)
    #: 深度相机 -> 彩色相机 的外参 (3x3 旋转 + 3 维平移, 平移单位米)
    #: 没有时是 None —— 深度与彩色就是同一个坐标系。
    depth_to_color: Optional[Tuple[np.ndarray, np.ndarray]] = None
    #: 标定好的彩色内参 (覆盖 SDK 标称内参)。
    #: 标定结果见 tools/calib_intrinsics.py。
    calibrated_intrinsics: Optional[Intrinsics] = None
    #: 标定好的畸变系数 (5 个或更多)
    calibrated_dist: Optional[np.ndarray] = None

    @property
    def depth_meters(self) -> Optional[np.ndarray]:
        """深度图 (米)。"""
        if self.depth_raw is None:
            return None
        return self.depth_raw.astype(np.float32) * self.depth_scale


class OrbbecCamera:
    """Orbbec RGBD 相机。

    用法::

        cam = OrbbecCamera(cfg.camera)
        cam.open()
        frame = cam.read()
        cam.close()
    """

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self._ctx = None
        self._device = None
        self._pipeline = None
        self._device_info: Dict[str, Any] = {}
        self._intrinsics = Intrinsics()
        #: 标定好的内参 (覆盖 SDK 标称值); None = 用 SDK 的
        self._cal_intrinsics: Optional[Intrinsics] = None
        self._depth_scale = 0.001
        self._opened = False
        #: 实际生效的对齐模式: HW_MODE / SW_MODE / DISABLE
        self._align_mode = "DISABLE"
        #: 深度图自己的内参 (未对齐时与彩色不同)
        self._depth_intrinsics = Intrinsics()
        #: 深度图分辨率 (宽, 高)
        self._depth_shape: Tuple[int, int] = (0, 0)
        #: 软件对齐过滤器 (AlignFilter), HW_MODE 不可用时用它做 D2C
        self._align_filter = None
        #: 枚举设备时的重试次数与间隔 (相机刚插上时可能枚举不到)
        self._enum_retries = 5
        self._enum_retry_delay = 1.0

    # ------------------------------------------------------------ 状态

    @property
    def opened(self) -> bool:
        return self._opened

    @property
    def device_info(self) -> Dict[str, Any]:
        return dict(self._device_info)

    @property
    def intrinsics(self) -> Intrinsics:
        return self._intrinsics

    @property
    def depth_scale(self) -> float:
        """深度原始值 -> 米的换算系数。

        SDK 给的 scale 是「毫米单位下的倍率」(官方示例里
        `pixel * scale` 的结果是毫米), 所以这里再乘 0.001 换成米。
        """
        return self._depth_scale * 0.001

    # ------------------------------------------------------------ 打开/关闭

    def _enumerate_devices(self, ob: Any):
        """枚举设备, 带重试。

        相机刚插上 / 刚被别的进程释放时, SDK 偶尔会枚举不到 (返回 0 个)。
        设备通常几百毫秒就能就绪, 所以重试几次。

        返回 (ctx, devices)。
        """
        ctx = ob.Context()
        last_error: Optional[Exception] = None

        for attempt in range(self._enum_retries):
            try:
                devices = ctx.query_devices()
            except Exception as exc:
                last_error = exc
                devices = []
            if devices:
                return ctx, devices
            if attempt < self._enum_retries - 1:
                logger.info(
                    "没枚举到相机, %s 秒后重试 (%d/%d)",
                    self._enum_retry_delay, attempt + 1, self._enum_retries,
                )
                time.sleep(self._enum_retry_delay)

        if last_error is not None:
            raise CameraError(f"枚举相机失败: {last_error}")
        return ctx, []

    def open(self) -> None:
        """枚举设备并启动数据流。失败抛 CameraError。

        **SDK 的异常必须在这里被兜住** —— `pyorbbecsdk` 抛的是
        `OBError` (不是我们的 `CameraError`), 直接冒出去会让 FastAPI
        的 lifespan 崩掉, 整个服务起不来。

        典型场景: 相机是 USB **独占**设备 —— 已经有另一个进程 (比如
        标定脚本或上一个没退干净的实例) 开着它, 这里就会报
        `uvc_open failed -6`。
        """
        if self._opened:
            return

        try:
            import pyorbbecsdk as ob
        except ImportError as exc:  # pragma: no cover
            raise CameraError(
                "pyorbbecsdk 未安装 —— 装带 USB 后端的版本, "
                "或用源码编译 (OB_BUILD_USB_PAL=ON)"
            ) from exc

        self._ob = ob

        try:
            self._open_device(ob)
        except CameraError:
            raise
        except Exception as exc:
            raise CameraError(f"打开相机失败: {exc}") from exc

    def _open_device(self, ob: Any) -> None:
        """真正的打开流程 (异常由 `open()` 统一包成 CameraError)。"""

        # ---- 枚举设备 (带重试) ----
        ctx, devices = self._enumerate_devices(ob)

        if not devices:
            raise CameraError(
                "没有找到 Orbbec 相机。\n"
                "  常见原因: pip 装的 pyorbbecsdk 是无 USB 后端的构建 "
                "(日志里会报 'Usb pal is not exist'), 需要用源码编译 "
                "(OB_BUILD_USB_PAL=ON) 或换用支持 USB 的构建。"
            )

        # ---- 选设备 ----
        device = None
        want_serial = str(getattr(self.cfg, "serial_number", "") or "")
        if want_serial:
            for dev in devices:
                info = dev.get_device_info()
                if _info_serial(info) == want_serial:
                    device = dev
                    break
            if device is None:
                raise CameraError(
                    f"找不到序列号为 {want_serial} 的相机 "
                    f"(共 {len(devices)} 个设备)"
                )
        else:
            device = devices[0]
        info = device.get_device_info()
        self._device_info = {
            "name": _info_name(info),
            "serial_number": _info_serial(info),
            "pid": _info_pid(info),
            "vid": _info_vid(info),
            "device_count": len(devices),
        }
        logger.info(
            "相机: %s (SN=%s, pid=0x%04x), 共发现 %d 个设备",
            self._device_info["name"],
            self._device_info["serial_number"],
            _as_int(self._device_info["pid"]),
            len(devices),
        )

        self._ctx = ctx
        self._device = device

        # ---- 配置数据流 ----
        config = ob.Config()
        width = int(getattr(self.cfg, "width", 0) or 0)
        height = int(getattr(self.cfg, "height", 0) or 0)
        fps = int(getattr(self.cfg, "fps", 0) or 0)

        # 彩色: RGB 格式 —— SDK 侧拿到的是 RGB, 后面转 BGR
        config.enable_video_stream(
            ob.OBSensorType.COLOR_SENSOR, width, height, fps, ob.OBFormat.RGB
        )
        # 深度: 不指定 format, 让 SDK 用默认 (Z16)
        config.enable_video_stream(
            ob.OBSensorType.DEPTH_SENSOR, width, height, fps, ob.OBFormat.UNKNOWN_FORMAT
        )
        config.set_frame_aggregate_output_mode(
            ob.OBFrameAggregateOutputMode.FULL_FRAME_REQUIRE
        )
        # 对齐模式在下面的启动循环里逐个尝试 —— set_align_mode 不校验兼容性,
        # 真正的错误要到 pipeline.start() 才暴露出来。

        # ---- 启动 ----
        #
        # 对齐模式的兼容性只有在 pipeline.start() 时才暴露出来
        # (set_align_mode 不校验), 所以这里逐个尝试:
        #   硬件对齐 -> 软件对齐 -> 不对齐
        #
        # **注意**: `config.set_align_mode(SW_MODE)` 对 Gemini 336L **无效** ——
        # 它不会真的把深度投影到彩色坐标系, 只是标记一下。实测深度图仍是
        # 848x480 的原始深度, 与 1280x800 的彩色图错位, 拿彩色框去查深度
        # 会查到完全无关的像素。
        #
        # 真正能做 D2C 的办法是 SDK 的 **AlignFilter** (软件对齐): 把 frameset
        # 丢给它, 它返回一个新的 frameset, 里面的深度帧已经按彩色相机的
        # 光轴重投影过 (分辨率也跟彩色一致)。
        #
        # 所以这里的策略是:
        #   1. 试 HW_MODE (硬件 D2C, 最好的方案)
        #   2. 试 SW_MODE + AlignFilter (软件 D2C)
        #   3. 都不行 -> DISABLE, 退化成「按内参映射 bbox」

        align_modes = ["HW_MODE", "SW_MODE", "DISABLE"]
        if not getattr(self.cfg, "align_depth_to_color", True):
            align_modes = ["DISABLE"]

        pipeline = None
        last_error: Optional[Exception] = None
        used_align = "DISABLE"

        for mode_name in align_modes:
            try:
                config.set_align_mode(getattr(ob.OBAlignMode, mode_name))
            except Exception:
                pass
            try:
                pipeline = ob.Pipeline(device)
                pipeline.start(config)
                used_align = mode_name
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                logger.warning("对齐模式 %s 启动失败: %s", mode_name, exc)
                try:
                    pipeline.stop()
                except Exception:
                    pass
                pipeline = None

        if pipeline is None:
            raise CameraError(
                f"启动数据流失败: {last_error}"
            ) from last_error

        self._align_mode = used_align
        self._pipeline = pipeline
        self._opened = True

        # ---- 软件对齐过滤器 ----
        #
        # HW_MODE 之外的情况一律挂上 AlignFilter, 把深度真正投到彩色坐标系。
        if used_align != "HW_MODE" and getattr(
            self.cfg, "align_depth_to_color", True
        ):
            self._align_filter = self._create_align_filter(ob)

        logger.info(
            "数据流已启动 (对齐模式: %s, AlignFilter: %s)",
            used_align,
            "on" if self._align_filter is not None else "off",
        )

        # ---- 读第一帧, 顺便取内参 ----
        try:
            first = self.read()
        except CameraError:
            self.close()
            raise
        if first is not None:
            self._intrinsics = first.intrinsics
            self._depth_scale = first.depth_scale / 0.001
            logger.info(
                "内参: fx=%.2f fy=%.2f cx=%.2f cy=%.2f (%dx%d), depth_scale=%g (毫米倍率)",
                self._intrinsics.fx,
                self._intrinsics.fy,
                self._intrinsics.cx,
                self._intrinsics.cy,
                self._intrinsics.width,
                self._intrinsics.height,
                self._depth_scale,
            )

        # ---- 覆盖成标定内参 ----
        #
        # SDK 给的是**标称内参**, 实测通常差 1~2%。1% 在 1.5 m 处就是
        # 15 mm 的横向误差, 跟手眼外参的误差同量级 —— 所以标定过的内参
        # 优先级更高。
        self._apply_calibrated_intrinsics()

    def _apply_calibrated_intrinsics(self) -> None:
        """用标定内参覆盖 SDK 内参 (如果配了)。"""
        cal = getattr(self.cfg, "calibrated_intrinsics", None)
        if cal is None:
            return
        try:
            fx = float(cal.get("fx", 0.0))
            fy = float(cal.get("fy", 0.0))
            cx = float(cal.get("cx", 0.0))
            cy = float(cal.get("cy", 0.0))
        except Exception as exc:
            logger.warning("读标定内参失败: %s", exc)
            return

        if fx <= 0 or fy <= 0:
            logger.warning("标定内参不完整, 继续用 SDK 内参")
            return

        width = int(cal.get("width", 0) or self._intrinsics.width)
        height = int(cal.get("height", 0) or self._intrinsics.height)

        # 分辨率对不上就不能用 —— 内参是跟着分辨率走的
        if (
            self._intrinsics.width
            and width
            and (width != self._intrinsics.width or height != self._intrinsics.height)
        ):
            logger.warning(
                "标定内参的分辨率 %dx%d 与当前彩色流 %dx%d 不符, 不用它",
                width, height, self._intrinsics.width, self._intrinsics.height,
            )
            return

        old = self._intrinsics
        self._cal_intrinsics = Intrinsics(
            fx=fx, fy=fy, cx=cx, cy=cy,
            width=width or old.width,
            height=height or old.height,
        )
        logger.info(
            "已用标定内参覆盖 SDK 内参: fx %.2f->%.2f cx %.2f->%.2f",
            old.fx, fx, old.cx, cx,
        )

    def close(self) -> None:
        """停止数据流并释放资源。"""
        if self._pipeline is not None:
            try:
                self._pipeline.stop()
            except Exception:  # pragma: no cover
                pass
            self._pipeline = None
        self._align_filter = None
        self._device = None
        self._ctx = None
        self._opened = False

    def __enter__(self) -> "OrbbecCamera":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------ 采帧

    def read(self, timeout_ms: Optional[int] = None) -> Optional[Frame]:
        """采一帧。超时返回 None。"""
        if not self._opened or self._pipeline is None:
            raise CameraError("相机未打开")

        if timeout_ms is None:
            timeout_ms = int(getattr(self.cfg, "frame_timeout_ms", 2000))

        try:
            frames = self._pipeline.wait_for_frames(timeout_ms)
        except Exception as exc:
            raise CameraError(f"采帧失败: {exc}") from exc

        if frames is None:
            return None

        # ---- 软件 D2C 对齐 ----
        #
        # AlignFilter 把深度帧按彩色相机的光轴重投影 (分辨率也跟彩色一致),
        # 这样彩色图上的像素可以直接查深度图, 不用再做坐标映射。
        if self._align_filter is not None:
            try:
                aligned = self._align_filter.process(frames)
                if aligned is not None:
                    frames = aligned
            except Exception as exc:
                logger.warning("AlignFilter 处理失败, 用未对齐的帧: %s", exc)

        color_frame = None
        depth_frame = None
        # FrameSet 的取帧方式在不同版本里不一样, 逐个试
        try:
            color_frame = frames.get_color_frame()
        except Exception:
            pass
        if color_frame is None:
            try:
                color_frame = frames.get_frame(ob.OBFrameType.COLOR_FRAME)
            except Exception:
                pass
        try:
            depth_frame = frames.get_depth_frame()
        except Exception:
            pass
        if depth_frame is None:
            try:
                depth_frame = frames.get_frame(ob.OBFrameType.DEPTH_FRAME)
            except Exception:
                pass

        if color_frame is None and depth_frame is None:
            return None

        color = None
        if color_frame is not None:
            color = self._to_bgr(color_frame)

        depth_raw = None
        depth_intr = Intrinsics()
        if depth_frame is not None:
            w = int(depth_frame.get_width())
            h = int(depth_frame.get_height())
            data = depth_frame.get_data()
            if data is not None and w > 0 and h > 0:
                arr = np.frombuffer(data, dtype=np.uint16)
                if arr.size >= w * h:
                    depth_raw = arr[: w * h].reshape(h, w)
            depth_intr = self._read_intrinsics(depth_frame, None)
        self._depth_intrinsics = depth_intr

        # 深度 scale: 每帧都可能变 (不同模式下 scale 不同)
        #
        # **SDK 的约定 (见官方示例 1.stream.depth/depth.cpp)**:
        #     pixel * scale = 实际距离, 单位是**毫米**
        #
        # 所以 Gemini 336L 返回 scale=1.0 时, 原始值就是毫米。
        # 这里统一换算成米, 让下游只跟「米」打交道。
        depth_scale = self._depth_scale
        if depth_frame is not None:
            try:
                s = float(depth_frame.get_depth_scale())
                if s > 0:
                    depth_scale = s
            except Exception:
                pass
        # 毫米 -> 米
        depth_scale_m = depth_scale * 0.001

        # 记录深度图的分辨率 —— 软件对齐后它可能跟彩色图不一样,
        # 决定要不要做坐标映射
        if depth_raw is not None:
            self._depth_shape = (int(depth_raw.shape[1]), int(depth_raw.shape[0]))

        return Frame(
            color=color,
            depth_raw=depth_raw,
            depth_scale=depth_scale_m,
            intrinsics=self._effective_intrinsics(
                self._read_intrinsics(color_frame, depth_frame)
            ),
            depth_intrinsics=depth_intr,
            depth_to_color=self._read_extrinsic(color_frame, depth_frame),
        )

    def _effective_intrinsics(self, sdk_intr: Intrinsics) -> Intrinsics:
        """最终生效的彩色内参。

        标定过的内参优先 —— SDK 给的是**标称值**, 实测差 1~2%, 而 1% 在
        1.5 m 处就是 15 mm 的横向误差。
        """
        cal = self._cal_intrinsics
        if cal is None:
            return sdk_intr

        # 分辨率对不上就不能用 —— 内参是跟着分辨率走的
        if (
            sdk_intr.valid
            and cal.width
            and cal.height
            and (cal.width != sdk_intr.width or cal.height != sdk_intr.height)
        ):
            return sdk_intr

        return cal

    # ------------------------------------------------------------ 内部工具

    def _create_align_filter(self, ob: Any):
        """建一个把深度对齐到彩色坐标系的 AlignFilter。

        对齐方向固定是 **D2C (Depth To Color)** —— 因为检测框是在彩色图上
        出的, 让深度去对齐彩色比反过来更省事。

        `set_match_target_resolution(True)` 让对齐后的深度分辨率
        跟彩色一致 (1280x800), 这样像素坐标能一一对应。
        """
        try:
            align = ob.AlignFilter(ob.OBStreamType.COLOR_STREAM)
        except Exception as exc:
            logger.warning("创建 AlignFilter 失败, 深度将不做 D2C 对齐: %s", exc)
            return None

        try:
            align.set_match_target_resolution(True)
        except Exception as exc:
            logger.debug("set_match_target_resolution 不可用: %s", exc)

        # 有条件的话指定彩色流 profile (不同 profile 的内参不一样,
        # 对齐必须针对具体的彩色流)
        try:
            profile = self._color_profile(ob)
            if profile is not None:
                align.set_align_to_stream_profile(profile)
                logger.info("AlignFilter 目标彩色流: %dx%d",
                            profile.get_width(), profile.get_height())
        except Exception as exc:
            logger.debug("指定 AlignFilter 目标流失败: %s", exc)

        return align

    def _color_profile(self, ob: Any):
        """找一个 RGB 彩色流 profile。找不到返回 None。"""
        try:
            profiles = self._pipeline.get_stream_profile_list(
                ob.OBSensorType.COLOR_SENSOR
            )
        except Exception:
            return None
        if profiles is None:
            return None
        try:
            count = profiles.get_count()
        except Exception:
            count = 0
        for i in range(count):
            try:
                prof = profiles.get_video_stream_profile(i)
            except Exception:
                continue
            if prof is None:
                continue
            try:
                if prof.get_format() == ob.OBFormat.RGB:
                    return prof
            except Exception:
                return prof
        return None

    @property
    def align_mode(self) -> str:
        """实际生效的对齐模式。"""
        return self._align_mode

    def map_bbox_to_depth(self, bbox, color_intr: Intrinsics) -> tuple:
        """把彩色图上的框映射到深度图上。

        深度没跟彩色对齐时 (软件对齐模式下深度图仍是 848x480, 彩色是
        1280x800), 直接拿彩色像素去查深度会错位。

        转换方式: 彩色像素 -> 相机坐标系的方向向量 -> 深度像素。
        两个内参描述的是**同一个相机**, 所以方向向量可以直接换算:

            彩色: dir = ((u - cx_c)/fx_c, (v - cy_c)/fy_c)
            深度: u_d = dir_x * fx_d + cx_d

        注意: 这里算的是**归一化像平面坐标** (tan 角), 不是米 ——
        深度图与彩色图的光轴有微小偏移, 但在这个量级下可以忽略。

        返回 (x1, y1, x2, y2), 已经裁到深度图范围内。
        """
        depth_intr = self._depth_intrinsics
        if (
            not depth_intr.valid
            or not color_intr.valid
        ):
            return tuple(bbox[:4])

        # 对齐模式下两者分辨率一致, 直接用
        if (
            depth_intr.width == color_intr.width
            and depth_intr.height == color_intr.height
        ):
            return tuple(bbox[:4])

        x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
        cu = (x1 + x2) / 2.0
        cv = (y1 + y2) / 2.0

        try:
            # 彩色像素 -> 归一化像平面
            nx = (cu - color_intr.cx) / color_intr.fx
            ny = (cv - color_intr.cy) / color_intr.fy
            # 归一化像平面 -> 深度像素
            du = nx * depth_intr.fx + depth_intr.cx
            dv = ny * depth_intr.fy + depth_intr.cy
        except Exception:
            return tuple(bbox[:4])

        # 框的尺寸按分辨率比例缩放
        sx = depth_intr.width / float(color_intr.width or 1)
        sy = depth_intr.height / float(color_intr.height or 1)

        nx1 = du + (x1 - cu) * sx
        nx2 = du + (x2 - cu) * sx
        ny1 = dv + (y1 - cv) * sy
        ny2 = dv + (y2 - cv) * sy

        w = depth_intr.width
        h = depth_intr.height
        nx1 = max(0.0, min(nx1, w - 1.0))
        nx2 = max(nx1 + 1.0, min(nx2, float(w)))
        ny1 = max(0.0, min(ny1, h - 1.0))
        ny2 = max(ny1 + 1.0, min(ny2, float(h)))
        return (nx1, ny1, nx2, ny2)

    @staticmethod
    def _read_extrinsic(color_frame: Any, depth_frame: Any):
        """读深度相机 -> 彩色相机的外参。

        返回 (rot 3x3, trans 3), **trans 单位是米**。

        注意: SDK 的 `OBExtrinsic.transform` 单位是**毫米**
        (Gemini 336L 实测是 -23.75mm, 即两个镜头差了 2.4 厘米),
        这里统一换算成米 —— 不换算的话 X 会差 1000 倍。
        """
        if color_frame is None or depth_frame is None:
            return None
        try:
            cprof = color_frame.get_stream_profile()
            dprof = depth_frame.get_stream_profile()
            if cprof is None or dprof is None:
                return None
            ext = dprof.get_extrinsic_to(cprof)
            if ext is None:
                return None
            rot = np.asarray(ext.rot, dtype=np.float64)
            # 毫米 -> 米
            trans = np.asarray(ext.transform, dtype=np.float64).reshape(3) * 0.001
            if rot.shape != (3, 3):
                return None
            return rot, trans
        except Exception:
            return None

    @staticmethod
    def _to_bgr(frame: Any) -> Optional[np.ndarray]:
        """帧 -> BGR numpy。"""
        width = int(frame.get_width())
        height = int(frame.get_height())
        data = frame.get_data()
        if data is None or width <= 0 or height <= 0:
            return None

        fmt = frame.get_format()
        arr = np.frombuffer(data, dtype=np.uint8)
        if arr.size < width * height * 3:
            return None
        rgb = arr[: width * height * 3].reshape(height, width, 3)
        # SDK 给的是 RGB, OpenCV 习惯 BGR
        return np.ascontiguousarray(rgb[:, :, ::-1])

    @staticmethod
    def _read_intrinsics(
        color_frame: Any, depth_frame: Any
    ) -> Intrinsics:
        """从帧的 stream profile 里读内参。

        内参字段是 cx/cy (不是老版本的 ppx/ppy) —— 两种都试。

        优先用彩色帧的 —— 深度对齐后两者坐标系一致, 用彩色内参最直接。
        """
        for frame in (color_frame, depth_frame):
            if frame is None:
                continue
            try:
                profile = frame.get_stream_profile()
                if profile is None:
                    continue
                intr = profile.get_intrinsic()
                if intr is None:
                    continue
                width = int(frame.get_width())
                height = int(frame.get_height())

                def _pick(*names: str) -> float:
                    for n in names:
                        v = getattr(intr, n, None)
                        if v is not None:
                            return float(v)
                    return 0.0

                fx = _pick("fx")
                fy = _pick("fy")
                cx = _pick("cx", "ppx")
                cy = _pick("cy", "ppy")
                if fx <= 0 or fy <= 0:
                    continue
                return Intrinsics(
                    fx=fx,
                    fy=fy,
                    cx=cx,
                    cy=cy,
                    width=width,
                    height=height,
                )
            except Exception:
                continue
        return Intrinsics()


__all__ = ["CameraError", "Frame", "Intrinsics", "OrbbecCamera"]
