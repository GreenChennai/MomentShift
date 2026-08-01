"""任务进度窗口（v0.7.9 快启3）。

右下角悬浮窗口，显示任务完成情况 + 系统占用。
折叠模式只显示统计数字，展开模式显示任务列表。
"""
from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, QRect
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea,
    QPushButton, QProgressBar, QApplication, QFrame,
)
from qfluentwidgets import FluentIcon as FIF, CaptionLabel, StrongBodyLabel

from ..i18n.translator import tr
from .theme import (
    ThemedCard, accent_color, muted_text, success_color, danger_color,
    border_color, surface, CARD_MARGIN,
)


# --------------------------------------------------------------------------
class _TaskRow(QWidget):
    """单个任务行：文件名 + 状态 + 进度条。"""

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self._name = name
        self._status = "pending"
        self._pct = 0

        hb = QHBoxLayout(self)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(8)

        self.nameLbl = CaptionLabel(Path(name).name)
        self.nameLbl.setStyleSheet("color: #1a1a1a; font-weight: 600;")
        hb.addWidget(self.nameLbl, 1)

        self.statusLbl = CaptionLabel(tr("progress.status.pending"))
        self.statusLbl.setStyleSheet(f"color: {muted_text()};")
        hb.addWidget(self.statusLbl)

        self.prog = QProgressBar()
        self.prog.setRange(0, 100)
        self.prog.setValue(0)
        self.prog.setFixedHeight(4)
        self.prog.setTextVisible(False)
        self.prog.setStyleSheet(
            f"QProgressBar{{border:none;background:{border_color()};"
            "border-radius:2px;}"
            f"QProgressBar::chunk{{background:{accent_color().name()};"
            "border-radius:2px;}}")

        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(4)
        vb.addLayout(hb)
        vb.addWidget(self.prog)

    def set_progress(self, pct: int):
        self._pct = pct
        self.prog.setValue(pct)

    def set_status(self, status: str, detail: str = ""):
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
        self.statusLbl.setStyleSheet(f"color: {c}; font-weight: 600;")
        if status == "done":
            self.prog.setValue(100)


# --------------------------------------------------------------------------
class TaskProgressWindow(QWidget):
    """右下角任务进度窗口。

    v0.7.9：显示快速调用任务的实时进度 + 系统占用。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._folded = False
        self._rows: dict[str, _TaskRow] = {}
        self._closing = False
        self._total = 0
        self._done = 0
        self._failed = 0
        self._start_time = time.monotonic()

        self.setWindowTitle(tr("progress.title"))
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet(f"background: {surface().name()};"
                           f" border: 1px solid {border_color()};"
                           " border-radius: 12px;")
        self.resize(340, 420)

        self._build_ui()
        self._position()
        self._update_stats()

    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # === 标题栏 ===
        bar = QWidget()
        bar.setFixedHeight(36)
        bar.setStyleSheet(f"background: {accent_color().name()};"
                          " border-radius: 12px 12px 0 0;")
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(12, 0, 8, 0)
        bh.setSpacing(6)

        title = StrongBodyLabel(tr("progress.title"))
        title.setStyleSheet("color: #fff; font-weight: 700;")
        bh.addWidget(title)
        bh.addStretch(1)

        self._fold_btn = QPushButton("▸")
        self._fold_btn.setFixedSize(24, 24)
        self._fold_btn.setStyleSheet(self._icon_btn_style())
        self._fold_btn.clicked.connect(self._toggle_fold)
        bh.addWidget(self._fold_btn)

        close = QPushButton("×")
        close.setFixedSize(24, 24)
        close.setStyleSheet(self._icon_btn_style())
        close.clicked.connect(self._on_close_clicked)
        bh.addWidget(close)
        root.addWidget(bar)

        # === 统计栏 ===
        stats = QWidget()
        stats.setStyleSheet("background: transparent;")
        sh = QHBoxLayout(stats)
        sh.setContentsMargins(12, 8, 12, 8)
        sh.setSpacing(10)

        self._totalLbl = CaptionLabel("")
        self._totalLbl.setStyleSheet("color: #1a1a1a; font-weight: 600;")
        sh.addWidget(self._totalLbl)

        self._doneLbl = CaptionLabel("")
        self._doneLbl.setStyleSheet(f"color: {success_color().name()}; font-weight: 600;")
        sh.addWidget(self._doneLbl)

        self._failLbl = CaptionLabel("")
        self._failLbl.setStyleSheet(f"color: {danger_color().name()}; font-weight: 600;")
        sh.addWidget(self._failLbl)

        self._pctLbl = CaptionLabel("")
        self._pctLbl.setStyleSheet(f"color: {accent_color().name()}; font-weight: 600;")
        sh.addWidget(self._pctLbl)

        sh.addStretch(1)
        root.addWidget(stats)

        # === 系统占用（折叠时也显示）===
        sys_row = QWidget()
        sys_row.setStyleSheet("background: transparent;")
        srh = QHBoxLayout(sys_row)
        srh.setContentsMargins(12, 0, 12, 8)
        srh.setSpacing(12)

        self._cpuLbl = CaptionLabel("")
        self._cpuLbl.setStyleSheet(f"color: {muted_text()}; font-size: 11px;")
        srh.addWidget(self._cpuLbl)

        self._gpuLbl = CaptionLabel("")
        self._gpuLbl.setStyleSheet(f"color: {muted_text()}; font-size: 11px;")
        srh.addWidget(self._gpuLbl)

        self._memLbl = CaptionLabel("")
        self._memLbl.setStyleSheet(f"color: {muted_text()}; font-size: 11px;")
        srh.addWidget(self._memLbl)
        srh.addStretch(1)
        root.addWidget(sys_row)

        # === 任务列表（可折叠）===
        self._list_area = QScrollArea()
        self._list_area.setWidgetResizable(True)
        self._list_area.setStyleSheet(
            "QScrollArea{border:none; background:transparent;}")
        self._list_widget = QWidget()
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(12, 4, 12, 12)
        self._list_layout.setSpacing(6)
        self._list_layout.addStretch(1)
        self._list_area.setWidget(self._list_widget)
        root.addWidget(self._list_area, 1)

        # 系统占用定时器
        self._sys_timer = QTimer(self)
        self._sys_timer.setInterval(1500)
        self._sys_timer.timeout.connect(self._refresh_sys_stats)
        self._sys_timer.start()

    # ------------------------------------------------------------------
    def _icon_btn_style(self):
        return ("QPushButton{background:transparent;color:#fff;"
                "border:none;font-weight:700;font-size:14px;}"
                "QPushButton:hover{background:rgba(255,255,255,0.2);"
                "border-radius:4px;}")

    def _position(self):
        """定位到屏幕右下角。"""
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.right() - self.width() - 20
        y = screen.bottom() - self.height() - 20
        self.move(x, y)

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
            self._auto_close()

    def _update_stats(self):
        self._totalLbl.setText(f"{tr('progress.total')} {self._total}")
        self._doneLbl.setText(f"{tr('progress.done')} {self._done}")
        self._failLbl.setText(f"{tr('progress.failed')} {self._failed}")
        pct = int((self._done + self._failed) / self._total * 100) if self._total else 0
        self._pctLbl.setText(f"{pct}%")

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
            return f"{m.used / 1e9:.1f}/{m.total / 1e9:.1f} GB"
        except ImportError:
            return "--"

    # ------------------------------------------------------------------
    def _toggle_fold(self):
        self._folded = not self._folded
        self._list_area.setVisible(not self._folded)
        self._fold_btn.setText("▾" if self._folded else "▸")
        if self._folded:
            self.setFixedHeight(120)
        else:
            self.setFixedHeight(420)

    def _on_close_clicked(self):
        self._closing = True
        self._fade_out()

    def _auto_close(self):
        """任务全部完成，延迟自动关闭（带淡出动画）。"""
        QTimer.singleShot(2000, self._fade_out)

    def _fade_out(self):
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(300)
        anim.setStartValue(self.geometry())
        end = self.geometry()
        end.setWidth(0)
        anim.setEndValue(end)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(self.close)
        anim.start()
        self._fade_anim = anim  # 防止被 GC
