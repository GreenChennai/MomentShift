"""core.formatting 的纯 Python 单测（不依赖 Qt，可在 CI 无显示环境跑）。"""

from __future__ import annotations

import pytest

from momentshift.core.formatting import (
    format_duration,
    format_size_compare,
    human_size,
    parse_bitrate,
    scale_bitrate,
)


class TestHumanSize:
    """体积格式化。"""

    @pytest.mark.parametrize(
        ("num_bytes", "expected"),
        [
            (0, "0 B"),
            (1, "1 B"),
            (1023, "1023 B"),
            (1024, "1.0 KB"),
            (1536, "1.5 KB"),
            (1024 * 1024, "1.0 MB"),
            (1024**3, "1.0 GB"),
        ],
    )
    def test_ladder(self, num_bytes: int, expected: str) -> None:
        assert human_size(num_bytes) == expected

    def test_negative_and_none_clamp_to_zero(self) -> None:
        """负数与 None 都按 0 处理，避免界面上出现 '-1 B'。"""
        assert human_size(-100) == "0 B"
        assert human_size(None) == "0 B"

    def test_above_gb_does_not_advance_unit(self) -> None:
        """GB 之后不再进位，5120 GB 就显示成 5120.0 GB。"""
        assert human_size(5120 * 1024**3) == "5120.0 GB"

    def test_precision_is_configurable(self) -> None:
        assert human_size(1536, precision=2) == "1.50 KB"
        assert human_size(1536, precision=0) == "2 KB"


class TestFormatSizeCompare:
    """压缩前后对比。"""

    def test_shrink_reports_negative_percent(self) -> None:
        before, after, pct = format_size_compare(2048, 1024)
        assert (before, after, pct) == ("2.0 KB", "1.0 KB", "-50.0%")

    def test_grow_reports_positive_percent(self) -> None:
        _, _, pct = format_size_compare(1000, 1100)
        assert pct == "+10.0%"

    def test_zero_before_does_not_divide_by_zero(self) -> None:
        """源文件大小为 0（读取失败）时不能抛 ZeroDivisionError。"""
        assert format_size_compare(0, 500) == ("0 B", "500 B", "0.0%")


class TestFormatDuration:
    """耗时格式化。"""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0.0s"),
            (12.54, "12.5s"),
            (59.9, "59.9s"),
            (60, "1:00"),
            (90, "1:30"),
            (3723, "1:02:03"),
        ],
    )
    def test_values(self, seconds: float, expected: str) -> None:
        assert format_duration(seconds) == expected

    def test_negative_clamps_to_zero(self) -> None:
        assert format_duration(-5) == "0.0s"


class TestParseBitrate:
    """码率解析 —— 覆盖旧实现 ``int(text.rstrip('Mk')) * 2`` 的踩坑点。"""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("20M", (20.0, "M")),
            ("320k", (320.0, "k")),
            ("128K", (128.0, "K")),
            ("2.5M", (2.5, "M")),
            (" 10 M ", (10.0, "M")),
        ],
    )
    def test_with_unit(self, text: str, expected: tuple[float, str]) -> None:
        assert parse_bitrate(text) == expected

    def test_plain_number_keeps_all_digits(self) -> None:
        """回归：旧实现会把 '1500' 的末位 0 当单位切掉，得到 150。"""
        assert parse_bitrate("1500") == (1500.0, "")

    @pytest.mark.parametrize("text", ["", None, "original", "abc", "M20"])
    def test_unparsable_returns_zero(self, text: object) -> None:
        assert parse_bitrate(text) == (0.0, "")


class TestScaleBitrate:
    """码率缩放（ffmpeg -bufsize 惯例取码率 2 倍）。"""

    def test_keeps_unit_and_drops_trailing_zero(self) -> None:
        assert scale_bitrate("20M", 2.0) == "40M"
        assert scale_bitrate("320k", 2.0) == "640k"

    def test_plain_number_stays_plain(self) -> None:
        assert scale_bitrate("1500", 2.0) == "3000"

    def test_fractional_result_keeps_decimals(self) -> None:
        assert scale_bitrate("5M", 1.5) == "7.5M"

    def test_unparsable_returns_input_unchanged(self) -> None:
        """'original' 是预设里的合法取值，必须原样透传给上层判断。"""
        assert scale_bitrate("original", 2.0) == "original"
        assert scale_bitrate("", 2.0) == ""
