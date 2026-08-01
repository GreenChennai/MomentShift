"""任务进度窗口（v0.7.10 重设计）。

右下角悬浮卡片式窗口：统计栏 + 系统占用 + 任务列表（可折叠）。
匹配 MomentShift 整体卡片风格，支持展开/折叠动画、自动关闭。
"""
from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, QSize
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea,
    QPushButton, QProgressBar, QApplication, QFrame, QSizePolicy,
)
from qfluentwidgets import FluentIcon as FIF, CaptionLabel, StrongBodyLabel

from ..i18n.translator import tr
from .theme import (
    ThemedCard, accent_color, muted_text, success_color, danger_color,
    border_color, surface, CARD_MARGIN, text_strong,
)


# --------------------------------------------------------------------------
class _TaskRow(QWidget):
    """单任务行。"""

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self._name = name
        self._status = "pending"
        self.setStyleSheet("background: transparent;")

        hb = QHBoxLayout(self)
        hb.setContentsMargins(0, 2, 0, 2)
        hb.setSpacing(6)

        self.nameLbl = CaptionLabel(Path(name).name)
        self.nameLbl.setStyleSheet(f"color: {text_strong()}; font-size: 11px;")
        hb.addWidget(self.nameLbl, 1)

        self.statusLbl = CaptionLabel("")
        self.statusLbl.setStyleSheet(f"color: {muted_text()}; font-size: 10px;")
        hb.addWidget(self.statusLbl)

        self.pctLbl = CaptionLabel("")
        self.pctLbl.setStyleSheet(f"color: {muted_text()}; font-size: 10px; min-width: 28px;")
        hb.addWidget(self.pctLbl)

    def set_progress(self, pct: int):
        self.pctLbl.setText(f"{pct}%")

    def set_status(self, status: str):
        self._status = status
        colors = {
            "running": accent_color().name(),
            "done": success_color().name(),
            "failed": danger_color().name(),
            "pending": muted_text(),
        }
        c = colors.get(status, muted_text())
        label = tr(f"progress.status.{status}")
        self.statusLbl.setText(label)
        self.statusLbl.setStyleSheet(f"color: {c}; font-size: 10px; font-weight: 600;")
        if status == "done":
            self.pctLbl.setText("100%")
            self.pctLbl.setStyleSheet(
                f"color: {success_color().name()}; font-size: 10px; font-weight: 600;")


# --------------------------------------------------------------------------
class _StatBox(QLabel):
    """统计数字盒子（带标签 + 数值）。"""

    def __init__(self, label: str, parent=None):
        super().__init__(label, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            "color: #1a1a1a; font-size: 10px; background: transparent;"
            " padding: 0 4px;")


class TaskProgressWindow(QWidget):
    """右下角任务进度卡片窗口。

    v0.7.10 重设计：卡片式风格，圆角阴影感，渐入渐出动画。
    """

    _CARD_BG = "#FAFAFA"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._folded = False
        self._rows: dict[str, _TaskRow] = {}
        self._closing = False
        self._total = 0
        self._done = 0
        self._failed = 0
        self._start_time = time.monotonic()
        self._normal_h = 420
        self._folded_h = 130

        self.setWindowTitle(tr("progress.title"))
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet(
            f"TaskProgressWindow{{"
            f" background: {self._CARD_BG};"
            f" border: 1px solid {border_color()};"
            " border-radius: 14px;"
            "}")
        self.resize(330, self._normal_h)

        self._build_ui()
        self._position()
        self._update_stats()
        self._animate_in()

    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # === 标题栏（品牌绿顶）===
        bar = QWidget()
        bar.setFixedHeight(38)
        bar.setStyleSheet(
            f"background: {accent_color().name()};"
            " border-radius: 13px 13px 0 0;")
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(14, 0, 8, 0)
        bh.setSpacing(6)

        # 小绿点
        dot = QLabel("●")
        dot.setStyleSheet("color: #fff; font-size: 16px; background: transparent;")
        bh.addWidget(dot)

        title = StrongBodyLabel(tr("progress.title"))
        title.setStyleSheet("color: #fff; font-weight: 700; font-size: 13px;")
        bh.addWidget(title)
        bh.addStretch(1)

        self._fold_btn = QPushButton("▾")
        self._fold_btn.setFixedSize(28, 28)
        self._fold_btn.setStyleSheet(self._bar_btn_style())
        self._fold_btn.clicked.connect(self._toggle_fold)
        bh.addWidget(self._fold_btn)

        close = QPushButton("×")
        close.setFixedSize(28, 28)
        close.setStyleSheet(self._bar_btn_style())
        close.clicked.connect(self._on_close_clicked)
        bh.addWidget(close)
        root.addWidget(bar)

        # === 统计栏 ===
        stat_row = QHBoxLayout()
        stat_row.setContentsMargins(12, 8, 12, 4)
        stat_row.setSpacing(12)

        self._totalLbl = _StatBox("")
        stat_row.addWidget(self._totalLbl)
        self._doneLbl = _StatBox("")
        stat_row.addWidget(self._doneLbl)
        self._failLbl = _StatBox("")
        stat_row.addWidget(self._failLbl)
        self._pctLbl = _StatBox("")
        stat_row.addWidget(self._pctLbl)
        stat_row.addStretch(1)
        root.addLayout(stat_row)

        # === 全局进度条 ===
        self._global_prog = QProgressBar()
        self._global_prog.setRange(0, 100)
        self._global_prog.setValue(0)
        self._global_prog.setFixedHeight(4)
        self._global_prog.setTextVisible(False)
        self._global_prog.setStyleSheet(
            "QProgressBar{border:none; background: #e8e8e8;"
            " border-radius: 2px; margin: 0 12px;}"
            f"QProgressBar::chunk{{background: {accent_color().name()};"
            " border-radius: 2px;}")
        root.addWidget(self._global_prog)

        # === 系统占用 ===
        sys_row = QHBoxLayout()
        sys_row.setContentsMargins(12, 6, 12, 4)
        sys_row.setSpacing(14)

        self._cpuLbl = CaptionLabel("")
        self._cpuLbl.setStyleSheet(f"color: {muted_text()}; font-size: 10px;")
        sys_row.addWidget(self._cpuLbl)

        self._gpuLbl = CaptionLabel("")
        self._gpuLbl.setStyleSheet(f"color: {muted_text()}; font-size: 10px;")
        sys_row.addWidget(self._gpuLbl)

        self._memLbl = CaptionLabel("")
        self._memLbl.setStyleSheet(f"color: {muted_text()}; font-size: 10px;")
        sys_row.addWidget(self._memLbl)
        sys_row.addStretch(1)
        root.addLayout(sys_row)

        # === 任务列表 ===
        self._list_area = QScrollArea()
        self._list_area.setWidgetResizable(True)
        self._list_area.setStyleSheet(
            "QScrollArea{border:none; background:transparent;}")
        self._list_widget = QWidget()
        self._list_widget.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(12, 0, 12, 10)
        self._list_layout.setSpacing(4)
        self._list_layout.addStretch(1)
        self._list_area.setWidget(self._list_widget)
        root.addWidget(self._list_area, 1)

        # 系统占用定时器
        self._sys_timer = QTimer(self)
        self._sys_timer.setInterval(2000)
        self._sys_timer.timeout.connect(self._refresh_sys_stats)
        self._sys_timer.start()

    # ------------------------------------------------------------------
    def _bar_btn_style(self):
        return (
            "QPushButton{background:transparent;color:#fff;"
            "border:none;font-size: 16px;}"
            "QPushButton:hover{background:rgba(255,255,255,0.18);"
            "border-radius: 5px;}")

    def _position(self):
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.right() - self.width() - 16
        y = screen.bottom() - self._normal_h - 16
        self.move(x, y)

    # ------------------------------------------------------------------
    def _animate_in(self):
        """入场动画：从透明渐显 + 从右滑入。"""
        self.setWindowOpacity(0)
        pos = self.pos()
        self.move(pos.x() + 40, pos.y())

        opacity_anim = QPropertyAnimation(self, b"windowOpacity", self)
        opacity_anim.setDuration(300)
        opacity_anim.setStartValue(0)
        opacity_anim.setEndValue(1.0)
        opacity_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        slide_anim = QPropertyAnimation(self, b"pos", self)
        slide_anim.setDuration(300)
        slide_anim.setStartValue(self.pos())
        slide_anim.setEndValue(pos)
        slide_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        opacity_anim.start()
        slide_anim.start()
        # keep refs
        self._in_opacity = opacity_anim
        self._in_slide = slide_anim

    # ------------------------------------------------------------------
    def add_task(self, item_id: str, name: str):
        row = _TaskRow(name)
        self._rows[item_id] = row
        self._list_layout.insertWidget(self._list_layout.count() - 1, row)
        self._total += 1
        self._update_stats()

    def update_progress(self, item_id: str, pct: int):
        row = self._rows.get(item_id)
        if row:
            row.set_progress(pct)
        self._update_stats()

    def update_status(self, item_id: str, status: str):
        row = self._rows.get(item_id)
        if row:
            row.set_status(status)
        if status == "done":
            self._done += 1
        elif status == "failed":
            self._failed += 1
        self._update_stats()
        if self._done + self._failed >= self._total > 0:
            QTimer.singleShot(2500, self._fade_out)

    # ------------------------------------------------------------------
    def _update_stats(self):
        self._totalLbl.setText(f"{tr('progress.total')} {self._total}")
        self._doneLbl.setText(f"{tr('progress.done')} {self._done}")
        self._failLbl.setText(f"{tr('progress.failed')} {self._failed}")
        pct = int((self._done + self._failed) / self._total * 100) if self._total else 0
        self._pctLbl.setText(f"{pct}%")
        self._global_prog.setValue(pct)

    def _refresh_sys_stats(self):
        cpu = self._get_cpu_usage()
        gpu = self._get_gpu_usage()
        mem = self._get_mem_usage()
        self._cpuLbl.setText(f"CPU {cpu}")
        self._gpuLbl.setText(f"GPU {gpu}")
        self._memLbl.setText(f"MEM {mem}")

    def _get_cpu_usage(self) -> str:
        try:
            import psutil
            return f"{psutil.cpu_percent(interval=None):.0f}%"
        except ImportError:
            return "--"

    def _get_gpu_usage(self) -> str:
        try:
            import pynvml
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            u = pynvml.nvmlDeviceGetUtilizationRates(h)
            return f"{u.gpu}%"
        except Exception:
            return "--"

    def _get_mem_usage(self) -> str:
        try:
            import psutil
            m = psutil.virtual_memory()
            return f"{m.used / 1e9:.1f}/{m.total / 1e9:.1f}GB"
        except ImportError:
            return "--"

    # ------------------------------------------------------------------
    def _toggle_fold(self):
        self._folded = not self._folded
        self._list_area.setVisible(not self._folded)
        self._fold_btn.setText("▸" if self._folded else "▾")
        target_h = self._folded_h if self._folded else self._normal_h

        anim = QPropertyAnimation(self, b"size", self)
        anim.setDuration(250)
        anim.setStartValue(self.size())
        anim.setEndValue(QSize(self.width(), target_h))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.start()
        self._fold_anim = anim

    def _on_close_clicked(self):
        self._fade_out()

    def _fade_out(self):
        """退场动画：缩小 + 淡出。"""
        if self._closing:
            return
        self._closing = True

        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(350)
        anim.setStartValue(self.geometry())
        end = self.geometry()
        end.setWidth(0)
        anim.setEndValue(end)
        anim.setEasingCurve(QEasingCurve.Type.InCubic)
        anim.finished.connect(self.close)
        anim.start()
        self._fade_anim = anim
