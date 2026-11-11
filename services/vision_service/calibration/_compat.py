"""跨包导入兼容层。

``calibration`` 这个包既可能被当作 ``vision_service.calibration`` 导入
(服务内部), 也可能被当作顶层 ``calibration`` 导入 (tools/ 下的脚本会把
``services/vision_service`` 直接加进 sys.path)。

两种情况下 ``from ..extrinsic import ...`` 的含义不同 —— 前者能找到,
后者会报 "attempted relative import beyond top-level package"。

这里统一处理: 优先试相对导入, 失败就退回绝对导入。
"""

from __future__ import annotations

try:  # 包内导入 (vision_service.calibration)
    from ..extrinsic import (  # type: ignore
        AXIS_PERMUTATION_CAMERA_TO_BASE,
        invert_transform,
        make_transform,
        matrix_from_pose,
        matrix_to_dict,
        matrix_to_rpy,
        rotation_error_rad,
        rpy_to_matrix,
        transform_points,
    )
except ImportError:  # 顶层导入 (calibration)
    from extrinsic import (  # type: ignore
        AXIS_PERMUTATION_CAMERA_TO_BASE,
        invert_transform,
        make_transform,
        matrix_from_pose,
        matrix_to_dict,
        matrix_to_rpy,
        rotation_error_rad,
        rpy_to_matrix,
        transform_points,
    )


__all__ = [
    "AXIS_PERMUTATION_CAMERA_TO_BASE",
    "invert_transform",
    "make_transform",
    "matrix_from_pose",
    "matrix_to_dict",
    "matrix_to_rpy",
    "rotation_error_rad",
    "rpy_to_matrix",
    "transform_points",
]
