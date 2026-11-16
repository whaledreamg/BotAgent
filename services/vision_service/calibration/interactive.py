"""交互式工具的公共辅助。

**为什么需要这个**

标定工具是**交互式**的 —— 每摆一个位姿按一次回车。这个交互靠
``input()``, 而 ``input()`` 在 stdin 是 EOF 时直接抛 ``EOFError``。

**最大的坑**: ``conda run`` 默认捕获输出, 会让子进程的 stdin 变成
EOF。于是脚本刚打印提示符就立刻退出, 一个样本都没采到 ——
而现象看起来像「相机坏了」或「机械臂有问题」, 极难定位。

实测::

    $ echo "" | conda run -n yolo python -c "input()"
    EOFError                      <- stdin 被切断

    $ echo "" | conda run --no-capture-output -n yolo python -c "input()"
    (正常读到)                    <- 加上 --no-capture-output 就好了

所以这里抽一个公共检查, 在**开相机之前**就拦住, 并给出明确的修复方法。
"""

from __future__ import annotations

import sys


def stdin_is_interactive() -> bool:
    """stdin 能不能交互 (能读到人按的回车)。

    判定顺序:
    1. 是 tty -> 肯定能交互 (人在终端前)
    2. ``peek()`` 探一下 -> 有数据说明是管道喂的, 也能用
    3. peek 出来是空的 -> **EOF**, 没法交互 (conda run 的典型症状)

    **注意**: 不能用 ``select`` 判断 —— EOF 时 fd 也是「可读」的
    (read 返回 0), 所以 select 会误报。``peek()`` 才能区分
    「有数据」和「已结束」。
    """
    if sys.stdin is None:
        return False

    try:
        if sys.stdin.isatty():
            return True
    except Exception:
        pass

    # peek 一下, 不真的消费掉
    try:
        data = sys.stdin.buffer.peek(1)
        if data:
            # 管道里有数据 —— 可以用 (input() 会读到它)
            return True
        # 空 = EOF
        return False
    except Exception:
        pass

    # peek 不可用 (比如 stdin 被换成 StringIO) —— 保守认为可以,
    # 让真正的 input() 去报错
    return True


def require_interactive_stdin(tool_hint: str = "") -> bool:
    """检查 stdin, 不行就打印修复方法并返回 False。

    ``tool_hint`` 是给用户看的命令示例 (比如
    ``python tools/calib_handeye.py --capture``)。
    """
    if stdin_is_interactive():
        return True

    hint = tool_hint or "python <脚本> --capture"
    bar = "=" * 62
    print(
        f"""
{bar}
  **stdin 不可交互 —— 没法采样**
{bar}

  本工具需要你每摆一个位姿按一次回车, 但当前 stdin 直接是 EOF。

  最常见的原因: **用了 `conda run`** —— 它默认捕获输出,
  子进程的 stdin 会变成 EOF, 于是第一句 input() 就抛 EOFError。

  解决办法 (任选一个):

    # 1. 加 --no-capture-output (推荐)
    conda run --no-capture-output -n yolo {hint}

    # 2. 直接用环境里的 python, 不套 conda run
    /home/gxh/miniconda3/envs/yolo/bin/python {hint}

  (需要先 cd services/vision_service)
{bar}
""",
        file=sys.stderr,
    )
    return False


__all__ = ["require_interactive_stdin", "stdin_is_interactive"]
