"""视频帧提取对话框（简化版）。

职责边界：
- 做：为 --quick extract_frame 右键菜单入口提供轻量弹窗，支持提取首帧/尾帧/指定秒数帧。
- 不做：不实现 ffmpeg 帧提取逻辑（在 quick_runner 中处理）。

依赖：core/logger、gui/theme、i18n/translator；被依赖：quick_runner。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)
from qfluentwidgets import (
    ComboBox,
    PrimaryPushButton,
)

from ..core.logger import get_logger
from ..i18n.translator import tr
from . import tokens
from .theme import (
    apply_text,
    muted_text,
    surface,
)

log = get_logger("extract_frame_dialog")


def _get_video_duration(ffmpeg_path: str, video_path: str) -> float | None:
    """使用 ffprobe 获取视频时长（秒）。"""
    try:
        ffprobe_path = Path(ffmpeg_path).parent / "ffprobe.exe"
        if not ffprobe_path.exists():
            ffprobe_path = Path(ffmpeg_path).parent / "ffprobe"
        cmd = [
            str(ffprobe_path),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ]
        # 隐藏CMD窗口
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=creation_flags,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


class ExtractFrameDialog(QDialog):
    """视频帧提取弹窗：单个视频，立即提取。"""

    _DIALOG_W = 400
    _DIALOG_H = 250

    def __init__(self, video_path: str, on_extract):
        super().__init__(None)
        self._video_path = video_path
        self._on_extract = on_extract
        self._duration = 0.0

        self.setWindowTitle(tr("quick.extract_frame.title"))
        self.setFixedSize(self._DIALOG_W, self._DIALOG_H)
        self.setObjectName("extractFrameDlg")
        self.setStyleSheet(f"#extractFrameDlg {{ background-color: {surface().name()}; }}")

        self._build_ui()
        self._load_video_info()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        # 文件名显示
        self.name_label = QLabel(Path(self._video_path).name)
        apply_text(self.name_label, tokens.TEXT_STRONG, size=13, weight=600)
        self.name_label.setWordWrap(True)
        root.addWidget(self.name_label)

        # 视频时长显示
        self.duration_label = QLabel(tr("quick.extract_frame.loading"))
        apply_text(self.duration_label, muted_text(), size=11)
        root.addWidget(self.duration_label)

        root.addSpacing(4)

        # 操作按钮行：[提取首帧] [秒数输入] [提取尾帧]
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_first = PrimaryPushButton(tr("quick.extract_frame.first"))
        self.btn_first.setFixedHeight(32)
        self.btn_first.clicked.connect(lambda: self._extract("first"))
        btn_row.addWidget(self.btn_first)

        self.second_input = QLineEdit()
        self.second_input.setFixedHeight(32)
        self.second_input.setMinimumWidth(60)
        self.second_input.setMaximumWidth(80)
        self.second_input.setPlaceholderText("0")
        self.second_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btn_row.addWidget(self.second_input)

        self.btn_custom = PrimaryPushButton(tr("quick.extract_frame.extract"))
        self.btn_custom.setFixedHeight(32)
        self.btn_custom.clicked.connect(lambda: self._extract("custom"))
        btn_row.addWidget(self.btn_custom)

        self.btn_last = PrimaryPushButton(tr("quick.extract_frame.last"))
        self.btn_last.setFixedHeight(32)
        self.btn_last.clicked.connect(lambda: self._extract("last"))
        btn_row.addWidget(self.btn_last)

        root.addLayout(btn_row)

        root.addSpacing(4)

        # 图片格式下拉
        fmt_row = QHBoxLayout()
        fmt_row.setSpacing(8)

        fmt_label = QLabel(tr("quick.extract_frame.format"))
        apply_text(fmt_label, muted_text(), size=11)
        fmt_row.addWidget(fmt_label)

        self.format_combo = ComboBox()
        self.format_combo.addItems(["PNG", "JPG", "BMP", "WebP"])
        self.format_combo.setFixedHeight(32)
        fmt_row.addWidget(self.format_combo)

        fmt_row.addStretch(1)
        root.addLayout(fmt_row)

        root.addStretch(1)

    def _load_video_info(self):
        """加载视频信息（时长）。"""
        from ..core.ffmpeg import find_ffmpeg

        ffmpeg_path = find_ffmpeg()
        if ffmpeg_path:
            duration = _get_video_duration(ffmpeg_path, self._video_path)
            if duration is not None:
                self._duration = duration
                mins = int(duration // 60)
                secs = duration % 60
                self.duration_label.setText(
                    tr("quick.extract_frame.duration", min=mins, sec=f"{secs:.1f}")
                )
            else:
                self.duration_label.setText(tr("quick.extract_frame.duration_unknown"))
        else:
            self.duration_label.setText(tr("quick.extract_frame.no_ffmpeg"))

    def _extract(self, mode: str):
        """执行帧提取。"""
        second = 0
        if mode == "custom":
            try:
                second = int(self.second_input.text() or "0")
            except ValueError:
                second = 0
            # 限制范围
            max_sec = max(0, int(self._duration) - 1) if self._duration > 0 else 0
            second = max(0, min(second, max_sec))
        elif mode == "last":
            second = max(0, int(self._duration) - 1) if self._duration > 0 else 0

        fmt = self.format_combo.currentText().lower()
        self._on_extract(self._video_path, mode, second, fmt)
        self.accept()

    def set_duration(self, duration: float):
        """设置视频时长。"""
        self._duration = duration
        mins = int(duration // 60)
        secs = duration % 60
        self.duration_label.setText(
            tr("quick.extract_frame.duration", min=mins, sec=f"{secs:.1f}")
        )
