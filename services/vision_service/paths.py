"""资源路径解析 —— 模型权重 / SDK 库 / 数据集统统走这里。

**为什么要这个模块**: 之前模型权重、训练数据、Orbbec SDK 全靠 yaml 里
写绝对路径 (``/home/whale/workspace/data/model/yolo/yolo26n.pt``)。换一台
机器、换个用户名就得改一堆配置, 而且改错了报的是「文件不存在」, 很难
看出是路径写错了还是文件真没了。

现在改成**相对路径 + 统一解析**:

1. **仓库内优先** —— 路径以 ``@`` 开头表示「相对仓库根」, 例如
   ``@models/yolo26n.pt``。仓库里放了就在仓库里找。
2. **环境变量覆盖** —— ``VISION_MODEL_DIR`` / ``VISION_DATA_DIR`` /
   ``VISION_SDK_DIR`` 等可以整体指到外部目录 (大文件不适合进 git)。
3. **兜底搜索** —— 在几个约定目录里找同名文件。

**配置里仍然可以写绝对路径**, 只是不再*必须*写 —— 绝对路径依然有效,
但现在是可选项而不是唯一选项。

用法::

    from paths import resolve_weights, resolve_model_dir, sdk_lib_dir

    weights = resolve_weights(cfg.detector.weights)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Optional

#: 仓库根目录 (BotAgent/) —— vision_service/config.py 的上两级
ROOT = Path(__file__).resolve().parents[2]

#: 仓库内放模型/权重的默认位置
DEFAULT_MODEL_DIR = ROOT / "models"

#: 仓库内放数据集的默认位置
DEFAULT_DATA_DIR = ROOT / "datasets"

#: 仓库内放 SDK 编译产物的默认位置
DEFAULT_SDK_DIR = ROOT / "sdk"


def _env_path(name: str) -> Optional[Path]:
    """读环境变量里的路径 (展开 ``~``, 转绝对路径)。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _candidate_dirs(kind: str) -> List[Path]:
    """某类资源的候选目录, 按优先级排。

    顺序 = 「最可能是用户想要的那个」排前面:

    1. 环境变量 (``VISION_MODEL_DIR`` 之类)
    2. 仓库内的默认位置 (``models/`` / ``datasets/`` / ``sdk/``)
    3. 仓库外的常见位置 (``~/workspace/data/model`` 等)
    """
    out: List[Path] = []

    env_names = {
        "model": ("VISION_MODEL_DIR", "MODEL_DIR"),
        "data": ("VISION_DATA_DIR", "DATASET_DIR", "DATA_DIR"),
        "sdk": ("VISION_SDK_DIR", "ORBBEC_SDK_DIR", "SDK_DIR"),
    }
    for name in env_names.get(kind, ()):
        p = _env_path(name)
        if p:
            out.append(p)

    defaults = {
        "model": DEFAULT_MODEL_DIR,
        "data": DEFAULT_DATA_DIR,
        "sdk": DEFAULT_SDK_DIR,
    }
    default = defaults.get(kind)
    if default:
        out.append(default)

    # 仓库外的常见位置 —— 之前 yaml 里写死的就是这些
    home = Path.home()
    out.extend([
        home / "workspace" / "data" / "model",
        home / "workspace" / "data" / "datasets",
        home / "workspace" / "project" / "datasets",
        home / "workspace" / "project" / "OrbbecSDK_v2" / "build" / "linux_x86_64",
        home / "workspace" / "project" / "OrbbecSDK_v2" / "build",
    ])

    return out


def resolve_path(
    raw: str,
    kind: str = "model",
    must_exist: bool = True,
) -> Optional[Path]:
    """把配置里的路径字符串解析成一个真实路径。

    支持的写法:

    - ``""`` / ``None`` -> 返回 None (没配)
    - ``@foo/bar.pt``   -> 相对**仓库根** (``ROOT/foo/bar.pt``)
    - ``~/foo/bar.pt``  -> 相对用户 home
    - ``./foo/bar.pt``  -> 相对**当前工作目录**
    - ``/abs/path``     -> 原样使用
    - ``foo/bar.pt``    -> 先当相对仓库根, 找不到就在候选目录里搜同名文件

    Args:
        raw: 配置里的原始字符串
        kind: 资源类别 ("model" / "data" / "sdk"), 决定候选目录
        must_exist: 是否要求文件真的存在

    Returns:
        解析后的路径 (may_exist=False 时即使不存在也返回推断出的路径)。
    """
    text = str(raw or "").strip()
    if not text:
        return None

    # ---- 显式写法 ----
    if text.startswith("@"):
        # @ = 相对仓库根
        p = ROOT / text[1:].lstrip("/")
        return p if p.exists() else (p if not must_exist else None)

    if text.startswith("~"):
        p = Path(text).expanduser()
        return p if p.exists() else (p if not must_exist else None)

    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        return candidate if candidate.exists() else (candidate if not must_exist else None)

    # 相对路径: 先按相对仓库根解
    p = ROOT / candidate
    if p.exists():
        return p
    if not must_exist:
        return p

    # 再按相对当前工作目录解
    p = Path.cwd() / candidate
    if p.exists():
        return p

    # 最后在候选目录里搜同名文件
    name = candidate.name
    for d in _candidate_dirs(kind):
        hit = d / name
        if hit.exists():
            return hit
        # 也试试子目录 (比如 models/yolo/yolo26n.pt 这种两层)
        if d.is_dir():
            for sub in d.rglob(name):
                if sub.is_file():
                    return sub

    return None


def resolve_weights(raw: str, must_exist: bool = True) -> Optional[Path]:
    """解析检测/姿态模型的权重路径。"""
    return resolve_path(raw, kind="model", must_exist=must_exist)


def resolve_model_dir() -> Path:
    """模型目录 (供训练工具放权重)。"""
    for d in _candidate_dirs("model"):
        if d.is_dir():
            return d
    return DEFAULT_MODEL_DIR


def resolve_data_dir(raw: str = "") -> Path:
    """数据集目录。配置里写了就用它, 否则用默认的。"""
    if raw:
        p = resolve_path(raw, kind="data", must_exist=False)
        if p:
            return p
    return DEFAULT_DATA_DIR


def sdk_lib_dir() -> Optional[Path]:
    """Orbbec SDK 的编译产物目录 (``lib/`` 所在处)。

    视觉服务需要它来把 ``libOrbbecSDK.so.2`` 放进 ``LD_LIBRARY_PATH`` ——
    pip 装的 pyorbbecsdk 自带的那个是**没开 USB PAL** 的, 枚举不到相机。
    """
    override = _env_path("VISION_SDK_LIB_DIR")
    if override and override.is_dir():
        return override

    for d in _candidate_dirs("sdk"):
        lib = d / "lib"
        if lib.is_dir() and (lib / "libOrbbecSDK.so").exists():
            return lib
        if (d / "libOrbbecSDK.so").exists():
            return d
    return None


def sdk_search_paths() -> List[str]:
    """要加进 ``LD_LIBRARY_PATH`` 的目录列表。"""
    out: List[str] = []
    lib = sdk_lib_dir()
    if lib:
        out.append(str(lib))
    return out


def describe() -> dict:
    """给 ``/health`` 用的资源状况摘要 (不泄露绝对路径以外的东西)。"""
    lib = sdk_lib_dir()
    return {
        "model_dir": str(resolve_model_dir()),
        "sdk_lib_dir": str(lib) if lib else "",
        "sdk_found": lib is not None,
    }


# ------------------------------------------------------------ conda 环境


def conda_env_python(env: str = "yolo") -> Optional[str]:
    """找到 conda 环境里的 python 解释器路径。

    标定工具需要**直接**用环境的 python (不能用 ``conda run``, 它会切断
    stdin, 导致采样中断)。之前这些路径是写死的 ``/home/gxh/miniconda3/...``,
    换机器/换用户名就失效了 —— 现在按 ``CONDA_EXE`` / ``CONDA_PREFIX`` /
    ``PATH`` 依次找。
    """
    # 1. 当前 shell 就在 conda 环境里 —— 直接用
    prefix = os.environ.get("CONDA_PREFIX", "").strip()
    if prefix:
        p = Path(prefix) / "bin" / "python"
        if p.exists():
            return str(p)

    # 2. CONDA_EXE 指向的安装根目录
    conda_exe = os.environ.get("CONDA_EXE", "").strip()
    if conda_exe:
        root = Path(conda_exe).resolve().parent.parent
        p = root / "envs" / env / "bin" / "python"
        if p.exists():
            return str(p)

    # 3. PATH 里的 conda
    import shutil

    conda = shutil.which("conda")
    if conda:
        root = Path(conda).resolve().parent.parent
        p = root / "envs" / env / "bin" / "python"
        if p.exists():
            return str(p)

    # 4. 常见的家目录位置
    home = Path.home()
    for p in (
        home / "miniconda3" / "envs" / env / "bin" / "python",
        home / "anaconda3" / "envs" / env / "bin" / "python",
        home / "miniforge3" / "envs" / env / "bin" / "python",
    ):
        if p.exists():
            return str(p)

    return None


__all__ = [
    "ROOT",
    "DEFAULT_MODEL_DIR",
    "DEFAULT_DATA_DIR",
    "DEFAULT_SDK_DIR",
    "resolve_path",
    "resolve_weights",
    "resolve_model_dir",
    "resolve_data_dir",
    "sdk_lib_dir",
    "sdk_search_paths",
    "describe",
    "conda_env_python",
]
