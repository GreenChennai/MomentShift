"""纯格式化函数：字节数、体积对比、耗时、码率解析。

职责边界：
- 做：把数字转成给人看的字符串，以及反过来解析用户/预设里的码率字符串。
- 不做：不依赖 Qt、不读配置、不做 i18n（单位符号 B/KB/MB/GB 与冒号等
  在三种语言下写法一致，所以这里返回的是语言无关的裸串）。

依赖：仅标准库；被依赖：core.compressor、gui.queue_widget、各队列界面。

历史背景：v0.8.0 之前 ``human_size`` 在 ``gui/queue_widget.py`` 与
``core/compressor.py`` 各有一份实现，阶梯与小数位略有出入。
"""

from __future__ import annotations

import re

# 体积阶梯。GB 之后不再进位，避免出现用户看不懂的 TB 级显示。
_SIZE_UNITS = ("B", "KB", "MB", "GB")

# 码率字符串形如 ``20M`` / ``320k``：数值 + 单个单位字符。
_BITRATE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)\s*$")


def human_size(num_bytes: int | float, *, precision: int = 1) -> str:
    """把字节数转成人类可读字符串。

    Args:
        num_bytes: 字节数，负数按 0 处理。
        precision: KB 及以上单位保留的小数位；B 恒为整数（半个字节没有意义）。
    Returns:
        形如 ``'1.5 KB'`` / ``'512 B'`` 的字符串。
    """
    value = float(max(0, num_bytes or 0))
    for unit in _SIZE_UNITS:
        if value < 1024 or unit == _SIZE_UNITS[-1]:
            if unit == "B":
                return f"{value:.0f} {unit}"
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    return f"{value:.{precision}f} {_SIZE_UNITS[-1]}"


def format_size_compare(before: int, after: int) -> tuple[str, str, str]:
    """把"压缩前 / 压缩后"两个体积格式化成三段展示文本。

    Args:
        before: 原始字节数。
        after: 处理后字节数。
    Returns:
        ``(前, 后, 变化百分比)``。百分比带符号：``'-42.3%'`` 表示变小，
        ``'+3.1%'`` 表示反而变大，``before <= 0`` 时返回 ``'0.0%'``。
    """
    before = max(0, int(before or 0))
    after = max(0, int(after or 0))
    if before <= 0:
        pct = "0.0%"
    else:
        delta = (after - before) / before * 100
        pct = f"{delta:+.1f}%"
    return human_size(before), human_size(after), pct


def format_duration(seconds: float) -> str:
    """把秒数格式化为耗时文本。

    Args:
        seconds: 秒数，负数按 0 处理。
    Returns:
        小于 60 秒返回 ``'12.5s'``；否则返回 ``'1:30'`` / ``'1:02:03'``。
    Notes:
        分钟以上不再显示小数——用户在意的是量级而不是毫秒。
    """
    total = max(0.0, float(seconds or 0.0))
    if total < 60:
        return f"{total:.1f}s"
    whole = int(total)
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_bitrate(text: str) -> tuple[float, str]:
    """解析码率字符串为 ``(数值, 单位)``。

    Args:
        text: 形如 ``'20M'`` / ``'320k'`` / ``'1500'``。
    Returns:
        ``(数值, 单位字符)``，单位为 ``''`` / ``'k'`` / ``'M'`` / ``'G'``，
        大小写按输入原样保留（ffmpeg 对 ``k`` 与 ``K`` 都认）。
        无法解析时返回 ``(0.0, '')``。
    Notes:
        旧实现是 ``int(bitrate.rstrip("Mk")) * 2`` 再拼 ``bitrate[-1]``，
        依赖"单位恰好是最后一个字符"这一隐含前提，遇到纯数字码率会把
        末位数字当单位切掉。这里显式做正则解析。
    """
    match = _BITRATE_RE.match(str(text or ""))
    if not match:
        return 0.0, ""
    return float(match.group(1)), match.group(2)


def scale_bitrate(text: str, factor: float) -> str:
    """按倍数缩放码率字符串，保留原单位。

    Args:
        text: 原码率字符串，如 ``'20M'``。
        factor: 缩放倍数，如 ``2.0``。
    Returns:
        缩放后的码率字符串，如 ``'40M'``。无法解析时原样返回输入。
    Notes:
        供 ffmpeg 的 ``-bufsize``（惯例取码率的 2 倍）使用。
    """
    value, unit = parse_bitrate(text)
    if value <= 0:
        return str(text or "")
    scaled = value * factor
    # 能整除就不带小数点，ffmpeg 对 "40M" 比 "40.0M" 更友好。
    number = f"{scaled:.0f}" if abs(scaled - round(scaled)) < 1e-9 else f"{scaled:g}"
    return f"{number}{unit}"
