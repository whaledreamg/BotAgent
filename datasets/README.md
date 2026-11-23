# datasets/ —— 数据集与训练产物

视觉模型的训练数据集、训练输出目录放这里。

**目录本身入库，实际的数据（图片几百 MB）不入库**。
运行时由 [`services/vision_service/paths.py`](../services/vision_service/paths.py)
按「`@` 相对仓库根 → 环境变量 → 约定目录」的顺序去找。

## 结构

```
datasets/
├── myobjects/           # 自定义物体数据集
│   ├── data.yaml        # ultralytics 的数据集描述
│   ├── images/
│   └── labels/
└── runs/                # 训练输出 (train/val 权重、混淆矩阵等)
```

## 怎么配

`configs/vision.yaml`：

```yaml
training:
  data: '@datasets/myobjects/data.yaml'
  base_weights: '@models/yolo/yolo26n.pt'
  project: '@datasets/runs'
```

数据太大就指到外面：

```bash
export VISION_DATA_DIR=/data/datasets
```

## 生成数据集

```bash
cd services/vision_service

# 采图
python tools/capture_dataset.py --out ../../datasets/myobjects --count 300

# 建目录结构 + 生成 data.yaml
python tools/setup_dataset.py --out ../../datasets/myobjects --names 杯子 瓶子
```

## 训练

```bash
cd services/vision_service && python tools/train_model.py
```

训练完 `auto_apply: true` 会把 `best.pt` 写回 `detector.weights`。
