"""MomentShift（瞬变工坊）—— ffmpeg 多媒体工具箱。

职责边界：
- 做：暴露包版本号等顶层元信息。
- 不做：不在此导入任何 GUI 模块，避免 import momentshift 就把 Qt 拉起来。

依赖：metadata；被依赖：全项目。
"""

from .metadata import (
    APP_NAME,
    APP_NAME_ZH,
    AUTHOR,
    COPYRIGHT,
    DESCRIPTION,
    ISSUE_URL,
    RELEASE_URL,
    REPO_URL,
    VERSION,
)

__version__ = VERSION
__all__ = [
    "APP_NAME",
    "APP_NAME_ZH",
    "VERSION",
    "AUTHOR",
    "REPO_URL",
    "ISSUE_URL",
    "RELEASE_URL",
    "DESCRIPTION",
    "COPYRIGHT",
]
