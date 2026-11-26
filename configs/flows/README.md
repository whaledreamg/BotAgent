# 流程定义 (configs/flows/)

这里放**流程** —— 一条流程由哪些**片段**、按什么顺序组成。

每个 `.yaml` 文件是一条流程。文件名即流程名 (`grasp.yaml` -> `grasp`)。

```yaml
name: grasp
description: 一句话说明这条流程干什么
steps:
  - id: ready
    type: arm
    action: 准备点
    label: 回准备点
```

## 按物品分流 (routing.yaml)

不同物品走不同流程 —— 「抓瓶子」走 `grasp_bottle.yaml`, 没配过的走
默认的 `grasp.yaml`。

分流表是同目录下的 **`routing.yaml`** (它不是流程, 加载流程时会跳过它):

```yaml
# 键 = 物品名 (用户说的那个词, 如「抓瓶子」-> 瓶子)
# 值 = 流程名 (本目录下的文件名, 不带 .yaml)
routing:
  瓶子: grasp_bottle

# 全局兜底流程。必须是本目录里存在的流程名。
# 留空 = 用服务端的默认流程 (grasp)。
default_flow: grasp
```

### 三级优先级

选流程时按这个顺序, **高的覆盖低的**:

| 优先级 | 来源 | 什么时候用 |
|---|---|---|
| 1 | `grasp_object` 的 `flow` 参数 | 有人**明确指定**了流程 |
| 2 | `routing.yaml` 的 `routing` 映射 | 物品名在表里 |
| 3 | `routing.yaml` 的 `default_flow` | 都没命中 —— 全局兜底 |

### 几个要点

- **分流在拍照之前就决定** —— 拿物品名查表, 不用等拍完照才知道抓的是什么。
- **物品名会先剥修饰词** —— 「那个瓶子」「一个瓶子」都当成「瓶子」查。
- **不做模糊匹配** —— 猜错了比不猜更糟 (走错流程可能抓不稳), 走默认
  流程至少是条能用的通用流程。
- **映射指向不存在的流程 -> 静默回落到默认流程** —— 流程文件还没建
  就先把映射写上了, 这种错在运行时只表现为「走错流程」, 很难查。
  所以 `check_flows.py` 会静态查出这种不一致。
- **显式指定的流程不存在 -> 不回落** —— 用户要的是那条流程, 没了就该
  如实报错, 而不是悄悄换成别的。

### 加一条分流

1. 写流程文件 (如 `configs/flows/grasp_cup.yaml`)
2. 在 `routing.yaml` 的 `routing` 段加一行 `杯子: grasp_cup`
3. 跑 `python3 services/arm_service/tools/check_flows.py` 验证

**不用改任何 Python 代码。**

## 扩展边界 (先看这个)

流程文件能通用到什么程度 —— **哪些改动不用碰代码**:

| 层次 | 定义在哪 | 改它要改 Python 吗 |
|------|----------|--------------------|
| 片段**内容** | YAML: 有几段、什么顺序、动作叫什么名 | **不用** |
| 片段**参数** | YAML: 距离、时长、偏移、具名点 | **不用** |
| 片段**类型** | Python 注册表: `arm`/`hand`/`detect`/`target` | **要** |

一句话: **新流程 / 改流程 = 只写 YAML**；**新片段类型 = 要写 Python**。

具体能做什么:

| 想做的事 | 改哪里 | 要改 Python 吗 |
|----------|--------|----------------|
| 抓取改成「拍两次照」 | 加一个 `detect` 段 | 否 |
| 抓取后先抬 10cm 再回安全点 | 加一个 `arm` 段 (`point:` + `offset`) | 否 |
| 换准备点 / 安全点用的动作 | 改 `action:` 的值 | 否 |
| 上方点高度从 5cm 改成 10cm | 改 `target` 片段的 `offset: {z: ...}` | 否 |
| 改拍照/抓取姿态 | 改 `detect` 片段的 `orientation` | 否 |
| 抓完顺便把夹爪张开 | 加 `type: hand` 段 (`direction: open`) | 否 (类型已内置) |
| 「先移到 A 点, 再从 A 点上方抓」 | 声明两个具名点, 一个 `base` 另一个 | 否 |

**目前还表达不了的** (架构留了口子, 但需要先补服务端能力):

- 「放在桌上」这类流程 —— 需要「具名放置点」概念 (现在动作库里只有
  准备点 / 安全点 / 零点 / 打包点 / 挥手 / 伸左手 / 伸右手)
- 「等 2 秒」这类延时步骤 —— `detect` 段有 `duration`, 但它同时会触发
  识别, 不能当纯延时用
- 调任意 HTTP 接口的片段 —— 需要新片段类型

## 片段类型

| type | 作用 | 终点/取值来源 |
|------|------|--------------|
| `arm` | 关节片段 (机械臂动) | `action` / `pose` / `point` / `base` **四选一** |
| `hand` | 手部片段 (灵巧手动) | `action` / `pose` / `preset` 至少一个 |
| `detect` | 识别片段 —— 产出识别结果 | `duration` (在时间轴上占多久) + `orientation` (拍照姿态) |
| `target` | **具名点声明** —— 纯声明, 不动 | `source: detect` / `base`+`offset` / `pose` 三选一 |

### `arm` 片段的四种终点

| 字段 | 含义 |
|------|------|
| `action` | 终点来自动作库 (如 `准备点`) —— 自动带动作库里的时长与关节角 |
| `pose` | 显式关节角 `{"left": [7 个], "right": [7 个]}` —— **不走逆解** |
| `point` | 引用一个具名点 -> 走本地逆解到那个点 |
| `base` | 基于另一个具名点再偏移 (等价于 `point` + `offset`) |

四者**互斥** —— 同时给两个会报错。

### `hand` 片段的三种手位

| 字段 | 含义 |
|------|------|
| `action` | 动作库里的动作名 (如 `闭合夹爪`) |
| `pose` | 6 个 0~1000 的手位 —— **必须再给 `direction`** |
| `preset` | 手势名 (如 `一把抓`) |

`direction` 只能是 `close` 或 `open`。**纯 `pose` 时必须给** ——
6 个数字看不出是合还是开。

### `detect` 片段的拍照姿态

`detect` 片段可以带 `orientation`, 规定**拍照时手是什么姿态**:

```yaml
- id: detect
  type: detect
  duration: 1.5
  label: 拍照识别
  orientation:
    right:
      a: 1.6683
      b: 0.6821
      c: 1.9317
    left:
      a: -1.6900
      b: 0.5548
      c: -1.9033
```

**拍照姿态 = 抓取姿态** —— 机械臂从拍照时的姿态直接抓过去, 中间不换姿态。

**为什么必须一致**: 抓取流程第 1 步播「准备点」, 然后拍照, 然后从那个
姿态抓。如果抓取姿态跟拍照姿态不是一回事, 机械臂得先转到另一个姿态,
而那个姿态在目标点很可能**根本不可达** —— 历史上每次抓瓶子都报
`unreachable` 就是这么来的 (示教姿态跟准备点差了 107 度)。

**优先级**: 流程里写了用流程的, 没写才用 `configs/arm.yaml` 的
`grasp.orientation`。所以流程文件是**就近覆盖**, 两条路都能用。

**必须是字典** —— 写成 YAML 的集合语法 `right: {1.6683, 0.6821, 1.9317}`
会被解析成 `{1.6683: null, ...}`, 姿态静默丢掉。加载时会明确报错。

**换准备点动作后要重新算这两个值**:

```bash
python3 services/arm_service/tools/check_grasp_reachable.py --scan
```

## 具名点 (target)

具名点是**纯声明** —— 登记一个点, 不让机械臂动。

**核心约束**: 具名点的值来自识别结果, 而**识别之前不知道拍到的是什么**。所以:

- `detect` 片段负责**产出**识别结果
- `target` 片段负责**消费**识别结果, **必须排在 `detect` 之后**
- 具名点的声明**不假设识别到的是什么** —— 只声明「识别到的东西, 叫这个名字」
  (物体名由 skill 的 `target` 参数决定, 用户说「抓瓶子」)

### 三种取值方式

| 写法 | 含义 |
|------|------|
| `source: detect` | 取识别结果的位置 |
| `base: <点名>` + `offset` | 基于另一个具名点再偏移 |
| `pose: {x, y, z}` | 写死的坐标 |

### 引用方式

引用是 `point: <步骤id>` —— **按名字引用, 位置任意**。

```yaml
steps:
  - id: detect
    type: detect
    duration: 1.5
    label: 拍照识别

  # 具名点声明 —— 纯登记, 不动
  - id: object
    type: target
    source: detect
    label: 目标物体

  - id: above_point
    type: target
    base: object
    offset: {x: 0.0, y: 0.0, z: 0.05}
    label: 目标上方

  # 引用具名点去做别的事
  - id: reach_above
    type: arm
    point: above_point
    duration: 3.0
    label: 移到目标上方

  - id: grasp_at
    type: arm
    point: object
    duration: 2.0
    label: 移到抓取点
```

**具名点可以声明了却根本不引用** —— 不报错。

### 一次声明多个点

`target` 片段支持 `points` 列表, 一次登记多个点:

```yaml
  - id: points
    type: target
    points:
      - id: object
        source: detect
        label: 目标物体
      - id: above_point
        base: object
        offset: {x: 0.0, y: 0.0, z: 0.05}
        label: 目标上方
```

## 顺序与引用约束

加载流程时会校验这几条, **任何一条不满足都会明确报错**:

1. **引用必须能解析** —— 引用不存在的点名要报错
2. **引用必须指向 `target` 片段** —— 不能引用一个 `arm` 片段
3. **`source: detect` 的点必须排在 `detect` 之后** —— 识别之前拿不到值
4. **`base` 的点必须排在被引用的点之后** —— 值要算得出来
5. **不能循环引用** —— A 基于 B, B 又基于 A

## 新旧格式对照

| 旧 (`play`/`move`/`close`/`open`) | 新 (`arm`/`hand`/`detect`/`target`) |
|-------------------------------|-----------------------------------|
| `type: play` + `action: 准备点` | `type: arm` + `action: 准备点` |
| `type: move` + `target: object` | `type: arm` + `point: object` (先声明 `target` 片段) |
| `type: move` + `target: above` | `type: arm` + `point: above_point` (基于 `object` + `z` 偏移) |
| `type: close` | `type: hand` + `action: 闭合夹爪` (或 `direction: close`) |
| `type: open` | `type: hand` + `action: 张开夹爪` (或 `direction: open`) |
| `type: detect` | `type: detect` (不变) |
| `move` 的 `offset` 直接写在 `move` 上 | 偏移写在引用它的 `arm` 片段上, 或写在 `target` 片段上 |

**旧格式不再被接受** (hard break) —— 加载时明确报
`未知片段类型 'play'`, 不会静默跳过。

## 失败语义

执行器对失败的处理是**按片段类型**定的, 不随流程改:

- `detect` 失败 -> 直接返回失败 (此时手还在准备点, 那是安全的地方)
- 中途 `arm` 失败 -> **先回安全点**再报失败 (否则手停在半空)
- 流程最后一段失败 -> 前面的片段已成功, 如实说「抓到了但收尾失败」
- 控制总闸关着 (`configs/arm.yaml` 的 `arm.enabled`) -> **不算失败**,
  流程照走完, 只是不动

## 校验

改完流程文件后跑一下自检:

```bash
conda run -n botagent python services/arm_service/tools/check_flows.py
```

它会检查: 文件格式、片段类型是否已知、动作名是否在动作库里存在、
引用与顺序约束 (引用不存在、引用类型不对、`source: detect` 排在
`detect` 之前、`base` 排在基准之前、循环引用 —— 每种都要报错)、
热重载的指纹逻辑, 以及新流程与旧 `configs/arm.yaml` 配置的行为是否等价。

## 热重载

**改完 yaml 不用重启服务** —— 服务每次用到流程时都会比对目录指纹
(每个 yaml 的 mtime + 大小), 变了就重读。

所以下面这些操作都会立刻生效:

- `GET /v1/flows` / `GET /v1/flows/{name}` —— 查流程定义时
- 每次抓取 (`/v1/grasp_sequence`) —— 合成序列时

日志里会看到:

```
[流程] 目录有变动 ((('grasp.yaml', 1795598774306972871, 4407),)
    -> (('grasp.yaml', 1795598943043146939, 4410),)), 重新载入
[流程] 载入 grasp (8 步): ready -> detect -> object -> above_point -> ...
```

**兜底**: 同一秒内改文件、且大小没变时, 指纹可能察觉不到 (有些文件系统
mtime 精度只到秒)。这时候手动重载:

```bash
curl -X POST localhost:8802/v1/flows/reload
```

它会无视指纹强制重读, 并返回当前指纹。

> **注意**: agent 服务那边**也有自己的缓存** —— `vision.grasp` skill 把
> 流程定义缓存在 skill 实例里。它每次抓取都新建 skill 实例, 所以抓取时
> 总是重新拉一次; 但如果你在 skill 之外改了 yaml 又期待立刻生效, 先
> `POST /v1/flows/reload` 一下更保险。
