# 相机无法识别排查

症状：`vision.recognize` 没有图像输出，OpenCV 报 `can't open camera by index`，但 `/dev/video*` 存在。

多品牌相机的接入见 [multi-brand-cameras.md](./multi-brand-cameras.md)，配置见 [vision-service.md](./vision-service.md)。

## 按顺序排查

### 1. 确认设备与占用

```bash
ls -la /dev/video*
lsof /dev/video0                 # 是否被其他程序占用
groups $USER | grep video        # 是否在 video 组
python3 -c "import cv2; print(cv2.VideoCapture(0).isOpened())"
```

用 `cheese` 或 VLC 能打开，说明硬件正常，问题在权限或配置。

### 2. 摄像头被 BIOS 禁用（笔记本最常见）

设备文件存在但 OpenCV 打不开时，先检查 BIOS 中的 Integrated Camera 是否为 Enabled。进入 BIOS 的按键：联想 F2/Enter，戴尔 F2，惠普 F10/Esc，华硕 F2/Del，微星 Del。

### 3. 权限与驱动

```bash
sudo usermod -aG video $USER     # 需要重新登录
sudo modprobe -r uvcvideo && sudo modprobe uvcvideo
sudo apt install -y v4l-utils libv4l-dev
```

### 4. 硬件是否被系统识别

```bash
lsusb | grep -i camera
dmesg | grep -iE "camera|v4l2"
```

没有输出说明系统没识别到硬件，检查 BIOS 或更换外置 USB 摄像头。

### 5. 换相机或指定设备

遍历可用设备：

```bash
python3 -c "
import cv2
for i in range(4):
    c = cv2.VideoCapture(i)
    print(i, c.isOpened()); c.release()"
```

在 `configs/vision.yaml` 中设置：

```yaml
camera:
  camera_brand: ""         # 笔记本或 USB 摄像头；Orbbec 用 "orbbec"；RealSense 用 "realsense"
  serial_number: "1"       # 空字符串表示第一个可用设备；USB 摄像头可填设备号
```

Orbbec 相机需要带 USB 后端的 `pyorbbecsdk`（源码编译时加 `-DOB_BUILD_USB_PAL=ON`）。

## 验证

```bash
cd services/vision_service && python3 tools/check_vision.py
cd services/agent_service  && python3 tools/demo_skill_workflow.py
```

相机故障时，Skill 会通过 TTS 播报具体原因，见 [skill-framework.md](./skill-framework.md)。
