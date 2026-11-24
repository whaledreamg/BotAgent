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
| GET  | `/v1/flow` | 抓取流程 (5 步) 的配置 —— agent 的 `vision.grasp` 用 |

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

## 抓取流程 (5 步)

`vision.grasp` skill 一次调用跑完这 5 步:

| 步 | 做什么 | 怎么做的 |
|----|--------|----------|
| 1 | 回准备点 | 播动作库的 `准备点` (拍照前固定视角) |
| 2 | 拍照识别 | 视觉服务 `fresh=True` (强制重拍, 不走缓存) |
| 3 | 移到上方点 | `move_to` 目标正上方 `above_distance` 米 |
| 4 | 移到抓取点 + 闭夹爪 | `move_to` 目标 + 偏移, 然后播 `闭合夹爪` |
| 5 | 回安全点 | 播动作库的 `安全点` |

配置在 `configs/arm.yaml` 的 `arm.grasp.flow`:

```yaml
    flow:
      ready_action: "准备点"      # 第 1 步, 留空 = 跳过
      safe_action: "安全点"        # 第 5 步, 留空 = 跳过
      above_distance: 0.10        # 上方点离目标多远 (米)
      grasp_offset:               # 抓取点相对目标的偏移 (米)
        x: 0.0
        y: 0.0
        z: 0.0
      close_action: "闭合夹爪"     # 留空 = 不闭夹爪
      open_action: "张开夹爪"
      durations:
        above: 3.0                # 移到上方点的时长 (秒)
        grasp: 2.0                # 移到抓取点的时长 (秒)
```

**失败收尾** —— 每步都可能单独失败:

- 第 1 步失败 -> 直接返回, 不拍照
- 第 2 步失败 (没找到) -> 返回, **不回安全点** (已经在准备点了)
- 第 3/4 步失败 -> **必须先回安全点**再报失败, 否则手停在半空
- 第 5 步失败 -> 抓取已成功, 如实说「抓到了但回安全点失败」

**总闸关着的时候 5 步全走完** (`arm.enabled: false`) —— 只是不动。
用户要看的正是每一步算出来的位置, 中途就停掉等于什么都看不到。

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

   **手部字段 (灵巧手)** —— 只有 `configs/arm.yaml` 的 `arm.hand.enabled`
   打开时才会带:
   `{"robot":"RightArm","controlSpace":0,"commandMode":0,"value":[7 个 rad],"hand":[6 个 0~1000]}`

   > ⚠️ **默认关着**。控制器认不认 `hand` 这个字段名还没在真机上验证过 ——
   > 格式不对的话控制器会**整包丢弃** (`invalidDatagram`), 那时候连手臂
   > 都不动了, 比不动手指糟得多。验证脚本: `tools/probe_hand.py`
   > (真机: `--controller <ip> --arm right`)。确认接受后再打开配置。

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
python tools/check_grasp_flow.py   # 抓取流程: 手部字段 / 插值 / 5 步顺序
```

**真机探针** (需要能连到控制器):

```bash
# 验证控制器认不认 UDP 报文里的手部字段
python tools/probe_hand.py --controller 192.168.100.16 --arm right

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

