"""内置工具版本号。

职责边界：
- 做：以常量形式记录随包分发的 oxipng / jpegoptim / gifsicle 版本号。
- 不做：不探测磁盘上二进制的真实版本；升级内置工具时需手工同步此处。

依赖：无；被依赖：gui/about_interface（展示用）。"""

OXIPNG_VERSION = "10.1.1"
JPEGOPTIM_VERSION = "1.5.6"
