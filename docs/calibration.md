# 标定指南 (视觉服务)

相机在机器人身上, 要把「看到的物体」变成「机械臂能去的位置」, 中间必须
过一道**标定**。这份文档讲清楚:

1. 有哪几个坐标系, 分别是什么含义
2. 内参标定 (重标相机的出厂内参)
3. 手眼标定 (求相机到机械臂的相对关系)
4. 怎么验证标定对不对 —— **尤其是左右手性**
5. 常见坑

---

## 目录

- [1. 坐标系](#1-坐标系)
  - [1.1 坐标树总览](#11-坐标树总览)
  - [1.2 世界系到底是谁给的](#12-世界系到底是谁给的)
  - [1.3 三个变换公式](#13-三个变换公式)
  - [1.4 各系的轴向约定](#14-各系的轴向约定)
- [2. 为什么要标定](#2-为什么要标定)
- [3. 内参标定](#3-内参标定)
- [4. 手眼标定](#4-手眼标定)
  - [4.2.1 板固定在哪只手上?](#421-板固定在哪只手上)
  - [4.3.1 采样会存下原始图](#431-采样会存下原始图--之后可以不动臂重解)
  - [4.3.2 离线重解 (不动机械臂)](#432-离线重解-不动机械臂)
  - [4.3.3 看图挑坏帧](#433-看图挑坏帧)
- [5. 验证标定](#5-验证标定)
- [6. 写回配置](#6-写回配置)
- [7. 常见坑](#7-常见坑)
  - [坑 12: 剔野值后只剩 0 个采样 (工具偏置被静默忽略)](#坑-12-剔野值后只剩-0-个采样--工具偏置文件不存在-被静默忽略了)
- [8. 工具清单](#8-工具清单)

---

## 1. 坐标系

### 1.1 坐标树总览

```
                                world  (大地系, 控制器定义)
                                │  ← 机器人站立的那个坐标系
                                │
            ┌───────────────────┼───────────────────┐
            │                   │                   │
      T_world_head      T_world_base_left  T_world_base_right
       (头/相机座)         (左臂基座)          (右臂基座)
            │                   │                   │
            │ ① 静态标定量      │ ③ 运行时拿        │ ③ 同上
            │ (头会动时)        │                   │
        ┌───▼───┐               │                   │
        │ head  │               │                   │
        └───┬───┘               │                   │
            │ ② 静态标定量      │                   │
        ┌───▼───┐               │                   │
        │ camera│               │                   │
        └───────┘               │                   │
                                │                   │
                         tool (左臂夹爪)     tool (右臂夹爪)
                                ↑ 标定时板固定在这里
```

> **图例**: ① ② 是**标定出来的静态量**; ③ 是**运行时从控制器拿的实时值**。

**先记住三件事**, 后面的公式就都不难理解了:

1. **`world` 系不是我们定义的** —— 它由机器人控制器定义 (见
   [1.2 世界系到底是谁给的](#12-世界系到底是谁给的))。我们只是**消费**它。
2. **标定只有一个产物** —— `T_world_cam` (相机 → 世界)。是静态量。
3. **左右臂的外参不是分别标出来的** —— 是从同一个 `T_world_cam` **推**出来的,
   差别只在 `T_world_base` (运行时从控制器拿)。

### 1.2 世界系到底是谁给的

来源链条 (在 `arm_service` 里, 我们没做任何坐标系变换, 只是把控制器的
6 个数 `{x,y,z,a,b,c}` 转成 4x4 矩阵):

```
控制器 robot/status 推送
    ├─ cartesian      → TCP 世界系位姿      → T_world_tool
    ├─ baseFrame      → 臂基座世界系位姿   → T_world_base_left / _right
    ├─ baseFrameKind  → static / derived
    └─ headFrame      → 头座世界系位姿     → T_world_head
```

对应代码 `services/arm_service/service.py` 的 `_record_kinematics()`,
以及 `services/vision_service/extrinsic.py` 的 `rpy_to_matrix()`
(用 **ZYX 内旋**, 与控制器 `cartesian.a/b/c` 的语义一致)。

**各变换矩阵的来源**:

| 符号 | 含义 | 从哪来 | 静态/动态 |
|------|------|--------|-----------|
| `T_world_tool` | TCP 世界系位姿 | 控制器 `cartesian` | 运行时 |
| `T_tool_board` | 板相对夹爪的偏置 | 你量出来的, 默认单位阵 | 静态 |
| `T_board_cam` | 板在相机系的位姿 | `solvePnP` 从角点解出 | 每次采样 |
| **`T_world_cam`** | **相机 → 世界** | **标定解出来** | **静态** |
| `T_world_base` | 臂基座 → 世界 | 控制器 `baseFrame` | 运行时 |
| `T_world_head` | 头座 → 世界 | 控制器 `headFrame` | 运行时 |

### 1.3 三个变换公式

**① 标定** (解 `T_world_cam`, 每个采样位姿一个):

```
T_world_cam = T_world_tool(i) · T_tool_board · T_board_cam(i)
```

20~30 个位姿各解一个, 取稳健平均 → 标定结果。

**② 运行时: 相机 → 世界**

```
T_world_cam  = 静态矩阵 (标定结果)
```

头会动的话标 `T_head_cam`, 运行时补 `T_world_head · T_head_cam`
(见 [4.6 头会动怎么办](#46-头会动怎么办))。

**③ 运行时: 相机 → 某条臂的基座系**

```
T_left_cam  = (T_world_base_left)⁻¹  · T_world_cam
T_right_cam = (T_world_base_right)⁻¹ · T_world_cam
```

**这就是为什么两条臂不用分别标定** —— 差异全在 `T_world_base` 里,
而它是运行时从控制器拿的实时值。

### 1.4 各系的轴向约定

```
camera (OpenCV)              base (ROS/URDF)            world
    Z (前,光轴)                  Z (上)                控制器定义
    |                            |                    (通常同 base)
    |   Y (下)                   |   X (前)
    |  /                        |  /
    +----> X (右)               +----> Y (左)
```

> `world` 系的轴向**由控制器说了算** —— 代码里对 `world` 不做任何轴置换,
> 置换只发生在 camera ↔ base 之间, 且默认把置换**烘进标定矩阵**
> (`flip_axes: false`)。

### 相机系 (OpenCV 约定)

```
        Z (前, 光轴方向)
        |
        |      Y (下)
        |     /
        +----> X (右)
```

- 原点在相机光心
- Z 沿光轴**向前**
- X 向**右**, Y 向**下**

### 机械臂基座系 (ROS / URDF 约定)

```
        Z (上)
        |
        |      X (前)
        |     /
        +----> Y (左)
```

- 原点在臂基座的安装面
- X 向前, Y 向左, Z 向上

### 两者之间的换算

这是一个**轴置换** (行列式 +1, 是旋转不是镜像):

```
M = [[ 0,  0,  1],
     [-1,  0,  0],
     [ 0, -1,  0]]
```

验算:

| 相机轴 | 方向 | 变成基座系 |
|--------|------|------------|
| Z (前) | 光轴前方 | `+X` (前) |
| X (右) | 画面右侧 | `-Y` (右) |
| Y (下) | 画面下方 | `-Z` (下) |

> **历史包袱**: 老版本的 `flip_axes` 开关做的是 `(-x, -y, z)` —— 那是**绕
> 光轴转 180°**, 跟注释里写的「X前 Y左 Z上」根本不是一回事。现已改成
> 正确的轴置换。**推荐做法**: 把置换直接烘进 `extrinsic.matrix`, 然后把
> `flip_axes` 置 `false` —— 一切都在矩阵里, 不会有隐式变换把结果带偏。

### 头/相机安装座系 (`head`)

相机实际**安装的那个座** (机器人头部, 或固定支架)。相机相对它是刚性的。

只有头会动时才需要这个系 —— 见 [4.6 头会动怎么办](#46-头会动怎么办)。

### 世界系 (`world`)

机器人站立的那个大地坐标系 —— **由控制器定义**, 不是我们定的。

相机到世界系的关系是:

```
T_world_cam = T_world_base · T_base_cam
```

- `T_base_cam` — 标定出来的静态量
- `T_world_base` — 运行时从控制器拿 (基座会随升降柱升降、挂接安装而动)

### 双臂的基座系 (`left_base` / `right_base`)

**双臂各有一个基座系**, 两者在世界里是**不同的位置**。相机在躯干上,
所以「相机 → 左臂基座」和「相机 → 右臂基座」是**两个不同的矩阵**:

```
T_left_cam  = (T_world_base_left)⁻¹  · T_world_cam
T_right_cam = (T_world_base_right)⁻¹ · T_world_cam
```

差的那一项由控制器给 (`arm_service /v1/kinematics` 的 `base_frame`),
**你不用自己填**。

> **注意**: `world` 和 `base` 不一定是同一个东西 —— 取决于你的机器人:
>
> | 情况 | `T_world_base` | 静态外参成立吗 |
> |------|----------------|--------------|
> | 臂基座焊死在躯干上, 躯干固定不动 | 单位阵 (或常量) | ✅ |
> | 臂装在升降柱顶端 | 随升降变化 | ✅ (相机和臂一起动) |
> | 臂挂接在会动的机器人上 (`derived`) | 实时变化 | ✅, 但**必须**开 `dynamic: true` |
>
> `base_frame_kind: "static"` 表示这个基座位姿是**配置量** (控制器里的常量);
> `"derived"` 表示基座本身在动 —— 静态外参依然成立 (相机到臂基座的相对关系
> 没变), 但臂基座到世界系的关系会变, 必须用实时 `base_frame` 做补偿。

### 该用哪个?

| 场景 | 用哪个 | 外参形式 |
|------|--------|---------|
| 只是想知道「离相机多远」 | `camera` | 不需要外参 |
| 相机固定在躯干, 头固定 | `world` | 静态外参 (`matrix_frame=world`) |
| 双臂, 要给左臂/右臂下抓取指令 | `left_base` / `right_base` | 静态矩阵 + 运行时基座位姿 |
| 相机装在**会动的头**上 | `world` | `matrix_frame=head` + 运行时头位姿 |

> **配 `left_base` / `right_base` / `arm_base` 时必须开 `dynamic: true`** ——
> 运行时要从 `arm_service` 拿基座位姿。

### 调试: 看所有系里的同一个点

```bash
curl -s http://127.0.0.1:8803/v1/frames | python3 -m json.tool
```

一次给出同一个点在所有系里的坐标, 外加 `sources` 字段说明每个系
「是怎么来的」 (`static_matrix` / `base_frame:left` / `head_frame·head_matrix` …)。

**变换不了的系会进 `unavailable`** —— 宁可少给一个系, 也不给错的。

---

## 2. 为什么要标定

| 误差源 | 量级 | 在 1.5 m 处的影响 |
|--------|------|------------------|
| SDK 标称内参 (差 1~2%) | 1~2% | 15~30 mm 横向 |
| 手眼外参 (不标) | 100% | 完全没法用 |
| 板子打印缩放 (2%) | 2% | 30 mm |
| TCP 位姿读数 | ~1 mm | 1 mm |

**结论**: 内参不标, 外参标得再准也没用 —— 两者的误差量级一样。

---

## 3. 内参标定

### 3.1 生成标定板

```bash
cd services/vision_service
conda run -n yolo python tools/make_board.py \
    --square 0.040 --marker 0.030 --cols 7 --rows 5 \
    --out /tmp/charuco_board.png
```

参数:

| 参数 | 含义 | 建议 |
|------|------|------|
| `--square` | 棋盘格边长 (米) | 0.03~0.05 m (太小了在 1 m 外角点定位不稳) |
| `--marker` | ArUco 标记边长 | 约 `square` 的 0.7 倍 |
| `--cols` / `--rows` | 角点列数 / 行数 | 7×5 或 9×6 (角点越多越准) |
| `--dict` | 字典 | `DICT_4X4_50` 够用 |

### 3.2 打印 (关键!)

1. **选 100% 原始尺寸打印** —— 不要「适应页面」「缩放以适合」
2. 打完**用尺子量相邻角点的距离**
3. 量出来不是 40.0 mm 就改 `--square` 重新生成
4. 把实际值填进 `configs/calibration.yaml` 的 `board.square_length`
5. 贴在**硬板子**上 —— 纸贴桌面会有褶皱, 角点定位会偏

> 打印缩放 2% 在 1.5 m 处就是 30 mm —— 跟整个标定的误差预算同量级。

### 3.3 采集

```bash
conda run --no-capture-output -n yolo python tools/calib_intrinsics.py --capture
```

每按一次回车拍一张。**拍摄要点**:

- 在**实际工作距离** (0.5~2.0 m) 拍
- 板要覆盖画面**不同区域**: 中心、四角、边缘都来几张
- 板要有**不同倾角**: 正面、左右倾 20~45°、上下倾 20~45°
- 至少 **20 张**, 建议 25~30 张
- 板**完整可见** (至少 6 个角点, 最好 15+)
- 光照均匀, 不要强反光

脚本会跳过检出角点太少的图。

### 3.4 结果

```bash
# 结果在 configs/intrinsics.json
conda run -n yolo python tools/apply_calibration.py --intrinsics
```

会写进 `configs/vision.yaml` 的 `camera.calibrated_intrinsics`。

> **内参是跟着分辨率走的** —— 改了彩色流分辨率就必须重标。
> 服务启动时会检查分辨率对不对, 对不上会拒绝使用并打日志。

---

## 4. 手眼标定

### 4.1 为什么板要挂在工具上

控制器给的世界系位姿 (`robot/status.cartesian`) 精度约 1 mm —— 那是我们
唯一的「量尺」。

- **板挂工具**: 板的世界位姿 = 控制器正解 → 毫米级。误差只剩「板相对
  工具的物理安装偏置」, 用销钉/打印模板对齐可以做到 < 1 mm。
- **板放桌上**: 板的世界位姿靠 TCP 触碰确定, 重复性只有 2~5 mm, 直接进
  外参就把误差预算吃光了。

### 4.2 前置条件

1. 内参已标定 (`tools/calib_intrinsics.py`)
2. 板**固定在夹爪上** (代码里叫「工具」/ tool = 末端执行器 = 夹爪)
3. 工具坐标系的**物理尖点**与板原点重合 (或用 `--tool-offset` 填偏置)
4. `arm_service` 在跑 (它提供控制器的世界系位姿)

> **重要**: 相机装在机器人**头部**时, 如果头部可以转动, 静态外参**不成立**。
> 先确认头部是固定的再往下做。

> **重要**: `--tool-offset` 指向的文件如果不存在, 脚本会**直接报错**。
> 这是故意的 —— 以前它会静默用单位阵, 导致「板原点 = 工具原点」这个
> 错误假设悄悄生效, 最后报「剔野值后只剩 0 个采样」让人一头雾水。
> 详见 [坑 12](#坑-12-剔野值后只剩-0-个采样--工具偏置文件不存在-被静默忽略了)。
>
> 不确定偏置是多少? 直接**不用** `--tool-offset` 参数 ——
> 常规解算失败时脚本会自动尝试**联合解算**, 把偏置和外参一起解出来,
> 并把结果存成 `configs/tool_board.json`。

### 4.2.1 板固定在哪只手上?

**这是眼在手外 (eye-to-hand) 标定 —— 标出来的是 `T_world_cam` (相机 → 世界),
跟你用哪只手无关。**

看解算公式就知道 (`solve_handeye()` 的签名里根本没有 arm 参数):

```
T_world_cam = T_world_tool(i) · T_tool_board · T_board_cam(i)
```

`T_world_tool(i)` 是控制器给的 **TCP 世界系位姿**, 已经是世界系了。臂名只
当备注存进 `PoseSample.label`, **不参与任何计算**。

所以真正的要求只有一条:

> **板固定在某一个夹爪上, 整个采样过程不换手。**

- 换夹爪 → `T_tool_board` 变了 → 外参有偏
- 采样中途换手 → 同上

**两条臂都要用怎么办?** 标**一次**就够 (`matrix_frame: "world"`),
运行时自动换算到左/右臂基座:

```
T_left_cam  = (T_world_base_left)⁻¹  · T_world_cam
T_right_cam = (T_world_base_right)⁻¹ · T_world_cam
```

> **但 `--arm` 要显式指定, 别用自动。** 两处「自动选」的行为**不一致**:
>
> | 位置 | 行为 |
> |------|------|
> | 采样时 (`capture.py` 的 `pick_arm`) | 取第一条有 `cartesian` 的 —— **依赖 dict 顺序, 不确定** |
> | 运行时 (`extrinsic.py` 的 `_base_frame_for`) | **优先 left**, 其次 right |
>
> 采样用了哪条臂不确定, 运行时又固定优先左臂 → 对不上。所以要显式写:
>
> ```bash
> conda run -n yolo python tools/calib_handeye.py --capture --arm left
> ```
>
> 你的控制器里两条臂叫 `LeftArm` / `RightArm`, `--arm left` 能匹配上。

**想让两条臂各标一次?** 技术上可以 (用 `--out` 指定不同输出文件避免覆盖),
但两次解出来的 `T_world_cam` **理论上应该相同**。差异只有两个来源:
`T_tool_board` 不同 (板装在不同夹爪) 和各自的随机误差。

**这反而是个有用的交叉验证**: 两个结果差几毫米 → 说明板装夹、TCP 读数、
内参都靠谱; 差几厘米 → 说明某个环节有问题。差异超过 10 mm 就该查了。

> **注意**: `configs/handeye.json` 是固定文件名, 第二次标定会**覆盖**第一次。
> 要两份都留着就显式指定:
>
> ```bash
> conda run -n yolo python tools/calib_handeye.py --capture --arm right \
>     --out configs/handeye_right.json \
>     --samples-out /tmp/handeye_right_samples.json
> ```
>
> **但要注意**: 当前 `apply_calibration.py` 写回时会强制设
> `matrix_frame=world` + `target_frame=world`, 所以「两次标定分别写回成不同
> 坐标系」这条路**当前走不通** —— 配置里只有单个 `extrinsic.matrix`,
> 没有 per-arm 结构。

### 4.3 采集

```bash
# ⚠️ 必须加 --no-capture-output (或不用 conda run), 否则 stdin 会被切断
conda run --no-capture-output -n yolo python tools/calib_handeye.py --capture --arm left
```

> ### ⚠️ `conda run` 会切断 stdin
>
> `conda run` 默认捕获输出, 子进程的 stdin 会变成 **EOF**。于是脚本第一句
> `input()` 就抛 `EOFError`, 表现为:
>
> ```
> [1/5] 把机械臂摆到新位姿 (姿态要多样), 按回车采样, 输入 q 退出
> 没采到任何样本          <- 一秒都没停, 因为 input() 直接 EOF 了
> ```
>
> **看起来像相机/机械臂有问题, 其实不是** —— 是 stdin 的问题。
>
> 两种解法:
>
> ```bash
> # 1. 加 --no-capture-output (推荐)
> conda run --no-capture-output -n yolo python tools/calib_handeye.py --capture
>
> # 2. 直接用环境里的 python, 不套 conda run
> /home/gxh/miniconda3/envs/yolo/bin/python tools/calib_handeye.py --capture
> ```
>
> 脚本会**在开相机之前**检查 stdin, 不可交互时直接给出这段提示并退出。

- `--arm left|right|<控制器里的机器人名>` — 用哪条臂。**显式指定**, 理由见
  [4.2.1 板固定在哪只手上?](#421-板固定在哪只手上)
- 每个位姿按一次回车采一个样
- 脚本会**等机械臂停稳**再采 (连续两次快照位置差 < 2 mm)

**采样要点**:

- 20~30 个位姿
- 姿态要**多样**: 三个轴各 ±30° 以上
- 不要都在一个平面附近 —— 旋转不够多样, 解不出某个旋转轴
- 避开: 7 轴冗余导致的肘部翻转、奇异位形、关节软限位附近
- 板与相机的距离要在深度相机有效范围内 (Gemini 336L 约 0.2 m 起)
- **全程用同一条臂、同一个夹爪**, 中途不换手

### 4.3.1 采样会存下原始图 —— 之后可以不动臂重解

采样时除了角点和位姿, 还会把**每一帧的原始彩色图**存下来:

```
/tmp/handeye_samples.json     <- 采样记录 (含 meta)
/tmp/images/0000.png          <- 第 1 帧的原始彩色图
/tmp/images/0001.png
...
```

> **为什么存图**: 标定最花时间的不是解算, 是「摆位姿 -> 拍照 -> 发现这帧
> 是坏的 -> 重摆」。存了图之后, 后面任何时候都能**不动机械臂、不再拍照**
> 地重新解算: 改了 `T_tool_board` 重新算、换一批内参重新精化、剔除几个
> 坏样本再算。

**不想存图** (省磁盘, 但之后不能重新检测角点):

```bash
conda run -n yolo python tools/calib_handeye.py --capture --no-images
```

### 4.3.2 离线重解 (不动机械臂)

```bash
# 用采样时存下的角点直接重解
conda run -n yolo python tools/calib_handeye.py --samples /tmp/handeye_samples.json

# 从保存的图片**重新检测角点**再解 (换了检测参数时用)
conda run -n yolo python tools/calib_handeye.py --samples /tmp/handeye_samples.json --redetect

# 换一个工具偏置重解 (比如发现 T_tool_board 填反了)
conda run -n yolo python tools/calib_handeye.py --samples /tmp/handeye_samples.json \
    --tool-offset /tmp/tool_offset.json

# 剔除坏帧
conda run -n yolo python tools/calib_handeye.py --samples /tmp/handeye_samples.json \
    --exclude 3,7,12
```

**这解决了标定最大的痛点**: `T_tool_board` 填错时, 旧流程要**全部重采**
(30 个位姿, 半小时起步)。现在改一个 JSON 重跑就行。

### 4.3.3 看图挑坏帧

采样时屏幕上只显示「检出 N 个角点」, 看不到板在画面里什么样 —— 板被挡了
一半? 角度太斜? 曝光过度? 全看不出来。

```bash
# 每一帧画上角点, 输出到 /tmp/calib_views/
conda run -n yolo python tools/calib_view.py --samples /tmp/handeye_samples.json

# 额外拼一张大图, 一眼看完所有帧
conda run -n yolo python tools/calib_view.py --samples /tmp/handeye_samples.json --montage

# 顺便用当前检测参数重检测一遍 (看哪些帧现在检不出来)
conda run -n yolo python tools/calib_view.py --samples /tmp/handeye_samples.json --redetect
```

每张图上有: 角点 (带编号)、板的包围框、板到相机的距离。

**挑出坏帧后**, 用 `--exclude` 剔掉再重解:

```bash
conda run -n yolo python tools/calib_handeye.py --samples /tmp/handeye_samples.json \
    --exclude 3,7
```

### 4.4 解算结果

```
参与解算的采样: 24
位置残差: 平均 4.2 mm, 最大 11.3 mm
位置 RMS: 6.15 mm (精化前 7.02 mm)
姿态残差: 平均 0.412°

外参矩阵 (相机系 -> 目标系):
   [ 0.999743, -0.022547,  0.007138,  0.012345]
   [ 0.022394,  0.999358, -0.028651,  -0.023456]
   [ 0.007825,  0.028538,  0.999563,  1.523456]
   [ 0.000000,  0.000000,  0.000000,  1.000000]
```

**验收标准**:

| 指标 | 阈值 | 说明 |
|------|------|------|
| 位置 RMS | < 10 mm | 1.5 m 工作距离下的实用要求 |
| 姿态残差 | < 1.0° | 1° 在 1 m 处就是 17 mm |

### 4.5 数学

每个采样位姿 i:

```
T_world_cam = T_world_tool(i) · T_tool_board · T_board_cam(i)
```

- `T_world_tool(i)` — 控制器给的 TCP 世界系位姿 (6D)
- `T_tool_board` — 板相对工具的固定偏置 (4x4), 标定前量好, 通常是单位阵
- `T_board_cam(i)` — 由 `solvePnP` 从板角点解出

多组位姿解出的 `T_world_cam` 取稳健平均 (位置取中位数, 旋转取「到其它
旋转角度和最小」的那个) → 外参。

**精化**: 用所有位姿上板角点的重投影误差做 Gauss-Newton 最小化, 只优化
外参的 6 个自由度。

> **精化必须用固定的内参**。边解外参边估内参, 两者会互相补偿 (内参偏大
> → 外参偏小), 结果是外参不准但残差好看。所以流程必须是: 先单独标内参,
> 再拿固定内参解外参。

**野值剔除**: 位置残差 > 20 mm 或姿态残差 > 5° 的采样会被剔除。

### 4.6 头会动怎么办

**标定出来的是 `T_world_cam` —— 一个「绝对量」**。它的隐含前提是:
*头在标定那一刻的位置, 之后不变*。

头一旦转了, `T_world_cam` 就过时了, 而且**不会报错** —— 只会默默给出
偏掉的坐标 (偏多少取决于头转了多少)。

#### 症状

标定很准, 但头转一下之后坐标就全错。

#### 方案 A: 改用「相机 → 头座」的相对标定量 (推荐)

把标定链路的末端从「世界」换成「头座」:

```
T_world_cam = T_world_head(q_head) · T_head_cam
```

`T_head_cam` 是相机相对头座的位姿 —— **不随头动而变**, 标一次一直有效。
运行时只要能拿到 `T_world_head(q_head)` 就能补上。

怎么标 `T_head_cam`:

1. 头转到某个**已知角度** θ (控制器给的头关节角)
2. 读 `T_world_head(θ)`
3. `T_head_cam = T_world_head(θ)⁻¹ · T_world_cam`
4. 换 2~3 个角度重复, 结果差异应 < 1 mm

配置:

```yaml
extrinsic:
  matrix_frame: "head"    # 矩阵描述的是「相机 -> 头座」
  target_frame: "world"   # 但对外输出世界系
  dynamic: true           # 运行时补 T_world_head
```

**注意**: 控制器目前**还没有确认**给不给头部位姿。`arm_service` 现在会
去认 `headFrame` / `headBaseFrame` / `headFramePose` 这几个字段名,
认不到就返回 `null` —— 此时服务会**如实退回相机系**, 而不是给出错的坐标。

#### 方案 B: 相机装到固定的地方

如果头会动但相机可以拆下来, 装到躯干上, 头动就完全不影响外参。

#### 检查配置对不对

```bash
curl -s http://127.0.0.1:8803/v1/calibration | python3 -m json.tool
```

会明确告诉你:

- `extrinsic.matrix_frame` — 标定量描述的是哪个系
- `extrinsic.head_compensation` — 有没有做头部补偿
- `extrinsic.head_frame_available` — 控制器给不给头位姿
- `extrinsic.warnings` — **最常见坑的直接提示**, 比如「标定矩阵是
  `T_world_cam` 但控制器给了头部位姿 —— 如果头会动, 改用
  `matrix_frame=head`」

---

## 5. 验证标定

**这一步不能省。**

```bash
conda run -n yolo python tools/calib_verify.py --samples /tmp/handeye_samples.json
```

### 5.1 手性检查 (最重要)

手眼标定最常见的翻车点是**外参看起来「很像是对的」** —— 距离和深度全对,
只有左右反了。这种错误在功能测试里很难发现, 一直到抓取时才暴露成
「抓到了旁边的东西」。

`calib_verify.py` 会检查:

1. **旋转部分行列式 = +1** —— 是旋转, 不是镜像。det = -1 说明手性反了
2. **旋转矩阵正交** (R^T R = I)
3. 打印相机各轴在目标系里的指向, 人工核对:
   - 相机前方应该跟机器人面朝的方向大致一致
   - 相机右方应该指向 `-Y` (基座系的 Y 向左)

### 5.2 采样一致性

用采的样本回代, 看每个位姿算出的 `T_world_cam` 是否都等于标定结果:

- 位置残差平均 < 10 mm
- 姿态残差平均 < 1.0°

### 5.3 实时验证 (最直观)

```bash
conda run --no-capture-output -n yolo python tools/calib_verify.py --live
```

**操作**: 在机器人**左侧**放一个物体, 看服务报出来的 Y 坐标符号。

- 基座系 Y 向左 → 物体在左边时 **Y 应该是负的**
- Y 是正的 → **左右反了** → 回去检查 `flip_axes` / 矩阵

---

## 6. 写回配置

```bash
# 内参 + 外参一起写
conda run -n yolo python tools/apply_calibration.py --all

# 先看看会改成什么 (不改文件)
conda run -n yolo python tools/apply_calibration.py --all --dry-run
```

脚本会:

1. 备份原配置 (`vision.yaml.bak.<时间戳>`)
2. 写入 `camera.calibrated_intrinsics` 和 `extrinsic.matrix`
3. 把 `extrinsic.enabled` 设为 `true`
4. 把 `extrinsic.flip_axes` 设为 `false` (置换已经烘进矩阵了)
5. 验证配置能正常加载

写回后**重启视觉服务**, 然后:

```bash
curl -s http://127.0.0.1:8803/v1/calibration | python3 -m json.tool
```

---

## 7. 常见坑

### 坑 1: 左右反了

**症状**: 距离和深度全对, 只有 X/Y 的符号不对。抓取时会抓到旁边的物体。

**原因**:
- `flip_axes` 用错了 (老版本那个 `(-x,-y,z)` 不是轴置换)
- 板子挂反了 (板装在工具上时转了 180°)
- 角点顺序反了

**排查**: `calib_verify.py` 的手性检查 + `--live` 实测。

### 坑 2: 距离差很多

**症状**: 报出来的距离系统性偏大/偏小。

**原因**:
- 板子打印缩放了 (最常见)
- 内参没标
- `flip_axes` 没做 (只影响 XY, 不影响 Z —— 所以如果 **Z 也错**, 就是内参或打印尺寸的问题)

### 坑 3: 旋转解不出来 / 解得很差

**症状**: 位置还行, 姿态残差很大。

**原因**:
- 采样位姿**不够多样** —— 都在一个平面附近, 解不出某个旋转轴
- 采样太少 (< 10 个)
- 采到了 7 轴冗余的「肘部翻转」位姿

**排查**: `calib_handeye.py` 的输出里会列出被剔除的野值采样索引, 重新采那些位姿。

### 坑 4: `uvc_open failed -6`

**症状**: 相机打不开。

**原因**: 相机是 USB **独占**设备 —— 视觉服务在跑的时候, 标定脚本再开一次就失败。

**解决**: 先停掉视觉服务, 或者用 `check_vision.py` 走 HTTP 路径。

### 坑 5: 相机装在可转动的头上

**症状**: 标定很准, 但换个头部姿态坐标就全错。**而且不会报错**。

**原因**: 静态外参的前提是「相机相对世界不动」。头部能转的话, 相机相对
世界就在动, 标定出来的 `T_world_cam` 过时了。

**解决**: 见 [4.6 头会动怎么办](#46-头会动怎么办) —— 改用
`matrix_frame=head` (相机 → 头座) + 运行时补 `T_world_head(q_head)`。

**自查**: `curl -s http://127.0.0.1:8803/v1/calibration` 看
`warnings` 里有没有相关提示。

### 坑 6: 双臂抓取用错了基座

**症状**: 左臂去抓右边的物体, 或者抓的时候位置整体偏 40 cm (双臂基座间距)。

**原因**: 目标系选成了 `arm_base` 但没配 `arm: left/right`, 或者请求的是
`left_base` 但 `dynamic: false` 拿不到基座位姿。

**解决**:

- 目标系用 `left_base` / `right_base` 明确指定
- `dynamic: true` (否则基座位姿拿不到, 会退回相机系)
- `GET /v1/frames` 看一眼 `sources` 字段确认用的是哪条臂的基座

### 坑 8: 标定时 `--arm` 没显式指定

**症状**: 标定「看起来」没问题, 但用右臂抓取时位置有偏差。

**原因**: 采样时的「自动选臂」取**第一条有 `cartesian` 的** (依赖 dict 顺序,
不确定), 而运行时的「自动选臂」是**优先 left**。采样用了哪条臂不确定,
运行时又固定优先左臂 —— 两边对不上, 偏置就带进来了。

**解决**: 标定时**显式写** `--arm left` 或 `--arm right`。详见
[4.2.1 板固定在哪只手上?](#421-板固定在哪只手上)。

**自查**: 采样时脚本会打印 `使用臂: <名字>` (stderr), 记下来。

### 坑 9: 用了 `conda run`, 采到一个样本都没有

**症状**: 提示符刚打印出来就立刻结束:

```
[1/5] 把机械臂摆到新位姿 (姿态要多样), 按回车采样, 输入 q 退出
没采到任何样本
```

**原因**: **`conda run` 默认捕获输出, 会切断子进程的 stdin**。脚本第一句
`input()` 就抛 `EOFError` —— 表现为「刚打印提示符就退出」。

**这个现象极易误判**: 看起来像「相机没开」或「机械臂连不上」, 其实都不是。

**解决**:

```bash
# 加 --no-capture-output
conda run --no-capture-output -n yolo python tools/calib_handeye.py --capture

# 或者直接用环境里的 python
/home/gxh/miniconda3/envs/yolo/bin/python tools/calib_handeye.py --capture
```

**注意**: 不是所有 `conda run` 都会断 —— 取决于终端/管道环境。所以**别靠
试**, 直接用 `--no-capture-output` 最稳。

> 脚本现在会**在开相机之前**检查 stdin, 不可交互时直接给出提示并退出。
> `tools/check_session.py` 里有针对这个的回归测试。

### 坑 10: 采了一堆样本, 解算时「剔野值后只剩 1 个」

**症状**: 采样时一切正常 (图有、角点有), 但解算报:

```
剔野值后只剩 1 个采样, 无法解算
```

**原因**: **控制器断连了, 但服务还在返回陈旧数据**。

链条是这样的:

1. 控制器 WebSocket 断开 (keepalive ping timeout)
2. `arm_service` 的读线程直接 `break` 退出 —— **没有任何重连**
3. 服务还活着, `/v1/kinematics` 照样返回 **200**, 但返回的是**断连前的快照**
4. 标定采了 25 帧, `T_world_tool` **完全相同** -> 解算时被当成野值剔光

**怎么确认**: 看采样文件里各个 `t_world_tool` 是不是一模一样:

```bash
python3 -c "
import json, numpy as np
d = json.load(open('/tmp/handeye_samples.json'))
s = d['samples'] if isinstance(d, dict) else d
tw = np.array([x['t_world_tool'] for x in s]).reshape(len(s), -1)
print('唯一位姿数:', len(np.unique(np.round(tw, 9), axis=0)), '/', len(s))
"
```

> `唯一位姿数: 1 / 25` -> 位姿是冻的, 数据不能用。
> 正常情况应该是 `25 / 25`。

**现在有三道防线** (不用再手动查):

| 防线 | 位置 | 作用 |
|------|------|------|
| 自动重连 | `robot/connection.py` | 断连后指数退避重连, 重连成功自动重新订阅 |
| `stale` 标记 | `/v1/kinematics` | 断连/快照过期时 `stale=true`, `ok=false` |
| 采样前预检 | `tools/calib_handeye.py` | 位姿冻结时**立刻中止**, 不用摆完 30 个姿态 |

**自己查**:

```bash
curl -s 'http://127.0.0.1:8802/v1/kinematics?refresh=true' | python3 -m json.tool
```

看 `stale` 是不是 `false`; 然后**手动动一下手臂**, 再查一次, 看 `cartesian` 的
x/y/z 变不变。

> **如果手动动了手臂, `cartesian` 也不变** —— 那不是软件问题, 是控制器
> 在**仿真模式** (`cartesian` 是固定预设值), 手眼标定必须用真机。

**还是不行的话**, 采完先把 session 存下来, 修好连接后用 `--samples` 离线重解
(不用重新摆位姿):

```bash
conda run -n yolo python tools/calib_handeye.py --samples /tmp/handeye_samples.json
```

### 坑 12: 「剔野值后只剩 0 个采样」—— 工具偏置文件不存在, 被静默忽略了

**症状**: 采了 15 个样本, 一切正常, 但解算报:

```
解算外参 (15 个采样)...
用 session 里记的内参做精化: fx=613.96 fy=632.48
剔野值后只剩 0 个采样, 无法解算
解算失败 —— 检查采样数量和多样性
```

**原因**: `--tool-offset /tmp/tool_offset.json` 指向的文件**根本不存在**。

旧版脚本的处理是:

```python
if not p.exists():
    print(f"找不到 {p}, 用单位阵", file=sys.stderr)   # ← 只是警告!
    return np.eye(4)
```

于是「板原点 = 工具原点」这个假设**悄悄生效**。而实际上板装在手腕外侧
**约 340 mm**, 根本不在工具原点。

**为什么这会导致「剔野值后只剩 0 个」**:

```
T_world_cam(i) = T_world_tool(i) · T_tool_board · T_board_cam(i)
```

`T_tool_board` 错了 340 mm, 于是每帧算出的 `T_world_cam` 都差了 340 mm,
而且**每帧差的方向还不同** (随工具姿态变)。各帧解出的外参互相打架,
全被判成野值剔光。

> 注意: 这个问题**不影响图像和角点** —— 重投影误差只有 0.24 px, 视觉部分
> 完全正常。所以「角点都对、图片没问题」并不能排除偏置错误。

**怎么确认**:

```bash
python3 -c "
import json
d = json.load(open('/home/gxh/tp/handeye_right.json'))
print('tool_offset:', d['tool_offset'])
"
```

> `tool_offset: [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], ...]` ->
> 是单位阵 -> 就是这个问题。

**现在有两道防线**:

| 防线 | 作用 |
|------|------|
| 文件不存在**直接报错** | `load_tool_offset()` 抛 `FileNotFoundError`, 不再静默回退 |
| **联合解算兜底** | 常规解算失败时自动同时解 `T_tool_board` + `T_world_cam` |

**联合解算** (`calibration/handeye.py` 的 `solve_joint()`):

数学上, 板在世界系下的位姿是 `T_world_tool · T_tool_board`, 相机在世界系
下的位姿是 `T_world_cam` —— 观测量是**两者的相对关系**, 所以这两个量
可以一起解出来。

对上面那批数据, 联合解算的结果:

```
重投影残差 RMS: 4.976 px   (从 30500 px 降下来)
T_world_cam  平移 = [ 0.0318 -0.0665  0.1279]
T_tool_board 平移 = [-0.1854  0.2783  0.0691]   (板原点离工具原点 341.5 mm)
```

**用法**:

```bash
# 常规解算失败时会自动尝试联合解算, 并把偏置存成 configs/tool_board.json
conda run -n yolo python tools/calib_handeye.py --samples /home/gxh/tp/handeye_right.json

# 下次采集直接用解出来的偏置
conda run -n yolo python tools/calib_handeye.py --capture --count 15 --arm right \
    --tool-offset configs/tool_board.json
```

**联合解算自带留一法自检** —— 每组留 3 帧重解, 看结果稳不稳。因为两个量
一起解有 6 个自由度是耦合的, 采样姿态不够多样时可能解得「偏」但残差看着
还行:

```
留一法稳定性自检 (每次留 3 帧)...
  5 组留一解: T_tool_board 平移标准差 最大 753.9 mm, ...
  **不稳定** —— 说明这批数据里有坏帧, 或者采样姿态不够多样。
```

> 标准差大**不代表解错了**, 只代表这批数据不足以支撑联合解。
> 这时候应该用常规解算 (已知偏置) + 多采几个姿态。

### 坑 11: Orbbec SDK 报 `Usb pal is not exist`

**症状**: 枚举不到相机, 报 `Usb pal is not exist, please check the build
config that you have enabled BUILD_USB_PAL`。

**原因**: pip 装的 `pyorbbecsdk` 是**没有 USB 后端**的构建。

**解决**: 用源码编译 OrbbecSDK (带 `OB_BUILD_USB_PAL=ON`), 然后设
`LD_LIBRARY_PATH` 指向它。编译方法见 [vision-service.md](vision-service.md)。

---

## 8. 工具清单

| 工具 | 用途 |
|------|------|
| `tools/make_board.py` | 生成可打印的 ChArUco 标定板 |
| `tools/calib_intrinsics.py` | 内参标定 (采集 + 解算) |
| `tools/calib_handeye.py` | 手眼标定 (采集 + 解算 + **离线重解**) |
| `tools/calib_view.py` | **看图挑坏帧** —— 角点叠加图 + 拼接大图 |
| `tools/calib_verify.py` | 验证 (含手性检查) |
| `tools/apply_calibration.py` | 把结果写回 `configs/vision.yaml` |
| `tools/check_calibration.py` | **自检** —— 用合成数据验证标定数学对不对 (含**联合解算**) |
| `tools/check_session.py` | **自检** —— 验证「存图 + 离线重解」链路 |
| `tools/check_frames.py` | **联调自检** —— 视觉服务 + 假 arm_service, 验多坐标系链路 |

### 自检的价值

`check_calibration.py` 不需要相机、不需要机械臂, 几秒钟就能验出标定脚本
本身的 bug (行列式搞反、角点顺序错、矩阵乘的方向错)。

靠真机试错很贵 —— 一次标定要半小时, 而且出了问题不知道是「数据没采好」
还是「脚本算错了」。自检先把后者排除掉。

```bash
conda run -n yolo python tools/check_calibration.py
```

「存图 + 离线重解」链路的自检 (同样不需要真机):

```bash
conda run -n yolo python tools/check_session.py
```

覆盖: session 存取往返、离线重解与采样时解算**完全一致**、工具偏置可覆盖、
样本可剔除、旧格式兼容、重检测可用、可视化能出图、**位姿陈旧/冻结的检测**。

联调自检 (不需要真机, 自己起一个假 `arm_service`):

```bash
conda run -n yolo python tools/check_frames.py
```

覆盖:

- 轴置换的行列式 / 正交性 / 语义方向
- RPY 变换往返
- 矩阵求逆
- 手眼标定 (无噪声 → 精确解回真值)
- 手眼标定 (带噪声)
- 野值剔除
- 精化 (固定内参)
- 手性错误检出
- 外参 provider
- 多臂基座换算 (left_base / right_base)
- 头部动态补偿 (头动 -> world 坐标跟着变)

---

## 相关文档

- [视觉服务](vision-service.md) —— 服务架构、API、Orbbec SDK 编译
- [README](../README.md) —— 项目总览
