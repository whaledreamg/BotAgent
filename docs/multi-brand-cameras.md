# 多品牌相机支持指南

本文档说明如何在 Bot Agent 的视觉服务中使用不同品牌的相机。

## 支持的相机品牌

### 1. 笔记本内置摄像头（推荐用于开发/测试）

**配置代码**: `""` (空字符串)

**特点**:
- 无需额外 SDK
- 支持笔记本内置摄像头
- 支持 USB 接入的通用摄像头
- 通过 OpenCV 访问

**限制**:
- 仅支持 RGB 彩色，无深度信息
- 不支持 `align_depth_to_color`（深度不可用）
- 内参需要手工标定或在线标定

**使用示例**:

```yaml
vision:
  camera:
    camera_brand: ""  # 或不填，默认为空
    camera_type: head
    serial_number: "0"  # 摄像头设备 ID
    width: 1280
    height: 800
    fps: 30
```

**代码示例**:

```python
from config import load_config
from camera_factory import create_camera_adapter

cfg = load_config()
camera = create_camera_adapter(cfg, "head")
camera.open()
frame = camera.read()
print(f"采集到彩色图: {frame.color.shape}")
print(f"深度图: {frame.depth_raw}")  # None
camera.close()
```

---

### 2. Orbbec 3D 相机

**配置代码**: `"orbbec"`

**特点**:
- 支持彩色和深度信息
- 支持多个相机同时使用
- 提供厂商标定内参
- 高质量 3D 数据

**支持的型号**:
- Orbbec Astra
- Orbbec Astra Mini
- Orbbec Gemini
- 其他使用 pyorbbecsdk 的 Orbbec 产品

**依赖**:
```bash
pip install pyorbbecsdk
```

**使用示例**:

```yaml
vision:
  camera:
    camera_brand: "orbbec"
    camera_type: head
    serial_number: ""  # 空表示第一个可用设备
    width: 1280
    height: 800
    fps: 30
```

**代码示例**:

```python
from config import load_config
from camera_factory import create_camera_adapter

cfg = load_config()
camera = create_camera_adapter(cfg, "head")
camera.open()
frame = camera.read()
print(f"彩色图: {frame.color.shape}")
print(f"深度图: {frame.depth_raw.shape}")
print(f"内参: {frame.intrinsics.fx}, {frame.intrinsics.fy}")
camera.close()
```

---

### 3. USB3Vision 相机（待实现）

**配置代码**: `"usb3vision"`

**特点**:
- 工业级相机
- 高速 USB3 传输
- 需要 GenTL SDK

**依赖**:
```bash
pip install harvesters  # GenTL SDK
```

**状态**: 暂未实现，需要创建 `USB3VisionCamera` 类

---

### 4. Intel RealSense 相机（待实现）

**配置代码**: `"realsense"`

**特点**:
- RGB-D 深度相机
- 提供 IMU 数据
- 支持多种型号

**支持的型号**:
- RealSense D415
- RealSense D435
- RealSense L515

**依赖**:
```bash
pip install pyrealsense2
```

**状态**: 暂未实现，需要创建 `RealSenseCamera` 类

---

## 多相机混合配置示例

可以在同一个系统中使用不同品牌的相机：

```yaml
vision:
  camera:
    # 默认使用笔记本摄像头
    camera_brand: ""
    camera_type: head
    serial_number: "0"
  
  cameras:
    head:
      camera_brand: ""        # 笔记本摄像头（开发用）
      serial_number: "0"
      width: 1280
      height: 800
    
    left_wrist:
      camera_brand: "orbbec"  # Orbbec 3D 相机
      serial_number: "ORBBEC_LEFT_001"
      width: 640
      height: 480
    
    right_wrist:
      camera_brand: "orbbec"  # Orbbec 3D 相机
      serial_number: "ORBBEC_RIGHT_001"
      width: 640
      height: 480
```

**在代码中使用**:

```python
from config import load_config
from camera_factory import create_camera_adapter

cfg = load_config()

# 头部使用笔记本摄像头（快速测试）
head_camera = create_camera_adapter(cfg, "head")

# 腕部使用 Orbbec 3D 相机（生产环境）
left_camera = create_camera_adapter(cfg, "left_wrist")
right_camera = create_camera_adapter(cfg, "right_wrist")

head_camera.open()
left_camera.open()
right_camera.open()

head_frame = head_camera.read()
left_frame = left_camera.read()
right_frame = right_camera.read()

# 处理...

head_camera.close()
left_camera.close()
right_camera.close()
```

---

## 环境变量控制

除了配置文件，也可以通过环境变量在运行时切换相机品牌和类型：

```bash
# 切换到笔记本摄像头
export VISION_CAMERA_BRAND=""
export VISION_CAMERA_TYPE=head

# 或者切换到 Orbbec 相机
export VISION_CAMERA_BRAND="orbbec"
export VISION_CAMERA_TYPE=head

# 运行服务
python services/vision_service/main.py
```

**支持的环境变量**:
- `VISION_CAMERA_BRAND`: 相机品牌
- `VISION_CAMERA_TYPE`: 相机类型 (head/left_wrist/right_wrist)
- `VISION_CAMERA_SERIAL`: 设备序列号

---

## 添加新的相机品牌

如果需要添加支持新品牌的相机，步骤如下：

### 1. 创建相机适配器类

创建新文件 `services/vision_service/xxx_camera.py`:

```python
from camera_adapter import CameraAdapter, CameraError, Frame, Intrinsics

class XXXCamera(CameraAdapter):
    """XXX 品牌相机适配器。
    
    支持的相机:
      • XXX Model A
      • XXX Model B
    """
    
    def __init__(self, cfg):
        # 初始化
        pass
    
    def open(self):
        # 打开相机
        pass
    
    def close(self):
        # 关闭相机
        pass
    
    def read(self, timeout_ms=None):
        # 采帧
        pass
    
    @property
    def opened(self):
        return self._opened
    
    @property
    def device_info(self):
        return {}
    
    @property
    def intrinsics(self):
        return Intrinsics()
    
    @property
    def depth_scale(self):
        return 0.001
    
    def map_bbox_to_depth(self, bbox, color_intr):
        return bbox
```

### 2. 在工厂中注册

编辑 `services/vision_service/camera_factory.py`，在 `create_camera_adapter()` 函数中添加:

```python
from xxx_camera import XXXCamera

def create_camera_adapter(cfg, camera_type="head"):
    camera_cfg = _get_camera_config(cfg, camera_type)
    camera_brand = str(camera_cfg.camera_brand or "").strip().lower()
    
    # ... 现有代码 ...
    
    elif camera_brand == "xxx":
        return XXXCamera(camera_cfg)
    
    else:
        raise CameraError(f"不支持的相机品牌: {camera_brand}")
```

### 3. 更新配置文档

在相关配置文件中添加 `camera_brand: "xxx"` 的配置示例。

---

## 常见问题

### Q: 如何检查相机是否成功打开？

```python
camera = create_camera_adapter(cfg, "head")
camera.open()
if camera.opened:
    print("相机已打开")
else:
    print("相机打开失败")
```

### Q: 如何获取相机信息？

```python
camera = create_camera_adapter(cfg, "head")
camera.open()
print(camera.device_info)  # {'name': '...', 'serial_number': '...', ...}
print(camera.intrinsics)   # 内参对象
camera.close()
```

### Q: 笔记本摄像头没有内参怎么办？

编辑配置文件中的 `calibrated_intrinsics` 字段，填入标定后的内参值：

```yaml
calibrated_intrinsics:
  fx: 1000.0      # 焦距 x
  fy: 1000.0      # 焦距 y
  cx: 640.0       # 主点 x
  cy: 360.0       # 主点 y
  width: 1280
  height: 720
```

或者使用标定工具自动标定：

```bash
python services/vision_service/tools/calib_intrinsics.py
```

---

## 架构图

```
┌───────────────────────────────────────┐
│      VisionService                    │
└───────────────────┬───────────────────┘
                    │
                    │ uses
                    ▼
        ┌───────────────────────┐
        │  CameraAdapter        │ (interface)
        │  (abstract base)      │
        └───────────────────────┘
                    △
        ┌───────────┼───────────┬──────────────┐
        │           │           │              │
   BuiltinCamera OrbbecCamera USB3Vision   RealSense
   (OpenCV)      (pyorbbecsdk) (待实现)   (待实现)
        │           │           │              │
        │           │           │              │
   ┌────┴───┐  ┌────┴───┐  ┌──┴───┐   ┌──────┴────┐
   │         │  │         │  │      │   │           │
  笔记本   USB摄 Astra  Gemini GenTL   D415  D435
  摄像头   像头  Mini等                L515
```

---

## 总结

- **开发/测试**: 使用笔记本摄像头（`camera_brand: ""`）
- **生产环境**: 使用 Orbbec 或其他专业 3D 相机（`camera_brand: "orbbec"`）
- **多相机混合**: 不同部位可使用不同品牌相机
- **易于扩展**: 添加新品牌只需继承 `CameraAdapter` 并注册到工厂

