"""标定模块: 内参标定 + 手眼标定。

- ``board.py``       ChArUco 板的生成与检测
- ``intrinsics.py``  内参标定 (重标相机出厂内参)
- ``handeye.py``     手眼标定 (板挂工具, 用控制器正解当量尺)
- ``capture.py``     采集辅助 (相机取帧 + 控制器位姿查询)
"""

from __future__ import annotations

__all__ = ["board", "intrinsics", "handeye", "capture"]
