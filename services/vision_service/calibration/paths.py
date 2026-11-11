"""路径解析 —— 找到仓库根目录。

标定工具的输出要落到 ``configs/`` 下, 而工具脚本在
``services/vision_service/tools/`` 里。硬编码 ``parents[N]`` 很容易数错
(工具挪个目录就找不到配置了), 所以这里**向上搜索**仓库根 —— 判据是
「存在 ``configs/vision.yaml``」。

同理, 标定结果也应该能通过环境变量覆盖 (``VISION_CONFIG_DIR``),
方便在不同机器上用不同的配置目录。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: 用来识别仓库根的标志文件
_MARKER = Path("configs") / "vision.yaml"


def find_repo_root(start: Optional[Path] = None) -> Path:
    """向上搜索仓库根目录。

    找不到就退回「start 的上两级」—— 至少不会崩。
    """
    here = Path(start or __file__).resolve()

    # 先看环境变量指定的配置目录, 它的上两级就是仓库根
    override = os.environ.get("VISION_CONFIG_DIR", "").strip()
    if override:
        cfg_dir = Path(override).expanduser().resolve()
        if cfg_dir.is_dir():
            return cfg_dir.parent if cfg_dir.name == "configs" else cfg_dir

    for parent in here.parents:
        if (parent / _MARKER).exists():
            return parent

    # 兜底: tools/ -> vision_service/ -> services/ -> 仓库根
    return here.parents[2] if len(here.parents) > 2 else here.parent


def config_dir(start: Optional[Path] = None) -> Path:
    """``configs/`` 目录的路径。"""
    override = os.environ.get("VISION_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return find_repo_root(start) / "configs"


def config_path(name: str, start: Optional[Path] = None) -> Path:
    """``configs/<name>`` 的路径。"""
    return config_dir(start) / name


__all__ = ["config_dir", "config_path", "find_repo_root"]
