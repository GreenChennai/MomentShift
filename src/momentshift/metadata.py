"""Application metadata for MomentShift (瞬变工坊).

职责边界：
- 做：集中存放版本号、应用名、作者与仓库地址等常量。
- 不做：不导入任何项目内模块，保证谁都能安全引用它而不产生循环导入。

依赖：无；被依赖：__init__、app_bootstrap、gui/about_interface、quick_runner。

All user-facing strings that are *not* localized at runtime (e.g. repo URLs,
author) live here. Localized UI strings come from :mod:`momentshift.i18n`.
"""

APP_NAME = "MomentShift"
APP_NAME_ZH = "瞬变工坊"
# v0.8.26：版本号 = 迭代版本号 + commit 短哈希（本地构建标识，不推送 GitHub）。
VERSION = "0.8.26-b696e94"
AUTHOR = "GreenChennai"

REPO_URL = "https://github.com/GreenChennai/MomentShift"
ISSUE_URL = REPO_URL + "/issues"
RELEASE_URL = REPO_URL + "/releases/latest"

DESCRIPTION = "ffmpeg 多媒体工具箱"
COPYRIGHT = f"Copyright © 2026 {AUTHOR}"
