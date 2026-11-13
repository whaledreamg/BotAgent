# 环境部署指南

新机器上从零搭起四个 conda 环境。

## 为什么不用 `conda env export` 的 yml

以前每个服务目录里各有一份 `conda env export` 出来的 `*.yml`（现备份在 `envs/legacy/`）。
换机器时 `conda env create` **必然失败**，原因有三类：

### 1. `prefix` 写死了绝对路径

```yaml
prefix: /home/gxh/miniconda3/envs/asrt
```

新机器上这个路径不存在，conda 会尝试创建 `/home/gxh/miniconda3/envs/...`，
要么权限报错，要么装到意外位置。

### 2. conda build 号是平台相关的

```yaml
- bzip2=1.0.8=h5eee18b_6
- libgcc=15.2.0=h69a1729_8
- python=3.10.22=hd9cb797_1
```

build 号（`h5eee18b_6` 这种）取决于编译器/glibc 版本。
不同机器可用的 build 号集合不同，精确匹配在新机器上可能无解 → `PackagesNotFoundError`。

### 3. pip 的 local version 在 PyPI 上不存在

TTS 环境里：

```yaml
- torch==2.6.0+cu124
- torchaudio==2.6.0+cu124
- torchvision==0.21.0+cu124
```

`+cu124` 是 **local version identifier**，只有 NVIDIA 官方索引
`https://download.pytorch.org/whl/cu124` 认。PyPI 上**没有**这个版本：

```
$ pip download "torch==2.6.0+cu124"
ERROR: No matching distribution found for torch==2.6.0+cu124
```

而 `conda env create` 生成的 pip 段是**纯 `pip install`**，不带 `--index-url` → 必挂。

### 结论

`conda env export` 产出的是「**这台机器的快照**」，不是「**可移植的安装清单**」。
所以本项目改成：

- **conda** 只负责 python 本体
- **pip** 只负责纯 PyPI 包（`requirements/*.txt`，手写）
- **torch** 单独用 pytorch 官方索引装

---

## 快速开始

```bash
# 建全部四个环境
./scripts/setup_envs.sh

# 只建某一个
./scripts/setup_envs.sh asrt
./scripts/setup_envs.sh vision
```

脚本是**幂等**的：环境已存在就跳过创建，不会动已有环境。

---

## 环境与服务对应

| 环境 | Python | 服务 | 依赖清单 |
| --- | --- | --- | --- |
| `asrt` | 3.10 | ASR (`:8801`) | `requirements/asr.txt` |
| `botagent` | 3.11 | Agent (`:8800`) + Arm (`:8802`) | `requirements/agent.txt` |
| `chattts` | 3.11 | TTS (`:8000`) | `requirements/tts.txt` |
| `yolo` | 3.11 | Vision (`:8803`) | `requirements/vision.txt` |

> Agent 和 Arm **共用** `botagent` 环境，见 `scripts/start_agent.sh`、`scripts/start_arm.sh`。

---

## 三个必须知道的安装顺序问题

### ① TTS：torch 要先装，且不能带 `+cu124`

```bash
pip install torch==2.6.0 torchaudio==2.6.0 torchvision==0.21.0 \
    --index-url https://download.pytorch.org/whl/cu124
```

- **不带 local version**：`torch==2.6.0` 是 PyPI 语义，`torch==2.6.0+cu124` 不是。
  CUDA 版本由**索引**决定，不由版本号决定。
- **先装 torch**：`vllm` / `xformers` 这类包会要求 `torch==2.1.2` 精确匹配，
  跟 torch 2.6 一起进 resolver 会死锁。本项目 `configs/tts.yaml` 的 `use_vllm`
  默认关闭，**这两个包根本不需要**，已从依赖清单里删掉。

换 CUDA 版本：

```bash
PYTORCH_INDEX=https://download.pytorch.org/whl/cu126 ./scripts/setup_envs.sh tts
```

### ② Vision：numpy 的三方冲突

`pyorbbecsdk2==2.1.3` 要求 `numpy<2.0.0`（Python < 3.12 时），
而 `opencv-python 5.x` 要求 `numpy>=2` —— **两者同时进 pip resolver 必然失败**。

`setup_envs.sh` 的解法是**分两步**：

```bash
# 先装 opencv + numpy
pip install torch==2.15.0 torchvision==0.29.2 numpy==2.4.6 \
    opencv-python==4.14.0.94 ultralytics==8.4.208 ultralytics-thop==2.2.5
# 再用 --no-deps 装 pyorbbecsdk2, 绕开它的 numpy 约束
pip install --no-deps pyorbbecsdk2==2.1.3
```

顺带把 `opencv-python` 从 5.x 降到 **4.14.0.94**（4.x 对 numpy 1.x/2.x 都兼容），
换机器时不用再赌 resolver 的心情。

### ③ ASR：vosk 必须显式声明

`vosk` 之前只装在 **user-site**（`~/.local/lib/python3.10/site-packages`），
不在 conda 环境里，`conda env export` 因此把它漏掉了。
代码里是硬依赖：

```26:26:services/asr_service/wakeup_asr.py
from vosk import KaldiRecognizer, Model, SetLogLevel
```

换机器后 ASR 服务**启动即 `ModuleNotFoundError`**。现在已写进 `requirements/asr.txt`。

---

## pip 装不上的东西（必须手工做）

### 1. Vosk 中文模型

不在任何 pip 包里，需要单独下载解压到 `~/.cache/vosk/`。
`configs/pipeline.yaml` 的 `model_path` 留空时会自动在这里找。

### 2. Orbbec SDK（Vision 必需）

`pyorbbecsdk2` 的 wheel **没有编译 USB PAL**，直接用会报：

```
Usb pal is not exist, please check the build config that you have enabled BUILD_USB_PAL
```

必须从源码编译：

```bash
cd <OrbbecSDK_v2>
mkdir -p build && cd build
cmake .. -DOB_BUILD_USB_PAL=ON -DOB_BUILD_NET_PAL=ON
cmake --build . -j$(nproc)
```

运行时让 Python binding 加载编出来的库：

```bash
export LD_LIBRARY_PATH=<OrbbecSDK_v2>/build/linux_x86_64/lib:$LD_LIBRARY_PATH
```

`scripts/start_vision.sh` 会自动在几个常见位置探测；也可以手动指定：

```bash
ORBBEC_LIB=/path/to/OrbbecSDK_v2/build/linux_x86_64/lib ./scripts/start_vision.sh
```

详见 [vision-service.md](vision-service.md)。

### 3. udev 规则

```bash
sudo cp <OrbbecSDK_v2>/scripts/env_setup/99-obsensor-libusb.rules /etc/udev/rules.d/
sudo udevadm trigger && sudo udevadm settle
```

### 4. 系统库

```bash
sudo apt install -y portaudio19-dev libsndfile1
```

- `portaudio19-dev` —— PyAudio 编译必需
- `libsndfile1` —— soundfile 运行时必需

### 5. 密钥

```bash
cp configs/.env.example configs/.env   # 填 LLM / API key
```

---

## 验证

```bash
# 环境都建好了吗
conda env list | grep -E "asrt|chattts|botagent|yolo"

# 各自的核心 import 能通
conda run -n asrt      python -c "import vosk, numpy, sounddevice; print('asrt OK')"
conda run -n botagent  python -c "import fastapi, httpx, pydantic; print('agent OK')"
conda run -n chattts   python -c "import torch, pynini, transformers; print('tts OK', torch.__version__)"
conda run -n yolo      python -c "import torch, ultralytics, pyorbbecsdk; print('vision OK')"
```

---

## 备份：旧的 yml

`envs/legacy/` 里是原来那四份 `conda env export` 的产物，仅作参考，**不要再用它们建环境**：

```
envs/legacy/asrt.yml
envs/legacy/botagent.yml
envs/legacy/yolo.yml
```

（`chattts.yml` 因为在 `services/tts_service` 这个 git submodule 里，
不能移出子模块，故保留原处。它同样不要再用于建环境。）

---

## 参考：被删掉的包及原因

| 包 | 原属环境 | 删除原因 |
| --- | --- | --- |
| `vllm` | chattts | 要求 `torch==2.1.2`，与 torch 2.6 死锁；`use_vllm` 默认关闭 |
| `xformers` | chattts | 同上 |
| `cuda-toolkit` | chattts/yolo | ~3GB 全量 CUDA meta 包，torch 依赖自带运行时 |
| `nvidia-*-cu12` / `nvidia-*-cu13` | chattts/yolo | 两套 CUDA 栈混装；由 torch 自动拉取 |
| `tensorflow-gpu` | asrt | **PyPI 不存在**（只有 `tensorflow`），且 Python 3.10 装不了 TF 2.8 |
| `keras` / `tensorboard` | asrt | TF 的依赖，ASR 用不到 |
| `asyncio` / `wave` | asrt | stdlib 回溯包，官方明确说不要装 |
| `rclpy` / `ament-*` / `rqt-*` | asrt | ROS Humble 污染，与 ASR 无关 |
| `asrt-sdk` / `xrobotoolkit-sdk` / `bc-stark-sdk` | asrt | 机械臂 SDK，与 ASR 无关 |
| `gradio` / `ipython` / `ray` | chattts | 交互/分布式用不到 |
| `polars` / `polars-runtime-32` | yolo | ultralytics 的传递依赖，交给 pip 自己解 |
