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

`action` 与 `actions` **二选一**, 都给时以 `actions` 为准。两个都不给返回 HTTP 400。

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

