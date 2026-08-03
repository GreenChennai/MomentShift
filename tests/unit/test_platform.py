"""core.platform 的纯 Python 单测：静默标志、路径解析、子进程封装。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from momentshift.core import platform as plat


class TestWinSilent:
    """Windows 子进程静默标志。"""

    def test_matches_subprocess_constant(self) -> None:
        assert plat.WIN_SILENT == getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def test_zero_on_non_windows(self) -> None:
        """非 Windows 平台必须是 0，否则 creationflags 会被拒绝。"""
        if sys.platform != "win32":
            assert plat.WIN_SILENT == 0


class TestDirectories:
    """目录解析。全部以 app_base_dir 为锚点，只需验证相对关系。"""

    def test_app_base_dir_is_absolute_existing_dir(self) -> None:
        base = plat.app_base_dir()
        assert base.is_absolute()
        assert base.is_dir()

    def test_dev_base_dir_contains_src_package(self) -> None:
        """开发态锚点必须是仓库根，否则 config.json 会落到错误位置。"""
        if not getattr(sys, "frozen", False):
            assert (plat.app_base_dir() / "src" / "momentshift").is_dir()

    def test_config_file_and_log_dir_hang_off_base(self) -> None:
        base = plat.app_base_dir()
        assert plat.config_file() == base / "config.json"
        assert plat.log_dir() == base / "logs"

    def test_tools_dir_is_created(self) -> None:
        directory = plat.tools_dir()
        assert directory == plat.app_base_dir() / "tools"
        assert directory.is_dir()

    def test_resources_dir_holds_bundled_assets(self) -> None:
        res = plat.resources_dir()
        assert res.name == "resources"
        # 开发态资源目录一定存在（字体随仓库分发）。
        if not getattr(sys, "frozen", False):
            assert res.is_dir()


class TestSilentDefaults:
    """``_with_silent_defaults`` 的合并语义。"""

    def test_injects_win_silent(self) -> None:
        assert plat._with_silent_defaults({})["creationflags"] == plat.WIN_SILENT

    def test_ors_instead_of_overwriting(self) -> None:
        """调用方自带的标志（如 DETACHED_PROCESS）不能被覆盖掉。"""
        extra = 0x00000008
        merged = plat._with_silent_defaults({"creationflags": extra})
        assert merged["creationflags"] == extra | plat.WIN_SILENT

    def test_text_mode_gets_utf8_defaults(self) -> None:
        merged = plat._with_silent_defaults({"text": True})
        assert merged["encoding"] == "utf-8"
        assert merged["errors"] == "replace"

    def test_binary_mode_stays_binary(self) -> None:
        """二进制模式不能注入 encoding，否则 stdout 会变成 str。"""
        merged = plat._with_silent_defaults({"stdout": subprocess.PIPE})
        assert "encoding" not in merged

    def test_caller_encoding_wins(self) -> None:
        merged = plat._with_silent_defaults({"text": True, "encoding": "gbk"})
        assert merged["encoding"] == "gbk"

    def test_does_not_mutate_caller_kwargs(self) -> None:
        original: dict = {}
        plat._with_silent_defaults(original)
        assert original == {}


class TestRunSilent:
    """``run_silent`` 的实际执行行为（用当前解释器当被测子进程）。"""

    def test_captures_stdout_as_text(self) -> None:
        proc = plat.run_silent(
            [sys.executable, "-c", "print('hello')"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0
        assert proc.stdout.strip() == "hello"

    def test_non_utf8_output_does_not_raise(self) -> None:
        """errors='replace' 兜底：引擎输出 GBK 报错信息时不能炸解码。"""
        proc = plat.run_silent(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfe bad')"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0
        assert "bad" in proc.stdout

    def test_accepts_path_objects(self) -> None:
        """命令序列里混入 Path 不应报错（引擎路径常以 Path 传入）。"""
        proc = plat.run_silent(
            [Path(sys.executable), "-c", "print(1)"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.stdout.strip() == "1"

    def test_missing_executable_raises_oserror(self) -> None:
        """找不到可执行文件时向上抛，由调用方决定提示文案。"""
        with pytest.raises(OSError):
            plat.run_silent(["__momentshift_no_such_binary__"], capture_output=True)


class TestPopenSilent:
    """``popen_silent`` 的流式读取行为。"""

    def test_streams_lines(self) -> None:
        with plat.popen_silent(
            [sys.executable, "-c", "print('a'); print('b')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as proc:
            lines = [line.strip() for line in proc.stdout]
        assert lines == ["a", "b"]
