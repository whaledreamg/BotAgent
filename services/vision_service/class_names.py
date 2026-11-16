"""中文物体名 -> YOLO 类别名 的映射。

YOLO 的 COCO 类别是英文 ("cup", "bottle", "person"), 用户说的是中文。
这个表把常见的中文说法翻过去。

**没命中映射的直接原样传过去** —— 视觉服务那边会用 YOLO 的原始类名
匹配, 所以英文类名 (或自定义训练模型的类名) 也照样能用。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

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


def candidates(name: str, known_classes: Optional[Iterable[str]] = None) -> List[str]:
    """所有可能的类名候选 (原名 + 翻译 + 剥修饰词)。

    视觉服务那边会挨个试, 避免「保温杯」这种组合词查不到。

    ``known_classes`` 是模型当前的实际类名 (来自 ``model.names``)。
    给了它就能额外做一件事: **把带修饰词的说法还原成模型里的类名** ——
    比如模型类名是「螺丝」, 用户说「那个螺丝」/「一颗螺丝」, 剥掉
    修饰词后就能对上。

    没给这个参数时行为和以前一样 (只查内置映射表), 所以不影响
    现有调用方。
    """
    out: List[str] = []
    raw = (name or "").strip()
    if raw:
        out.append(raw)

    mapped = translate(raw)
    if mapped and mapped not in out:
        out.append(mapped)

    # ---- 剥修饰词 ----
    #
    # 内置映射表覆盖不到自定义类名 ("螺丝" 不在表里), 但用户的说法
    # 往往带修饰词 ("那个螺丝"、"一个瓶子")。所以拿模型的实际类名
    # 再试一遍剥修饰词。
    if known_classes:
        known = {str(k).strip().lower() for k in known_classes if str(k).strip()}
        for trimmed in _strip_modifiers(raw):
            if not trimmed:
                continue
            low = trimmed.lower()
            # 原样对上
            if low in known and low not in [o.lower() for o in out]:
                out.append(trimmed)
            # 中文说法 -> 英文类名 (模型类名是英文时的翻译)
            trans = translate(trimmed)
            if trans and trans.lower() in known and trans not in out:
                out.append(trans)

    return out


def _strip_modifiers(name: str) -> List[str]:
    """剥掉常见的中文修饰词, 列出所有可能的「裸类名」。

    例: "那个螺丝" -> ["螺丝", "那个螺丝"]
    """
    out: List[str] = []
    if not name:
        return out

    key = name.strip()
    if not key:
        return out

    # 前缀修饰词 (可以叠加, 如 "那个大的螺丝" —— 逐层剥)
    prefixes = ("那个", "这个", "那些", "这些", "一只", "一个", "一颗",
                "一根", "一把", "一台", "一个的", "那个的")
    changed = True
    while changed:
        changed = False
        for p in prefixes:
            if key.startswith(p) and len(key) > len(p):
                key = key[len(p):]
                out.append(key)
                changed = True
                break

    # 后缀修饰词 ("螺丝啊"、"杯子呢")
    for s in ("啊", "呀", "呢", "吗", "吧", "哦", "了"):
        if key.endswith(s) and len(key) > len(s):
            key = key[:-len(s)]
            out.append(key)

    if key and key not in out:
        out.append(key)

    return out


__all__ = ["ZH_TO_CLASS", "candidates", "translate"]
