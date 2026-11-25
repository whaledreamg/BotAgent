# 机械臂动作服务 (arm_service)

参考 `ActionPlanner` 的 `src/timeline/` 与 `src/robot/`, 把**动作序列播放**从
GUI 程序里剥离出来, 做成与 TTS / ASR / Agent 并列的独立服务。

- 端口: **8802**
- conda 环境: `botagent`
- 配置: `configs/arm.yaml`

## 接口

### HTTP

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 服务状态、控制器连接状态、动作库数量 |
| GET  | `/v1/actions` | 动作库清单 (key / label / 时长 / 片段数) |
| POST | `/v1/play` | 下发播放 (单个或队列), **异步**, 立即返回 |
| POST | `/v1/stop` | 停止播放并交还控制通道 |
| GET  | `/v1/status` | 当前播放状态 (阶段 / 进度 / 错误 / 最近一次门阀结果 / 队列) |
| GET  | `/v1/safety` | 跑一次安全门阀检查 (不执行动作) |
| GET  | `/v1/kinematics` | 运动学快照 (TCP 世界系位姿 / 基座位姿 / 关节角), 手眼标定用 |
| GET  | `/v1/flows` | 流程清单 (configs/flows/ 里的每条流程) |
| GET  | `/v1/flows/{name}` | 一条流程的完整定义 (片段 + 参数) |
| POST | `/v1/flows/reload` | **强制重载**流程定义 (平时不用, 自动热重载) |
| GET  | `/v1/flow` | 默认流程的扁平参数 (旧接口, 兼容用) |
| POST | `/v1/move_to` | TCP 移到世界系目标点 (本地逆解 + 关节流) |
| POST | `/v1/move_joints` | **直接**移到给定关节角 (不走逆解) |

```bash
# 看动作库
curl -s http://127.0.0.1:8802/v1/actions | python -m json.tool

# 播一个动作 (2 倍速)
curl -s -X POST http://127.0.0.1:8802/v1/play \
  -H 'Content-Type: application/json' \
  -d '{"action":"挥手", "speed":2.0}'

# 播一串动作 (按顺序, 只入控一次)
curl -s -X POST http://127.0.0.1:8802/v1/play \
  -H 'Content-Type: application/json' \
  -d '{"actions":["准备点","挥手"], "speed":1.0}'

# 停止
curl -s -X POST http://127.0.0.1:8802/v1/stop

# 看状态
curl -s http://127.0.0.1:8802/v1/status
```

`/v1/play` 的 body:

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `action` | string | — | 动作名 (= JSON 文件名, 不含 `.json`), 如 `挥手` |
| `actions` | string[] | — | 动作序列, 按顺序依次播放 |
| `speed` | float | 1.0 | 倍速, 0.1 ~ 3.0 |
| `loop` | bool | false | 循环播放 |
| `wait` | bool | false | **等播完再返回** (同步) |

`action` 与 `actions` **二选一**, 都给时以 `actions` 为准。两个都不给返回 HTTP 400。

**`wait=true`** —— 抓取流程要按顺序走 (回准备点 -> 拍照 -> 移到上方点 -> ...),
下一步必须等上一步真的动完了才能开始。开了这个参数服务端会阻塞到播完,
应答里多两个字段: `finished` (播完了吗) 和 `error` (没播完的原因)。

```bash
# 等播完再返回
curl -s -X POST http://127.0.0.1:8802/v1/play \
  -H 'Content-Type: application/json' \
  -d '{"action":"准备点", "wait":true}'
```

### 播放队列

`actions` 传多个时, 整个队列**只入控一次** —— 动作之间不松手, 机械臂运动连贯,
也省掉每个动作重新入控的 700ms S 曲线对齐开销。

每个动作播完时重新取一次实测关节角作为下一个动作的起点 (轨迹不落盘起始姿态,
避免把机器人拽到某个固定姿态上去)。动作之间**没有额外停顿**, 直接衔接。

**队列里每个动作切换前会再查一次安全门阀** —— 队列可能播十几秒, 期间用户
可能按了急停。下一个动作带故障去播, 后果比第一个动作更严重。

`/v1/status` 里的队列字段:

| 字段 | 说明 |
|------|------|
| `queue` | 还没播的动作 (按顺序), 空 = 没有排队 |
| `queue_total` | 整个队列的动作数 (含正在播的) |
| `queue_duration` | 队列总时长 (秒) |

### WebSocket `ws://127.0.0.1:8802/ws`

服务端推送:

```json
{"type": "ready", "phase": "idle", "actions": 13}
{"type": "state", "phase": "aligning", "detail": "正在平滑对齐轨迹起点"}
{"type": "state", "phase": "playing", "detail": "已对齐起点, 开始播放"}
{"type": "finished"}
{"type": "error", "message": "入控失败 (24085): ..."}
```

阶段取值: `idle` / `connecting` / `entering` / `aligning` / `playing` / `stopping` / `error`

## 运动学接口 (手眼标定用)

### GET /v1/kinematics

返回各臂的运动学快照 —— **视觉服务做手眼标定时的「量尺」**。

```bash
curl -s "http://127.0.0.1:8802/v1/kinematics" | python -m json.tool
curl -s "http://127.0.0.1:8802/v1/kinematics?refresh=true" | python -m json.tool
```

```json
{
  "ok": true,
  "arms": {
    "LeftArm": {
      "joints": [0.0, 0.1, -0.2, 0.0, 0.3, 0.5],
      "cartesian": {"x": 0.35, "y": 0.12, "z": 0.45, "a": 1.57, "b": 0.0, "c": 0.0},
      "base_frame": {"x": 0.0, "y": 0.0, "z": 0.0, "a": 0.0, "b": 0.0, "c": 0.0},
      "base_frame_kind": "static",
      "enabled": true,
      "mode": "CSPPassthrough",
      "timestamp_ms": 1731290000000
    }
  },
  "lifting_column": {"name": "Column", "height": 0.85, "online": true},
  "controller_connected": true,
  "timestamp_ms": 1731290000123,
  "notes": []
}
```

| 字段 | 含义 |
|------|------|
| `arms.<臂名>.joints` | 实测关节角 (rad) |
| `arms.<臂名>.cartesian` | **TCP 世界系位姿** {x,y,z,a,b,c} —— 标定用的量尺 |
| `arms.<臂名>.base_frame` | 臂基座在**世界系**下的位姿 |
| `arms.<臂名>.base_frame_kind` | `static` (配置量) / `derived` (挂接安装, 基座会动) |
| `lifting_column` | 升降柱状态 (相机装在升降柱顶端时做动态补偿要用) |
| `notes` | 提示信息 (比如某条臂还没有 `cartesian`) |

### `refresh=true` 为什么重要

状态推送是**异步**的, 周期 50ms —— 快速运动时推送值跟不上实际。标定每个
位姿都要精确的 TCP 位姿, 所以标定脚本用 `?refresh=true`, 让服务**主动**
向控制器发一次 `robot/status` 请求-响应再返回。

### `base_frame_kind: derived` 的含义

`derived` 说明这条臂是**挂接**在父机器人上的 (比如装在升降柱顶端),
基座本身在动。这时候:

- 相机到**臂基座系**的相对关系仍然是静态的 → 静态外参成立
- 但臂基座到**世界系**的关系会变 → 需要用这里的 `base_frame` 做动态补偿

视觉服务配置 `target_frame: world` + `dynamic: true` 时, 每次检测前都会
拉一次这个接口。

## 笛卡尔移动: 本地逆解 + 关节流

`POST /v1/move_to` 把 TCP 移到世界系目标点。**逆解在本地做, 不调用控制器的
逆解接口**。

### 为什么不用控制器的逆解

控制器的末端逆解 (`cartesianIkPath: analytic`) 有自己的参考坐标系约定
(API 说明 21.9):

| 机型模板 | 入控位姿在哪个系 | 末端是什么 |
|----------|------------------|------------|
| `HM5-54-*` / `DA5-54-*` | **躯干系** | 该机型数值模型的第 7 关节坐标系 |
| `NDA-06-*` | **该臂基座系** | 法兰 |

而 `robot/status.cartesian` 是**基座系 + 工具偏置**。前两者**不是同一个系**,
两套位姿不可互相代入。

一旦搞混, ``flange = target_base · tool⁻¹`` 算出来的「工具偏置」是垃圾 ——
本项目实测过 **1.01 米 / 120°**, 目标位姿整个翻转, 逆解必然失败。

### 本地方案

```
目标位姿 (世界系)
  → 减去控制器实测的 baseFrame, 换到该臂基座系
  → pinocchio (URDF) DLS 逆解 → 关节角
  → 关节空间 S 曲线插值
  → UDP controlSpace=0 关节流下发
```

入控参数是 ``cartesianIkPath: "none"`` —— 控制器**全程不做末端逆解**,
只吃关节角。

| 组件 | 位置 |
|------|------|
| 运动学 (FK/IK) | `services/arm_service/robot/kinematics.py` |
| 关节流移动 | `services/arm_service/joint_mover.py` |
| URDF | `services/arm_service/urdf/{left,right}_arm.urdf` |

### 坐标系口径 (关键)

- IK / FK 都在**该臂基座系 (base_link)** 里做;
- 世界系 ↔ 基座系靠控制器的 ``baseFrame`` 换算 (真机唯一正确的口径);
- URDF 自带的装法与实测 ``baseFrame`` **不是同一个数** —— 不覆盖的话
  IK 目标整体偏。

### 关节约定探测

URDF 的 ``j1..j7`` 与控制器上报的 ``joints[0..6]`` 是否同序同向**没有文档
保证**。猜错的后果是机械臂朝完全相反的方向动, 而且**不报任何错**。

``detect_joint_convention()`` 把实测关节角代入 FK, 与控制器上报的 TCP
位姿对比, 自动探测「同序 / 反序 / 取反」。本机实测两条臂都是**同序**,
误差 0.00mm / 0.00°。

自检:

```bash
python3 services/arm_service/tools/check_local_ik.py
```

### 奇异位形与姿态回退

七轴冗余有无数解, 位置定了姿态本来就不唯一。而且手臂**伸直**时
(j3 ≈ 0) 雅可比近奇异 —— 姿态方向的那个自由度**丢失了**, 此时要求姿态
精确会把位置一起拖死 (实测位置差 100mm 都下不去, 而纯位置逆解能到
0.000mm)。

所以逆解分三层回退:

1. **全姿态** (`rot_weight=1`) —— 位置姿态都要;
2. **多起点** —— 换种子再试;
3. **位置优先** (`rot_weight=0`) —— 只管位置, 姿态随冗余自由度走。

第 3 步命中时, 响应的 `ik` 字段里会有 ``"position_only": true`` 和
``rot_deviation_deg``, 并打一条警告 —— 抓取只按位置走, 姿态偏差要靠
视觉结果复核。

### 依赖

本地逆解需要 ``numpy`` + ``pinocchio``:

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "numpy<2.3" pin
```

装不上时本地逆解**降级但不崩** (``/v1/move_to`` 如实报「本地运动学不可用」),
动作库播放等老功能照常。

### 切换回控制器逆解

```yaml
arm:
  move_mode: controller_ik   # local_ik (缺省) | controller_ik
```

``controller_ik`` 是走控制器的 ``cartesianIkPath=analytic``, 保留是为了
对照排障, 不是推荐路径。

## 安全门阀

**动作执行前先检查状态, 有异常就拒绝动作, 并把原因播报出来。**

代码在 `services/arm_service/safety.py`, 判定逻辑与控制器无关 (只吃一份
`robot/status` 快照), 所以可以脱离真机做单元测试。

### 为什么要有

机械臂出问题时 (急停、安全锁定、伺服故障、没使能、控制器连不上), 直接把
轨迹发下去的表现是「下发成功但机械臂不动」或者「动到一半卡住」—— 用户和
现场都无从判断到底出了什么事。门阀把这件事变成**明确的一句话**。

### 检查项

门阀同步发一条 `robot/status` (请求-响应, 不是状态推送 —— 推送是异步的,
且只在订阅周期内到达, 不能用来做即时判定), 然后逐项判定:

| 判定项 | `robot/status` 字段 | 不通过时的播报 |
|--------|---------------------|----------------|
| `connected` | (连接状态) | 机械臂控制器连不上 |
| `status` | — (读不到快照) | 读不到机械臂的状态 |
| `faulted` | `faulted` / `faultCode` | 机械臂处于安全锁定状态, 有故障没清除 |
| `emergency_stop` | `emergencyStopActive` | 机械臂的急停还按着 |
| `safety_response` | `safetyResponse` | 机械臂刚被安全保护停掉了 (按类型区分措辞) |
| `safety_phase` | `safetyPhase` | 机械臂还在安全减速中 |
| `enabled` | `enabled` | 机械臂还没上使能 |
| `mode` | `mode` | 机械臂没上电 / 在拖动模式 / 在救援模式 … |

**所有臂都要查** —— 双臂共用一条通道时, 一条臂异常意味着另一条也处于
不确定状态。

`mode` 只允许 `CSPPassthrough` (位置控制模式): 只有它能接受实时位置流,
其余模式 (`Disabled` / `CSPRescue` / `CSTDrag` / `ServoDrag` / `CSVZeroSpeed`)
都不该动。

### 两道关卡

1. **服务端** (`arm_service`): 入控之前必查, 不通过就拒绝并用 TTS 播报原因。
   这是最后一道防线 —— 就算有人绕过 Agent 直接调 `/v1/play` 也会被拦。
2. **客户端** (`agent_service`): 下发前先问一句 `/v1/safety`, 有异常就直接
   回「为什么现在不能动」, 而不是先说「好的」然后让机器人纹丝不动。

两道关卡用的是同一份判定逻辑, 不会互相矛盾。

### TTS 播报

门阀拒绝时由 `services/arm_service/notifier.py` 连 ChatTTS 的 WebSocket 播报。

默认走**服务端播报** (`notifier.server_play: true`): 在请求里带 `play: true`,
让 TTS 服务自己开扬声器放音。arm_service 所在的 conda 环境通常没有
`sounddevice`, 走这条路才真能出声, 而且音色与 ASR 侧播报一致。

### 配置

```yaml
# configs/arm.yaml
arm:
  safety:
    enabled: true           # 门阀总开关
    require_enabled: true   # 必须已上使能
    require_connection: true
    check_fault: true
    allowed_modes: ["CSPPassthrough"]
    timeout_ms: 3000        # 查询状态的超时; 超时按「不通过」处理 (不能盲动)
    speak_on_reject: true   # 拒绝时用 TTS 播报原因
  notifier:
    enabled: true
    ws_url: "ws://127.0.0.1:8000/ws"
    server_play: true       # true = TTS 服务端播; false = 本地 sounddevice 播
```

### 自检

```bash
cd services/arm_service && python tools/check_safety.py
```

不需要真机 —— 直接喂假的 `robot/status` 快照, 验证每一项的判定与播报文案。

## 动作库

动作序列放在 `services/arm_service/data/*.json`。

**一个 JSON 文件 = 一个动作, 文件名就是动作名** (去掉 `.json`):

```
data/挥手.json    -> 动作 "挥手"
data/准备点.json  -> 动作 "准备点"
data/伸左手.json  -> 动作 "伸左手"
```

文件里的 `segments` 是这个动作的**轨迹片段**: 首尾相连构成一条连续运动。
每段只记录**结束关节角**, 本段起点 = 上一段终点; 第一段的起点取**播放那一刻的
实测姿态** (所以轨迹不会把机器人拽到某个固定姿态上)。

检索接受几种写法: `挥手` / `挥手.json` / `  挥手  ` (忽略首尾空白与大小写)。

新增动作直接在 `data/` 下放一个 `<动作名>.json` 即可, 服务重启后自动加载。

## 抓取流程 (声明式)

`vision.grasp` skill 是**通用流程执行器** —— 流程本身不在代码里, 而是
定义在 `configs/flows/*.yaml` 里。改流程**不用改代码**。

默认流程 (`configs/flows/grasp.yaml`):

| 段 | id | type | 做什么 |
|----|----|------|--------|
| 1 | `ready` | `arm` | 播动作库的 `准备点` (拍照前固定视角) |
| 2 | `detect` | `detect` | 视觉服务 `fresh=True` (强制重拍, 不走缓存), 时间轴上占 1.5s |
| 3 | `object` | `target` | **声明**具名点 = 这次识别到的东西 (纯声明, 不动) |
| 4 | `above_point` | `target` | **声明**具名点 = `object` 正上方 5cm (纯声明, 不动) |
| 5 | `above` | `arm` | `move_to` `above_point` (走本地逆解) |
| 6 | `grasp` | `arm` | `move_to` `object` + 偏移 |
| 7 | `close` | `hand` | 播动作库的 `闭合夹爪` |
| 8 | `safe` | `arm` | 播动作库的 `安全点` |

### 流程文件格式

```yaml
name: grasp
description: "标准抓取: 回准备点 -> 拍照 -> 上方点 -> 抓取点 -> 闭夹爪 -> 回安全点"

# 某一段失败时的收尾动作 —— 先播它, 再报失败
recovery_action: 安全点
recovery_label: 回安全点

steps:
  - id: ready
    type: arm           # 关节片段
    action: 准备点      # 终点来自动作库
    label: 回准备点

  - id: detect
    type: detect        # 识别片段 —— 产出识别结果
    duration: 1.5       # 在时间轴上真的占 1.5s
    label: 拍照识别

  # ---- 具名点声明: 只登记, 不产生运动 ----
  - id: object
    type: target
    source: detect      # 取自识别结果
    label: 目标物体

  - id: above_point
    type: target
    base: object        # 基于另一个具名点
    offset: {x: 0, y: 0, z: 0.05}
    label: 目标上方

  # ---- 引用具名点去做别的事 ----
  - id: above
    type: arm
    point: above_point  # 引用具名点 -> 走本地逆解
    duration: 3.0
    label: 移到上方点

  - id: close
    type: hand          # 手部片段
    action: 闭合夹爪
    label: 闭合夹爪
```

**片段类型** (执行器内置注册表):

| type | 作用 | 终点/取值来源 |
|------|------|--------------|
| `arm` | 关节片段 (机械臂动) | `action` / `pose` / `point` / `base` **四选一** |
| `hand` | 手部片段 (灵巧手动) | `action` / `pose` / `preset` 至少一个 |
| `detect` | 识别片段 —— 产出识别结果 | `duration` |
| `target` | **具名点声明** —— 纯声明, 不动 | `source: detect` / `base`+`offset` / `pose` 三选一 |

**具名点 (target)**:

- 值的来源有三 种: `source: detect` (识别结果) / `base`+`offset` (基于另一个具名点) / `pose` (写死坐标)
- 引用方式是 `point: <步骤id>` —— **按名字引用, 位置任意**
- **顺序约束**: `source: detect` 的点必须排在 `detect` 之后; `base` 的点必须排在被引用的点之后; 不能循环引用

**`on_failure`** (可选, 每段可单独指定): `recover` = 失败后先收尾再报;
`abort` = 直接报失败不收尾。

### 新旧格式对照

| 旧 (`play`/`move`/`close`/`open`) | 新 (`arm`/`hand`/`detect`/`target`) |
|-------------------------------|-----------------------------------|
| `type: play` + `action: 准备点` | `type: arm` + `action: 准备点` |
| `type: move` + `target: object` | `type: arm` + `point: object` (先声明 `target` 片段) |
| `type: move` + `target: above` | `type: arm` + `point: above_point` (基于 `object` + `z` 偏移) |
| `type: close` | `type: hand` + `action: 闭合夹爪` (或 `direction: close`) |
| `type: open` | `type: hand` + `action: 张开夹爪` (或 `direction: open`) |
| `move` 的 `offset` 写在 `move` 上 | 偏移写在引用它的 `arm` 段上, 或写在 `target` 段上 |

**旧格式不再被接受** (hard break) —— 加载时明确报 `未知片段类型 'play'`。

### 扩展边界

| 层次 | 定义在哪 | 改它要改 Python 吗 |
|------|----------|--------------------|
| 片段**内容** | YAML: 有几段、什么顺序、动作叫什么名 | **不用** |
| 片段**参数** | YAML: 距离、时长、偏移、具名点 | **不用** |
| 片段**类型** | Python 注册表 (`vision_grasp.py` 的 `_HANDLERS`) | **要** |

**新流程 / 改流程 = 只写 YAML**；**新片段类型 = 要写 Python**。

目前还表达不了的: 「放在桌上」(需要「具名放置点」概念)、「等 2 秒」
(需要新类型)、调任意 HTTP 接口 (需要新类型)。详见
`configs/flows/README.md`。

### 失败收尾

- `detect` 失败 -> 直接返回, **不回安全点** (此时手还在准备点, 那是安全的)
- 中途失败 -> **先回 `recovery_action`** 再报失败, 否则手停在半空
- 最后一步失败 -> 前面的步骤已成功, 如实说「抓到了但收尾失败」

**总闸关着的时候流程全走完** (`arm.enabled: false`) —— 只是不动。
用户要看的正是每一步算出来的位置, 中途就停掉等于什么都看不到。

### 接口

| 接口 | 用途 |
|------|------|
| `GET /v1/flows` | 列出所有流程 (名字 + 描述 + 步骤 id) |
| `GET /v1/flows/{name}` | 拿一条流程的完整定义 (步骤 + 参数) |
| `POST /v1/flows/reload` | 强制重载 (平时不用 —— 自动热重载) |
| `GET /v1/flow` | 旧接口, 返回默认流程的扁平参数 (兼容用) |

### 热重载

**改完 `configs/flows/*.yaml` 不用重启服务** —— 服务每次用到流程时都会
比对目录指纹 (每个 yaml 的 mtime + 大小), 变了就重读。

自动生效的时机: `GET /v1/flows`、`GET /v1/flows/{name}`、每次抓取
(`/v1/grasp_sequence`)。

同秒内改文件且大小没变时指纹可能察觉不到 (有些文件系统 mtime 精度只到
秒), 这时候手动重载:

```bash
curl -X POST localhost:8802/v1/flows/reload
```

### 自检

```bash
python services/arm_service/tools/check_flows.py
```

校验: 文件格式、步骤类型是否已知、动作名是否在动作库里、热重载的指纹
逻辑、以及新流程与 `configs/arm.yaml` 里旧配置的**行为是否等价**
(防止迁移时手抖改错)。

## 灵巧手 (Revo2)

灵巧手**不挂在 EtherCAT 上** —— 它接在末端通信板的 RS485 口, 要经控制器
的 `rs485/*` 接口透传 Modbus RTU 帧。

协议**逐字节移植**自 ActionPlanner 的 `src/robot/HandController.cpp`
(那是这套设备上跑通过的权威实现), 代码在 `services/arm_service/robot/hand.py`。

### 通道与从站

| | 通信板从站名 | Modbus 从站地址 |
|---|---|---|
| 左手 | `EtherCAT485L` | 126 |
| 右手 | `EtherCAT485R` | 127 |

通信板从站名用来在 `rs485/list` 里找通道; Modbus 从站地址是帧里的第一个字节。
**通道标识必须原样取自 `rs485/list`** —— 里面含主站名, 不能自己拼。

波特率 115200 8N1。

> **2026-11-25 真机实测确认** (控制器 `192.168.100.16`):
>
> ```
> 通道:   dualEc2485:Master1/EtherCAT485R
> 波特率: 115200  ← 只有这个能通; 9600 / 19200 都报 23256
> 从站:   127     ← 只有这个能通; 126 报 23256
> 寄存器: 0x07D0 读位置 (实测应答 [127,4,12,1,144,1,144,0,49,0,50,0,49,0,50,95,136])
> ```
>
> 23256 = 从站无应答。**这个实测帧已作为回归用例写进
> `tools/check_hand.py`** —— 解析器对它的 CRC、长度、6 个 DOF 都对得上。

### 6 个自由度

| 序号 | 名称 | 最大角度 |
|---|---|---|
| 0 | 拇指弯曲 | 59° |
| 1 | 拇指侧摆 | 90° |
| 2 | 食指 | 81° |
| 3 | 中指 | 81° |
| 4 | 无名指 | 81° |
| 5 | 小指 | 81° |

归一化值 0~1000, 与角度线性对应。

### 预设手势

| 名称 | 值 | 说明 |
|---|---|---|
| 一把抓 | `[1000, 0, 1000, 1000, 1000, 1000]` | 拇指弯曲 + 四指拉满 |
| 释放 | `[0, 0, 0, 0, 0, 0]` | 全开 |
| 半抓 | `[500, 0, 500, 500, 500, 500]` | |
| 两指捏 | `[800, 0, 200, 0, 0, 0]` | 拇指 + 食指 |
| 点赞 | `[0, 0, 1000, 1000, 1000, 1000]` | |
| 指点 | `[1000, 0, 0, 1000, 1000, 1000]` | |

> ⚠️ **DOF 1 (拇指侧摆) 在「一把抓」里必须保持 0**。拉满会让拇指侧张到
> 一边, 抓不住东西。

### 寄存器

| 操作 | 功能码 | 起始寄存器 | 数量 |
|---|---|---|---|
| 写 6 个 DOF | `0x10` | `0x03F2` (1010) | 12 |
| 读 6 个 DOF | `0x04` | `0x07D0` (2000) | 6 |

写寄存器的数据按 `(位置, 时长)` 交替排列, 每个 2 字节 —— 时长就是
`write_time_ms` (驱动器按这个时间走完目标, 避免瞬间打到)。

### 为什么手部指令必须独立线程

一次「写 + 读」是**两次控制面往返**, 还要等通信板发送、等手应答 —— 远慢于
90 Hz 的臂控循环。塞进臂控循环会把关节流拖死 (缺帧保持窗只有 5 个锚点周期)。

所以: 臂控线程只负责**入队**, 独立线程 `arm-hand` 负责真正下发。

### 前置条件

控制器里该手的 `gripper` 设备型号必须是 **Generic485**。厂商夹爪协议会
占用 RS485 通道, 报 `23252`。停用夹爪协议要走
`peripheral/setDeviceEnabled` (需要现场确认夹爪静止, 本程序不会替你确认)。

### 接口

| 接口 | 用途 |
|------|------|
| `GET /v1/hand/status` | 通道、就绪状态、当前位置 |
| `POST /v1/hand/connect` | 解析通道 + 初始化串口 (幂等) |
| `POST /v1/hand/open` | 张开手 (`arm.hand.open_pose`) |
| `POST /v1/hand/close` | 握紧手 (`arm.hand.close_pose`) |
| `POST /v1/hand/preset` | 下发预设手势 (`{"preset": "一把抓"}`) |

### 自检

```bash
python services/arm_service/tools/check_hand.py
# 连真机 (会真的读位置; 加 --move 还会真的动手指)
python services/arm_service/tools/check_hand.py --controller 192.168.100.16 --move
```

离线部分校验帧构造是否与 ActionPlanner 逐字节一致、序列合成的时序是否正确。

## 动作序列合成 (先识别, 再合成一条序列)

**关节片段**和**夹爪片段**走两条不同的通道, 但它们要合成**一次**执行:

- 关节片段 (回准备点、移到上方点) → 拼进一条时间轴, 一次入控连续播完;
- 夹爪片段 (闭合夹爪) → 记成时间轴上的**时间点**, 到点了再单独下发。

代码在 `services/arm_service/sequence.py`。

### 为什么合成而不是排队播

排队播 (`play_many`) 每换一个动作就重新构造时间轴, 每个动作的起点取当前
实测姿态 —— 中间会有「重新对齐」的空隙。合成一条时间轴则完全连续。

### 移动步怎么进时间轴

移动步 (移到上方点 / 移到抓取点) 的目标点是**识别之后**才知道的, 所以它
不是从动作库拿的, 而是:

1. 用**本地 pinocchio 逆解** (基座系) 把目标点解成关节角;
2. 作为一条 `arm` 片段插进时间轴。

逆解失败就不合成 —— 宁可报错也不要播一条错的轨迹。

### 接口

| 接口 | 用途 |
|------|------|
| `POST /v1/sequence` | 把几个动作合成一条时间轴 (`{"actions": [...]}`) |
| `POST /v1/grasp_sequence` | 识别之后合成抓取序列 (`{"x":…, "y":…, "z":…}`) |

`/v1/grasp_sequence` 的 `plan_only: true` 可以只合成不执行, 看一眼合成的
结果 (每个移动步的逆解误差) 再决定要不要动。

### Agent 侧的模式

`vision.grasp` skill 默认走**序列模式**: 先识别, 再把 flow 合成一条序列
交给机械臂服务执行。传 `sequence: false` 退回逐步骤下发 (老行为)。

## 协议约束 (踩错就是机器人不动或直接被停控)

这部分决定了实现的正确性, **不能简化**:

1. **入控与首包必须并行**
   `realtime/startControl` 发出后不能等响应才发第一包 —— 首包等待窗只有
   2000ms。必须先起流再发 `startControl`。
   代码: `robot/controller.py` 的 `enter_control()` 里先 `streamer.start()`
   再 `start_control()`。

2. **首包必须贴近实测关节角**
   关节包若偏离入控时位形会触发 `firstFrameJumpRejected` 并自动停控。
   所以先持续发实测锚点, 收到轨迹起点后再用 700ms S 曲线平滑过渡。

3. **关节流必须独立线程**
   控制器的缺帧保持窗只有 5 个锚点周期 (下限 30ms), 任何卡顿 (Python 的
   GIL、GC、HTTP 处理) 都会让关节流断顿, 被判定为通道陈旧并回收直达通道。
   代码: `robot/streamer.py` 用独立线程 + 绝对时刻节流。

4. **停发不掉线**
   `holdOnIdle: true` 下只有 `realtime/stopControl` 交还通道。
   停止时必须显式调停控, **退出前也要停控**, 否则会话残留占住该臂,
   下次入控会撞上「startControl 无响应 / 包被旧会话吞掉」。

5. **UDP 报文格式**
   扁平 JSON, 无 `{id,ty,db}` 包裹:
   `{"robot":"LeftArm","controlSpace":0,"commandMode":0,"value":[7 个 rad]}`

   **报文里绝对不能有手部字段。** 控制器文档 (API 说明 §21.9) 规定实时
   指令包的字段只有 `robot` / `controlSpace` / `commandMode` / `poseType` /
   `value` —— **没有 `hand`**。多塞一个字段进去, 控制器会**整包判
   `invalidDatagram`**, 那时候连手臂都不动了, 比不动手指糟得多。

   灵巧手走的是**另一条路** —— 控制器的 `rs485/*` 透传, 见下一节。

6. **入控前必须清残留会话**
   控制器上残留的会话 (尤其 `state="stopped"` 的) 会占住该臂, 表现为
   「发了指令但机械臂不动」而且**不报任何错**。每次入控前必须先
   `realtime/getStatus` 查一遍并停掉陈旧会话。

7. **会话参数冻结**
   运行期改参数必须 `stopControl` 后重新 `startControl`。

8. **必须显式开关节限幅**
   `jointLimit.enable` 的协议缺省是 `false` —— 即没有任何关节级限速。
   配置里 `joint_limit.enable: true` 必须保留。

## 插值

移植 `InterpolationEngine` 的三种插值:

| 取值 | 行为 |
|------|------|
| `linear` | 匀速直线, 速度在片段边界突变 |
| `cubic` | 片段内 S 形加减速, 起止速度为零 (默认) |
| `spline` | 跨片段三次样条, 关键帧处速度连续 |

spline 用的是**按时间尺度的 Hermite 形式** (等价非均匀 Catmull-Rom), 不是
教科书上的均匀基函数 —— 均匀版在片段时长不等时交界处速度会跳变, 手臂会「抽」一下。

## 真机联调步骤

1. 确认控制器可达:
   ```bash
   curl -s http://127.0.0.1:8802/health
   # controller_connected 应为 true
   ```
2. 先跑**单个短动作** (`零点`, 2 秒), 确认方向与幅度正确:
   ```bash
   curl -s -X POST http://127.0.0.1:8802/v1/play \
     -H 'Content-Type: application/json' -d '{"action":"零点"}'
   ```
3. 观察 `logs/arm.log`:
   - `[关节流] 首包 -> 192.168.100.16:9030 {...}` —— 报文格式与目标地址
   - `[控制] 阶段 -> aligning` → `playing` —— 对齐与开播
4. 再跑长序列 (`挥手`, 8 秒)。
5. 异常时先 `POST /v1/stop` 交还通道。

### 排障

| 现象 | 原因 |
|------|------|
| 「发了指令但机械臂不动」 | 控制面没有报错 —— 多半是残留会话占住了臂。查 `realtime/getStatus` 的 `sessions[]` |
| `firstFrameJumpRejected` 停控 | 首包偏离实测角过大。检查 `lead_in_ms` 与首包值 |
| 「直达通道陈旧，已自动回收」 | 首包没在 2000ms 窗口内发出。检查起流是否早于 `startControl` |
| `24158` | `analyticIk` 键名与机型不符 (DA5 用 `maxQ7Delta`, 其余用 `maxArmAngleDelta`) |
| 连不上控制器 | 环境里的 `HTTP_PROXY` / `SOCKS_PROXY` 会把内网地址当外网。服务已强制 `proxy=None` |

## 自检

不需要真机:

```bash
cd services/arm_service
python tools/check_arm.py          # 协议级: 时序 / 报文格式 / 对齐 / 停控
python tools/check_arm_service.py  # 端到端: mock 控制器 + 真实 HTTP/WS 服务
python tools/check_safety.py       # 安全门阀: 逐项判定 + 播报文案 (不需要真机)
python tools/check_grasp_flow.py   # 抓取流程: 报文格式 / 协议帧 / 序列合成 / 步骤顺序
python tools/check_flows.py        # 流程定义: 格式 / 类型 / 动作名 / 与旧配置等价
python tools/check_hand.py         # 灵巧手: 协议帧 + 序列合成 (不需要真机)
```

**真机探针** (需要能连到控制器):

```bash
# 验证灵巧手: 连通道、读位置 (加 --move 会真的合一次手再张开)
python tools/check_hand.py --controller 192.168.100.16 --move

# 录一个固定点动作 (安全点/准备点) —— 关节角必须现场示教
python tools/record_safe_point.py --name 安全点
```

## 与 Agent 服务的对接 (已接通)

语音控制机械臂的链路:

```
语音 -> ASR -> Agent (device 分支) -> arm_service /v1/play -> 机械臂
                              \-> TTS 播报 "好的，挥手"
```

### 配置文件

**`configs/actions.yaml`** —— 说法到动作名的映射表。动作名 = JSON 文件名:

```yaml
arm:
  base_url: "http://127.0.0.1:8802"
  exclude: ["怎么样", "如何", "为什么", "怎么", ...]   # 疑问句式不算指令

  actions:
    - action: "挥手"
      say: ["挥手", "挥个手", "挥挥手", "打个招呼", ...]
    - action: "伸左手"
      say: ["伸左手", "左手伸出来", "伸出左手", ...]
    - action: "零点"
      say: ["零点", "回到零点", "归零", ...]
```

匹配顺序 (先命中先用):
1. 精确等于动作名 (用户直接说了「挥手」)
2. 命中 `say` 里的说法 (「挥个手」-> 挥手)
3. 都没命中 -> 交给 **tool calling** 让模型从动作清单里挑

> 第 3 步现在走的是 Agnes 的原生 function calling, 与设备工具共用同一套
> tool schema。详见 [docs/action-integration.md](action-integration.md)。
>
> 无论走哪条路径, **安全门阀都会照常执行** —— 换成 tool calling 不会
> 绕过安全检查。

**`configs/agent.yaml`** —— 启用 device 分支:

```yaml
handlers:
  device:
    module: handlers.device_handler
    class: DeviceHandler
    enabled: true
    speak: true        # 机械臂动了要出声确认
```

device 规则的关键词**不在 agent.yaml 里**, 而是从 `configs/actions.yaml` 自动取
(`router._arm_keywords()`), 避免两处配置不同步。

### 复合指令 (动作队列)

用户一句话里说了多个动作时 (「先准备点然后挥挥手」), Agent 会解析成
**有序的动作列表**, 交给 arm_service 排成队列播。

```
"先准备点然后挥挥手"
   │
   └─ 规则路径 (_match_all): 按动作在句子里出现的**位置**排序
        -> ["准备点", "挥手"]     零延迟, 不依赖 LLM
```

播报是**一次性的总览**: 「好的，准备点、挥手」—— 告诉用户接下来会做什么,
而不是每播一个动作就播报一次 (播报会打断动作执行)。

> **为什么复合指令走规则而不是 LLM**: 之前复合指令一律升级到 tool calling,
> 结果它**完全依赖 LLM** —— LLM 限流 (429) 或超时时, 整句话掉到 chat 分支,
> 机器人什么都不做。位置排序虽然不如模型懂时序, 但「先A再B」这种最常见的
> 说法位置排序就是对的, 没必要为它付 LLM 的延迟与失败风险。
> 实测延迟从 800~3000ms 降到 26~57ms。

> 规则路径只能按**位置**排序, 拿不准「挥完手再准备点」这种带明确时序的
> 复杂说法 —— 那类说法目前仍会走 tool calling 路径。

### 说法的匹配规则

匹配命中**最长的说法** —— 说「伸左手」时「伸左手」(3字) 赢过「伸手」(2字)。
所以 `say` 列表的排列顺序**不影响结果**, 加新说法不会意外改变已有匹配。

**复合指令**用另一套规则 (`_match_all`): 命中句子里**所有**动作, 按**出现位置**
排序, 同一个动作去重 (「挥挥手」同时命中「挥手」和「挥挥手」两条说法, 只算一个)。

`check_actions.py` 会校验: 没有两条完全相同的说法指向不同动作、包含关系能被
最长优先消解、配置的动作与动作库双向对齐。

### 已修的两个 bug

1. **router 的 exclude 是全局生效的** —— 之前所有规则的 exclude 汇总成一个列表,
   tool 规则的「介绍」会误伤 device 规则。现在改成**按规则隔离**。
2. **arm_service 从不上使能** —— 机器人下使能时入控被控制器拒
   (24077 「机器人 X 未上使能, 不能入控」), 表现为「下发成功但机械臂不动」。
   现在播放前会自动补使能 (等 3 秒, 失败则明确报错)。

### 测试

```bash
# 说法匹配 + 分支隔离 (不需要服务)
cd services/agent_service && python tools/check_device.py

# 加上真实下发 (需要 agent_service 与 arm_service 运行中)
cd services/agent_service && python tools/check_device.py --live

# 完整链路 (四个服务都要在跑)
python tools/check_voice_arm.py
```

### 加新动作

1. 在 `services/arm_service/data/` 放 `<动作名>.json`
2. 在 `configs/actions.yaml` 加一条 `- action: "<动作名>"` + `say` 说法
3. 重启 arm_service 与 agent_service

动作名两边必须对得上 —— `configs/actions.yaml` 里的 `action` 必须是
`data/` 下真实存在的文件名, 否则 arm_service 会返回 404。

