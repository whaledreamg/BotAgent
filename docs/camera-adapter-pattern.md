# 多相机配置示例

本文档展示如何在 `configs/vision.yaml` 中配置多个相机。

## 场景 1: 单个相机（默认）

这是最简单的配置，与原有配置兼容：

```yaml
vision:
  camera:
    camera_type: head  # 相机类型
    serial_number: ''  # 相机序列号，空表示用枚举到的第一个
    width: 1280
    height: 800
    fps: 30
    frame_timeout_ms: 2000
    align_depth_to_color: true
    calibrated_intrinsics:
      fx: 613.955834
      fy: 632.477331
      cx: 677.750872
      cy: 409.485186
      width: 1280
      height: 800
```

在这个配置下，系统会使用头部相机。

## 场景 2: 多个相机配置

当你有多个相机（头部、左腕、右腕）时，可以在 `cameras` 段中分别配置：

```yaml
vision:
  camera:
    # 主相机配置（默认使用这个）
    camera_type: head
    serial_number: ''
    width: 1280
    height: 800
  
  # 多相机配置（可选）
  cameras:
    head:
      serial_number: 'HEAD_CAM_001'
      width: 1280
      height: 800
      fps: 30
      align_depth_to_color: true
      calibrated_intrinsics:
        fx: 613.955834
        fy: 632.477331
        cx: 677.750872
        cy: 409.485186
        width: 1280
        height: 800
    
    left_wrist:
      serial_number: 'LEFT_WRIST_001'
      width: 640
      height: 480
      fps: 30
      align_depth_to_color: true
      calibrated_intrinsics:
        fx: 306.977917
        fy: 316.238666
        cx: 338.875436
        cy: 204.742593
        width: 640
        height: 480
    
    right_wrist:
      serial_number: 'RIGHT_WRIST_001'
      width: 640
      height: 480
      fps: 30
      align_depth_to_color: true
```

## 场景 3: 通过环境变量切换相机

在运行时，可以通过环境变量切换相机类型，而无需修改配置文件：

```bash
# 使用头部相机（默认）
export VISION_CAMERA_TYPE=head
python services/vision_service/main.py

# 或切换到左腕相机
export VISION_CAMERA_TYPE=left_wrist
python services/vision_service/main.py

# 或切换到右腕相机
export VISION_CAMERA_TYPE=right_wrist
python services/vision_service/main.py
```

## 在代码中使用多个相机

### 场景 1: 在服务内使用指定的相机

```python
from config import load_config
from camera_factory import create_camera_adapter

# 加载配置
cfg = load_config()

# 为头部创建适配器
head_camera = create_camera_adapter(cfg, "head")
head_camera.open()
head_frame = head_camera.read()
head_camera.close()

# 为左腕创建适配器
left_camera = create_camera_adapter(cfg, "left_wrist")
left_camera.open()
left_frame = left_camera.read()
left_camera.close()
```

### 场景 2: 在服务启动时指定相机

```python
from config import load_config
from service import VisionService

# 加载配置
cfg = load_config()

# VisionService 会自动使用 cfg.camera.camera_type 指定的相机
service = VisionService(cfg)
service.start()
```

## 适配器模式架构

```
┌─────────────────────────────────────────────────┐
│         VisionService (main service)            │
└──────────────────┬──────────────────────────────┘
                   │
                   │ uses
                   ▼
        ┌──────────────────────┐
        │  CameraAdapter       │ (abstract interface)
        │  ├─ open()           │
        │  ├─ close()          │
        │  ├─ read()           │
        │  ├─ opened           │
        │  └─ intrinsics, ...  │
        └──────────────────────┘
                   △
                   │ implements
                   │
        ┌──────────────────────┐
        │  OrbbecCamera        │ (concrete implementation)
        │  (works with all     │
        │   camera types)      │
        └──────────────────────┘
```

## 关键特性

1. **配置驱动** - 在 YAML 中切换相机，无需改代码
2. **易扩展** - 添加新的相机类型只需实现 CameraAdapter 接口
3. **向后兼容** - 现有代码无需修改，工厂隐藏了细节
4. **测试友好** - 可轻松注入 Mock 相机进行单元测试

## 更新现有代码

由于采用了工厂模式，现有使用 VisionService 的代码无需任何修改：

```python
# 原有代码仍然工作
from config import load_config
from service import VisionService

cfg = load_config()
service = VisionService(cfg)
service.start()
```

当需要在代码中直接创建相机时，使用工厂而不是直接实例化：

```python
# 新的方式（推荐）
from camera_factory import create_camera_adapter

adapter = create_camera_adapter(cfg, "left_wrist")

# 旧的方式仍然工作（向后兼容）
from camera import OrbbecCamera

camera = OrbbecCamera(cfg.camera)
```

