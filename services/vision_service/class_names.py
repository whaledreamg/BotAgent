"""中文物体名 -> YOLO 类别名 的映射。

YOLO 的 COCO 类别是英文 ("cup", "bottle", "person"), 用户说的是中文。
这个表把常见的中文说法翻过去。

**没命中映射的直接原样传过去** —— 视觉服务那边会用 YOLO 的原始类名
匹配, 所以英文类名 (或自定义训练模型的类名) 也照样能用。
"""

from __future__ import annotations

from typing import Dict, List, Optional

#: 中文 -> YOLO 类别名
ZH_TO_CLASS: Dict[str, str] = {
    # 人
    "人": "person",
    "人们": "person",
    "行人": "person",
    "人脸": "person",
    # 常见容器
    "杯子": "cup",
    "水杯": "cup",
    "马克杯": "cup",
    "茶���": "cup",
    "咖啡杯": "cup",
    "瓶子": "bottle",
    "水瓶": "bottle",
    "饮料瓶": "bottle",
    "罐子": "can",
    "易拉罐": "can",
    "杯子们": "cup",
    # 食物
    "苹果": "apple",
    "香蕉": "banana",
    "橙子": "orange",
    "橘子": "orange",
    "柠檬": "lemon",
    "葡萄": "grapes",
    "西瓜": "watermelon",
    "草莓": "strawberry",
    "菠萝": "pineapple",
    "面包": "bread",
    "三明治": "sandwich",
    "汉堡": "hamburger",
    "披萨": "pizza",
    "甜甜圈": "donut",
    "蛋糕": "cake",
    "饼干": "cookie",
    # 蔬菜
    "胡萝卜": "carrot",
    "土豆": "potato",
    "花椰菜": "broccoli",
    "西兰花": "broccoli",
    # 家具
    "椅子": "chair",
    "凳子": "chair",
    "桌子": "dining table",
    "餐桌": "dining table",
    "书桌": "dining table",
    "沙发": "couch",
    "床": "bed",
    "电视": "tv",
    "显示器": "tv",
    "笔记本电脑": "laptop",
    "键盘": "keyboard",
    "鼠标": "mouse",
    "遥控器": "remote",
    "手机": "cell phone",
    "电话": "cell phone",
    "书": "book",
    "花瓶": "vase",
    "剪刀": "scissors",
    # 家电
    "空调": "air conditioner",
    "微波炉": "microwave",
    "烤箱": "oven",
    "冰箱": "refrigerator",
    "洗衣机": "washer",
    "电风扇": "fan",
    # 动物
    "猫": "cat",
    "小猫": "cat",
    "狗": "dog",
    "小狗": "dog",
    "鸟": "bird",
    "马": "horse",
    "牛": "cow",
    "羊": "sheep",
    "猪": "pig",
    # 交通工具
    "汽车": "car",
    "车": "car",
    "自行车": "bicycle",
    "摩托车": "motorcycle",
    "公交车": "bus",
    "卡车": "truck",
    "飞机": "airplane",
    "火车": "train",
    "船": "boat",
    # 其他
    "背包": "backpack",
    "手提包": "handbag",
    "行李箱": "suitcase",
    "行李": "suitcase",
    "雨伞": "umbrella",
    "球": "sports ball",
    "棒球棒": "baseball bat",
    "滑板": "skateboard",
    "滑雪板": "skis",
    "冲浪板": "surfboard",
    "网球拍": "tennis racket",
    "瓶子们": "bottle",
    "球拍": "tennis racket",
    "泰迪熊": "teddy bear",
    "泰迪": "teddy bear",
    "熊": "teddy bear",
    "钟": "clock",
    "花瓶儿": "vase",
    "婴儿车": "stroller",
    "滑雪板儿": "skis",
    "发卡": "hair drier",
    "吹风机": "hair drier",
    "计算器": "calculator",
    "蜡烛": "candle",
    "酒瓶": "bottle",
    "咖啡机": "cup",
    "灭火器": "fire hydrant",
    "路牌": "stop sign",
    "停车计时器": "parking meter",
    "长椅": "bench",
    "长凳": "bench",
}


def translate(name: str) -> str:
    """中文物体名 -> YOLO 类别名。

    没命中就原样返回 (英文类名、自定义模型的类名都能直接用)。
    """
    if not name:
        return name
    key = name.strip()
    if not key:
        return key

    # 完全匹配
    if key in ZH_TO_CLASS:
        return ZH_TO_CLASS[key]

    # 英文 / 数字组成的 —— 大概率已经是类名了, 原样返回
    if all(ord(c) < 128 for c in key):
        return key

    # 逐字剥前缀/后缀的常见修饰词后重试
    for prefix in ("那个", "这个", "一只", "一个", "台", "个", "只"):
        if key.startswith(prefix) and len(key) > len(prefix):
            trimmed = key[len(prefix):]
            if trimmed in ZH_TO_CLASS:
                return ZH_TO_CLASS[trimmed]
            key = trimmed
            break

    return name


def candidates(name: str) -> List[str]:
    """所有可能的类名候选 (原名 + 翻译)。

    视觉服务那边会挨个试, 避免「保温杯」这种组合词查不到。
    """
    out: List[str] = []
    raw = (name or "").strip()
    if raw:
        out.append(raw)
    mapped = translate(raw)
    if mapped and mapped not in out:
        out.append(mapped)
    return out


__all__ = ["ZH_TO_CLASS", "candidates", "translate"]
