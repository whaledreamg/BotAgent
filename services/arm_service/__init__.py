"""机械臂动作服务。

参考 ActionPlanner 的 src/timeline/ 与 src/robot/ 实现, 把动作序列播放从
GUI 程序里剥离出来, 做成与 TTS / ASR / Agent 并列的独立服务。

模块:
    config.py      配置加载 (configs/arm.yaml)
    models.py      请求/响应模型
    library.py     动作库加载与索引
    service.py     播放编排
    main.py        FastAPI 入口
    timeline/      时间轴: 数据模型 / 插值 / 播放引擎
    robot/         控制面连接 / 关节流发送 / 实时控制状态机
"""
