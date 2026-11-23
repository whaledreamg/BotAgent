# arm_service — 机械臂动作服务

参考 `ActionPlanner` 的 `src/timeline/` 与 `src/robot/` 实现, 把动作序列播放
从 GUI 程序里剥离出来, 做成与 TTS / ASR / Agent 并列的独立服务。

完整文档见 [`docs/arm-service.md`](../../docs/arm-service.md)。

## 快速开始

```bash
# 启动 (conda 环境 botagent, 端口 8802)
../scripts/start_arm.sh

# 看动作库
curl -s http://127.0.0.1:8802/v1/actions | python -m json.tool

# 播一个动作
curl -s -X POST http://127.0.0.1:8802/v1/play \
  -H 'Content-Type: application/json' -d '{"action":"挥手"}'

# 跑一次安全门阀 (不执行动作)
curl -s http://127.0.0.1:8802/v1/safety | python -m json.tool

# 停止
curl -s -X POST http://127.0.0.1:8802/v1/stop
```

## 动作库: 一个 JSON 文件 = 一个动作

**文件名就是动作名** (去掉 `.json`):

```
data/挥手.json   ->  动作 "挥手"
data/零点.json   ->  动作 "零点"
data/准备点.json ->  动作 "准备点"
```

当前动作库: `挥手` / `准备点` / `打包点` / `零点` / `伸左手` / `伸右手`。

文件里的 `segments` 是这个动作的轨迹片段, 首尾相连构成连续运动。每段只记
录**结束关节角**, 第一段的起点取播放那一刻的实测姿态。

新增动作: 往 `data/` 放一个 `<动作名>.json` 即可, 重启服务自动加载。
再在 `configs/actions.yaml` 加一条说法映射, 语音就能控制它。

旧版的大文件 (`action.json` / `sport.json` / `test.json`) 是整场编排, 不是动作,
已通过 `arm.exclude_files` 排除。用 `tools/split_actions.py` 可以把它们拆成
单动作文件。

## 目录结构

```
services/arm_service/
├── main.py              # FastAPI 入口 (HTTP + WebSocket)
├── config.py            # 配置加载 (configs/arm.yaml + 环境变量)
├── models.py            # 请求/响应模型
├── service.py           # 播放编排 (入控 -> 对齐 -> 播放 -> 停控)
├── library.py           # 动作库加载与索引 (一个 JSON 文件 = 一个动作)
├── safety.py            # 安全门阀 (状态检查 + 拒绝原因 + 播报文案)
├── notifier.py          # 门阀拒绝时的 TTS 播报 (WS, 不落盘)
├── timeline/
│   ├── model.py         # Segment / TimelineModel
│   ├── interpolation.py # linear / cubic / spline (Hermite) 插值
│   └── playback.py      # 时间轴推进引擎
├── robot/
│   ├── connection.py    # WebSocket 控制面客户端
│   ├── streamer.py      # UDP 关节流发送线程
│   └── controller.py    # 实时控制状态机 (入控/对齐/停控)
├── data/                # 动作序列 JSON (来自 ActionPlanner/data)
└── tools/
    ├── check_arm.py         # 协议级自检 (mock, 不需要真机)
    ├── check_arm_service.py # 端到端自检 (mock 控制器 + 真实服务)
    ├── check_safety.py      # 安全门阀自检 (不需要真机)
    ├── check_control_gate.py # 控制指令总闸自检 (mock, 不需要真机)
    └── check_move_to.py     # 抓取移动自检 (mock, 不需要真机)
```

## 配置

`configs/arm.yaml`:

| 键 | 默认 | 说明 |
|----|------|------|
| `arm.controller_ws` | `ws://192.168.100.16:9000` | 控制器控制面 |
| `arm.command_port` | `9030` | UDP 指令面端口 (以控制器回读为准) |
| `arm.frequency` | `100` | 关节流频率 (Hz), 协议允许 10~1000 |
| `arm.lead_in_ms` | `700` | 从实测角过渡到轨迹起点的时长 |
| `arm.left_arm` / `right_arm` | `""` | 留空则连接后用 `robot/list` 自动识别 |
| `arm.joint_limit.enable` | `true` | 关节级限幅, **协议缺省是关闭的, 不要关** |
| `arm.action_dir` | `services/arm_service/data` | 动作库目录 |
| `arm.exclude_files` | `[]` | 要跳过的文件名 (留空 —— 动作库都是单动作文件) |
| `arm.enabled` | `true` | **控制指令总闸**。false = 只算不动, 一个控制指令都不下发 |
| `arm.grasp.orientation` | `{}` | 示教的抓取姿态 (a/b/c, rad)。**留空 = 保持当前 TCP 姿态** |

环境变量覆盖: `ARM_CONTROLLER_WS` / `ARM_SERVER_PORT` / `ARM_FREQUENCY` /
`ARM_ENABLED` 等。

## 抓取移动 (`POST /v1/move_to`)

视觉识别到物体后, 把 TCP 移到物体上方的预抓取点。

### 抓取姿态: 留空 = 保持当前姿态

`configs/arm.yaml` 的 `arm.grasp.orientation` 留空时, **保持当前 TCP 姿态**,
只挪位置 —— 这是默认且推荐的做法。

姿态一变, 七个关节全得跟着动, 很容易撞软限位被控制器停掉。

**别填 `0,0,0` 当占位** —— 那是「姿态全零」不是「不指定」。机械臂当前姿态
若是 `(2.97, 0.74, -3.00)`, 硬转到全零要转 **138 度**, 关节角大幅变化,
必然把某个关节推出软限位 (报 `13101` 关节位置超出软限位)。

要固定抓取姿态, 先手动摆好再抄值:

```bash
curl -s http://127.0.0.1:8802/v1/kinematics | python -m json.tool | grep -A 8 cartesian
```

### 下发前的规划检查

任何一项不过, **一个控制指令都不发** (机械臂完全不动):

| code | 含义 |
|------|------|
| `disabled` | 控制指令总闸关着 (`arm.enabled=false`) —— 只算位置 |
| `out_of_reach` | 目标离基座超过 `arm.grasp.max_reach` |
| `safety` | 安全门阀不通过 (急停 / 故障 / 手动模式 / 未使能) |

### 排查「被安全停掉」

`stoppedBySafety` 是控制器的笼统说法, 真正原因在控制器告警里:

```bash
# 查控制器告警 (13101 = 关节超软限位, 13105 = 末端过速)
# 用 log/queryLog 接口, 或看机械臂本体的 HMI
```

两个常见原因:

**关节撞到软限位** —— 控制器用的是**解析逆解** (`cartesianIkPath: analytic`),
逆解会自己挑一组关节角。挑到贴限位的解时, 运动途中关节就被推过软限位。
**先手动把臂收回安全姿态**再试。

另外 `maxArmAngleDelta` 用的是控制器缺省的 `0.2`, 别调小 —— 它限的是每包
臂型角的变化量, 控制器靠臂型角**让位**来躲开关节限位, 让位跟不上就会撞限位。
