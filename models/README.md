# models/ —— 模型权重

视觉服务（YOLO 检测/姿态）用的权重放这里。

**目录本身入库，实际的 `.pt` 文件不入库**（几十 MB，不适合进 git）。
运行时由 [`services/vision_service/paths.py`](../services/vision_service/paths.py)
按下面的顺序去找：

1. **配置里的 `@` 相对路径** —— `configs/vision.yaml` 里写 `@models/yolo/yolo26n.pt`
2. **环境变量** —— `VISION_WEIGHTS`（单个文件）/ `VISION_MODEL_DIR`（整个目录）
3. **约定目录** —— 仓库 `models/`、`~/workspace/data/model/` 等

## 放什么

```
models/
└── yolo/
    └── yolo26n.pt          # 检测模型
    └── yolo26n-pose.pt     # 姿态模型 (可选)
```

## 怎么配

`configs/vision.yaml`：

```yaml
detector:
  weights: '@models/yolo/yolo26n.pt'    # @ = 相对仓库根
  pose_weights: ''                       # 留空 = 不用姿态
```

大文件不想进仓库就指到外面：

```bash
export VISION_MODEL_DIR=/data/models
```

## 检查是否就位

```bash
cd services/vision_service && python -c "from paths import describe; print(describe())"
```
