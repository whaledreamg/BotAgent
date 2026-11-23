# sdk/ —— 第三方 SDK 的编译产物

放相机 SDK 的编译产物（共享库），让视觉服务能直接找到它们，**不用依赖
系统安装或 pip 包自带的那份**。

**目录本身入库，实际的 `.so` 文件不入库**。

## 为什么需要这个

pip 装的 `pyorbbecsdk` 自带的 `libOrbbecSDK.so` 是**没开 USB PAL**
编译的 —— 枚举 Orbbec 相机时直接返回 0 个设备，报：

```
Usb pal is not exist, please check the build config that you have enabled BUILD_USB_PAL
```

必须用**开了 `OB_BUILD_USB_PAL=ON` 源码编译**的那份。

视觉服务启动时会自动把这里的库目录挂到 `LD_LIBRARY_PATH` **最前面**
（`DT_RUNPATH` 优先级低于 `LD_LIBRARY_PATH`，所以能覆盖掉 pip 自带的那份）。

## 结构

```
sdk/
└── orbbec/
    └── lib -> /path/to/OrbbecSDK_v2/build/linux_x86_64/lib
        ├── libOrbbecSDK.so
        ├── libOrbbecSDK.so.2
        └── libOrbbecSDK.so.2.10.6
```

## 怎么接进来

### 方式 A：软链接（推荐，编译产物还留在 SDK 源码树里）

```bash
cd sdk/orbbec
ln -sfn /path/to/OrbbecSDK_v2/build/linux_x86_64/lib lib
```

### 方式 B：环境变量指到别处

```bash
export VISION_SDK_DIR=/path/to/OrbbecSDK_v2/build/linux_x86_64
# 或精确到 lib 目录
export VISION_SDK_LIB_DIR=/path/to/OrbbecSDK_v2/build/linux_x86_64/lib
```

### 自己编译一份

```bash
cd /path/to/OrbbecSDK_v2
cmake -B build -S . -DOB_BUILD_USB_PAL=ON -DOB_BUILD_NET_PAL=ON
cmake --build build -j$(nproc)
```

确认 USB PAL 真的编进去了：

```bash
nm -C build/linux_x86_64/lib/libOrbbecSDK.so.2.10.6 | grep LinuxUsbPal
```

有输出就对了。

## 检查是否就位

```bash
cd services/vision_service && python -c "from paths import describe; print(describe())"
# {'model_dir': '...', 'sdk_lib_dir': '...', 'sdk_found': True}
```

## udev 规则

相机是 USB 独占设备，还需要 udev 规则（否则普通用户打不开设备节点）：

```bash
sudo cp /path/to/OrbbecSDK_v2/scripts/env_setup/99-obsensor-libusb.rules /etc/udev/rules.d/
sudo udevadm trigger && sudo udevadm settle
```

规则里已含 Gemini 336L 的 `0807` 条目。

> **注意**：GNOME 的 `gvfsd-gphoto2` 会自动抢占相机，导致 SDK 报
> `usbEnumerator openUsbDevice failed!`。遇到这个错误先把它杀掉：
> ```bash
> pkill -f gvfsd-gphoto2
> ```
