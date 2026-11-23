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

---

## Orbbec 3D 相机专用排查

Orbbec 是 **USB 独占**设备, 报错信息又很不直观, 下面按**报错原文**查。

### A. `Invalid index value for argument "index"`

**这个报错跟「索引越界」没关系** —— 是 `pyorbbecsdk` 的 `DeviceList`
迭代器有个 off-by-one: `for dev in devices` 第一轮正常、第二轮就抛。

```python
# 错的
for dev in devices: ...

# 对的
for i in range(len(devices)):
    dev = devices[i]
```

或者直接用 SDK 提供的 `devices.get_device_by_serial_number(sn)`。

`camera.py` 现在统一走后者。

### B. `usbEnumerator openUsbDevice failed!` / `uvc_open failed -6`

**相机被别的进程占着**。最常见的是 GNOME 的 `gvfsd-gphoto2`
(自动挂载相机的守护进程), 它会在背后悄悄占着 USB 设备节点。

```bash
pkill -f gvfsd-gphoto2

# 查是谁占着
fuser -v /dev/bus/usb/001/*
```

### C. `Usb pal is not exist, please check the build config that you have enabled BUILD_USB_PAL`

pip 装的 `pyorbbecsdk` 自带的 `libOrbbecSDK.so` **没开 USB PAL**。
必须用源码编译 (`-DOB_BUILD_USB_PAL=ON`) 的那份, 见
[sdk/README.md](../sdk/README.md)。

```bash
# 确认编译产物里确实有
nm -C sdk/orbbec/lib/libOrbbecSDK.so.2.10.6 | grep LinuxUsbPal
```

### D. `No matched profile found for: {type: Color, format: RGB, width: ..., height: ..., fps: ...}`

相机不支持这个分辨率/帧率组合。查这台相机到底支持哪些:

```bash
python -c "
import pyorbbecsdk as ob
d = ob.Context().query_devices()
s = d[0].get_sensor(ob.OBSensorType.COLOR_SENSOR)
l = s.get_stream_profile_list()
for i in range(l.get_count()):
    p = l.get_stream_profile_by_index(i)
    print(p.get_width(), 'x', p.get_height(), '@', p.get_fps(), p.get_format())
"
```

> **坑**: 同一个分辨率不同格式的支持情况不一样。Gemini 336L 的
> `1280x800` 只支持 **MJPG** (而且只有 8fps), `640x400` 才支持原生 RGB。

### E. `没有找到 Orbbec 相机 (但 USB 层能看到设备)`

内核认到了但 SDK 枚举不到 —— 依次查:

1. 相机被占 (见 B)
2. SDK 没开 USB PAL (见 C)
3. 相机刚插上还没就绪 —— 拔插一次

### F. `没有找到 Orbbec 相机 (USB 层也看不到设备)`

**物理连接问题** —— 线松了 / USB 口供电不足 / 相机掉电了。

```bash
lsusb | grep 2bc5     # 应该能看到 Orbbec Gemini 336L
```

### G. 标定内参跟当前流对不上

```
标定内参 1280x800 与当前彩色流 640x480 宽高比不符, 不用它
```

内参是跟着分辨率走的, 但**宽高比必须一致**才能按比例缩放复用。
`640x480` (1.33) 和 `1280x800` (1.6) 比例不同, 缩放会引入形变,
所以只能用 SDK 标称内参。

想要用上标定精度, 就把彩色流设成**同样 1.6 比例**的分辨率:

```yaml
camera:
  width: 640      # 640x400 = 1.6, 与 1280x800 同比例
  height: 400
  fps: 30
```

代码会自动把标定内参缩放到 640x400 (`fx 605.78 -> 302.89`)。

---

## 验证

```bash
cd services/vision_service && python3 tools/check_vision.py
cd services/agent_service  && python3 tools/demo_skill_workflow.py
```

相机故障时，Skill 会通过 TTS 播报具体原因，见 [skill-framework.md](./skill-framework.md)。
