"""Batch queue list: one card per task with live progress and actions."""

import math
from pathlib import Path

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QPainter
from ..core.qt_compat import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, Signal, QApplication
)
from qfluentwidgets import (
    CardWidget,
    FluentIcon as FIF,
    ComboBox,
    TransparentToolButton,
    StrongBodyLabel,
    CaptionLabel,
    InfoBar,
    InfoBarPosition,
    isDarkTheme,
    Theme,
)
from .theme import ThemedCard
from ..i18n.translator import tr
from ..core.presets import TARGET_GROUPS
from ..core.models import Task

CATEGORY_ICON = {"image": FIF.PHOTO, "audio": FIF.MUSIC, "video": FIF.VIDEO}


def human_size(n: int) -> str:
    if not n or n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = min(len(units) - 1, int(math.log(n, 1024)))
    return f"{n / 1024 ** i:.1f} {units[i]}"


def format_size_compare(before: int, after: int) -> str:
    b = human_size(before)
    a = human_size(after)
    if before and after and before != after:
        pct = (after - before) / before * 100
        sign = "+" if pct > 0 else ""
        return tr("convert.result.size", before=b, after=a, pct=f"{sign}{pct:.0f}%")
    return f"{b} → {a}"


class StatusPill(QLabel):
    """A compact, color-coded status chip.

    Replaces the old column header: each task card is now self-describing, so a
    status pill (plus the visible filename / format / progress) makes the queue
    readable without a separate, hard-to-align table header on a 400px window.
    """

    _COLORS = {
        "pending":  ("rgba(128,128,128,32)", "rgba(120,120,120,1)"),
        "running":  ("rgba(32,128,240,46)", "rgba(28,110,210,1)"),
        "done":     ("rgba(80,180,100,46)", "rgba(46,140,70,1)"),
        "failed":   ("rgba(220,80,80,50)",  "rgba(200,60,60,1)"),
        "canceled": ("rgba(128,128,128,32)", "rgba(120,120,120,1)"),
    }

    def __init__(self, status: str = "pending", parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_status(status)

    def set_status(self, status: str):
        bg, fg = self._COLORS.get(status, self._COLORS["pending"])
        self.setText(tr(f"convert.status.{status}"))
        self.setStyleSheet(
            f"padding: 0 9px; border-radius: 10px; font-size: 11px;"
            f" background-color: {bg}; color: {fg};"
        )


class ProgressBar(QWidget):
    """A full-width progress background bar.

    The background is white/light-grey; the filled portion is light green when
    progressing and the entire bar turns red for failed tasks.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0
        self._error = False
        self.setMinimumHeight(8)
        self.setMaximumHeight(8)

    def set_value(self, value: int):
        self._value = max(0, min(100, value))
        self.update()

    def set_error(self, error: bool):
        self._error = error
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())

        if self._error:
            bg = QColor(220, 80, 80)
            fill = bg
        else:
            # Background: subtle light colour.
            bg = QColor(220, 220, 220)
            fill = QColor(120, 200, 120)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, 4, 4)

        if not self._error and self._value > 0:
            w = r.width() * self._value / 100.0
            fill_rect = QRectF(r.x(), r.y(), w, r.height())
            p.setBrush(fill)
            p.drawRoundedRect(fill_rect, 4, 4)


class QueueItemWidget(ThemedCard):
    """A single task row inside the queue (portrait-friendly layout)."""

    removeRequested = Signal(str)
    retryRequested = Signal(str)
    formatChanged = Signal(str, str)

    def __init__(self, task: Task, parent=None):
        super().__init__(parent)
        self.task = task
        self.setMinimumHeight(88)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(6)

        # Row 1: icon | name | status | actions
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.setContentsMargins(0, 0, 0, 0)

        self.iconLabel = QLabel()
        self.iconLabel.setPixmap(
            CATEGORY_ICON.get(task.category, FIF.DOCUMENT)
            .icon(Theme.DARK if isDarkTheme() else Theme.AUTO)
            .pixmap(22, 22)
        )
        self.iconLabel.setFixedSize(24, 24)
        self.iconLabel.setStyleSheet("background-color: transparent;")

        self.nameLabel = StrongBodyLabel(Path(task.input_path).name)
        self.nameLabel.setToolTip(str(task.input_path))

        self.statusLabel = StatusPill(task.status)

        self.retryBtn = TransparentToolButton(FIF.SYNC, self)
        self.retryBtn.setToolTip(tr("convert.action.retry"))
        self.retryBtn.setFixedSize(28, 28)
        self.retryBtn.setVisible(task.status == Task.FAILED)
        self.retryBtn.clicked.connect(lambda: self.retryRequested.emit(task.id))

        self.copyBtn = TransparentToolButton(FIF.COPY, self)
        self.copyBtn.setToolTip(tr("convert.result.copy"))
        self.copyBtn.setFixedSize(28, 28)
        self.copyBtn.setVisible(task.status == Task.DONE)
        self.copyBtn.clicked.connect(self._copy)

        self.removeBtn = TransparentToolButton(FIF.DELETE, self)
        self.removeBtn.setToolTip(tr("convert.action.remove"))
        self.removeBtn.setFixedSize(28, 28)
        self.removeBtn.clicked.connect(lambda: self.removeRequested.emit(task.id))

        row1.addWidget(self.iconLabel)
        row1.addWidget(self.nameLabel, 1)
        row1.addWidget(self.statusLabel)
        row1.addWidget(self.retryBtn)
        row1.addWidget(self.copyBtn)
        row1.addWidget(self.removeBtn)

        # Progress bar (full-width background)
        self.progress = ProgressBar()
        self.progress.set_value(task.progress)
        self.progress.set_error(task.status == Task.FAILED)

        # Row 2: format combo | details (size/quality/time)
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.setContentsMargins(0, 0, 0, 0)

        self.formatCombo = ComboBox()
        for fmt in TARGET_GROUPS.get(task.category, []):
            self.formatCombo.addItem(fmt.upper(), userData=fmt)
        self.formatCombo.setFixedWidth(70)
        self._set_combo(task.target_format)
        self.formatCombo.currentIndexChanged.connect(self._on_format)

        self.detailLabel = CaptionLabel("")
        self.detailLabel.setObjectName("queueSub")
        self.detailLabel.setWordWrap(False)

        row2.addWidget(self.formatCombo)
        row2.addWidget(self.detailLabel, 1)

        # Merge tasks combine several inputs into one output.
        if task.merge:
            n = len(task.input_paths or [])
            self.nameLabel.setText(tr("convert.queue.merge_name", n=n))
            self.formatCombo.setVisible(False)

        outer.addLayout(row1)
        outer.addWidget(self.progress)
        outer.addLayout(row2)

        self._update_details()

    def _set_combo(self, fmt: str):
        for i in range(self.formatCombo.count()):
            self.formatCombo.setCurrentIndex(i)
            if self.formatCombo.currentData() == fmt:
                break

    def _update_details(self):
        """Refresh the detail line (size compare + quality/bitrate)."""
        parts = []
        if self.task.status == Task.DONE and self.task.src_size and self.task.dst_size:
            parts.append(format_size_compare(self.task.src_size, self.task.dst_size))
        # Quality / bitrate hint from advanced options.
        adv = self.task.adv or {}
        if self.task.category == "image" and adv.get("quality"):
            parts.append(tr("convert.queue.quality", q=adv["quality"]))
        elif self.task.category in ("video", "audio") and adv.get("vbitrate"):
            parts.append(tr("convert.queue.bitrate", b=adv["vbitrate"]))
        self.detailLabel.setText("  ·  ".join(parts))

    # -- updates from manager --------------------------------------------
    def set_progress(self, pct: int):
        self.task.progress = pct
        self.progress.set_value(pct)
        if self.task.status == Task.RUNNING:
            self._update_details()

    def set_status(self, status: str, error: str = ""):
        self.task.status = status
        self.statusLabel.set_status(status)
        self.retryBtn.setVisible(status == Task.FAILED)
        self.copyBtn.setVisible(status == Task.DONE)
        self.progress.set_error(status == Task.FAILED)
        if status == Task.DONE:
            self._update_details()
            self.setToolTip(self.task.output_path or "")
        else:
            self.setToolTip(error.strip().splitlines()[-1] if error.strip() else "")

    def set_format(self, fmt: str):
        self.task.target_format = fmt
        self._set_combo(fmt)

    def _on_format(self, _index):
        fmt = self.formatCombo.currentData()
        if fmt:
            self.formatChanged.emit(self.task.id, fmt)

    def _copy(self):
        QApplication.clipboard().setText(self.task.output_path)
        InfoBar.success(
            tr("convert.result.copied"), "", parent=self.window(),
            duration=2000, position=InfoBarPosition.TOP_RIGHT,
        )

    def retranslate(self):
        self.statusLabel.set_status(self.task.status)
        self.retryBtn.setToolTip(tr("convert.action.retry"))
        self.removeBtn.setToolTip(tr("convert.action.remove"))
        self.copyBtn.setToolTip(tr("convert.result.copy"))
        self._update_details()


class QueueListWidget(QWidget):
    """Holds the queue items, header and stats, and keeps them in sync."""

    removeRequested = Signal(str)
    retryRequested = Signal(str)
    formatChanged = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: transparent; border: none;")
        self.items: dict[str, QueueItemWidget] = {}
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(10)

        # ---- stats bar ----
        stats = QHBoxLayout()
        stats.setSpacing(8)
        stats.setContentsMargins(4, 0, 4, 0)
        self.statTotal = QLabel(tr("convert.queue.stats.total", n=0))
        self.statRunning = QLabel(tr("convert.queue.stats.running", n=0))
        self.statError = QLabel(tr("convert.queue.stats.error", n=0))
        for label in (self.statTotal, self.statRunning, self.statError):
            label.setObjectName("queueSub")
            stats.addWidget(label)
        stats.addStretch(1)
        self.layout.addLayout(stats)

        # ---- list ----
        self.listLayout = QVBoxLayout()
        self.listLayout.setContentsMargins(0, 0, 0, 0)
        self.listLayout.setSpacing(8)
        self.layout.addLayout(self.listLayout)

        self.emptyLabel = QLabel(tr("convert.queue.empty"))
        self.emptyLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyLabel.setObjectName("queueEmpty")
        self.listLayout.addWidget(self.emptyLabel)
        self._update_empty()

    def _update_empty(self):
        self.emptyLabel.setVisible(len(self.items) == 0)

    def _update_stats(self, counts: dict):
        self.statTotal.setText(tr("convert.queue.stats.total", n=counts.get("total", 0)))
        self.statRunning.setText(tr("convert.queue.stats.running", n=counts.get("running", 0)))
        self.statError.setText(tr("convert.queue.stats.error", n=counts.get("failed", 0)))

    def add_item(self, task: Task):
        if task.id in self.items:
            return
        w = QueueItemWidget(task)
        w.removeRequested.connect(self.removeRequested.emit)
        w.retryRequested.connect(self.retryRequested.emit)
        w.formatChanged.connect(self.formatChanged.emit)
        self.items[task.id] = w
        self.listLayout.addWidget(w)
        self._update_empty()

    def update_progress(self, task_id: str, pct: int):
        w = self.items.get(task_id)
        if w:
            w.set_progress(pct)

    def update_status(self, task_id: str, status: str, error: str = ""):
        w = self.items.get(task_id)
        if w:
            w.set_status(status, error)

    def update_format(self, task_id: str, fmt: str):
        w = self.items.get(task_id)
        if w:
            w.set_format(fmt)

    def remove_item(self, task_id: str):
        w = self.items.pop(task_id, None)
        if w:
            self.listLayout.removeWidget(w)
            w.deleteLater()
        self._update_empty()

    def sync(self, tasks: list[Task]):
        present = {t.id for t in tasks}
        for t in tasks:
            if t.id not in self.items:
                self.add_item(t)
            else:
                self.items[t.id].set_format(t.target_format)
                self.items[t.id].set_status(t.status)
        for tid in list(self.items):
            if tid not in present:
                self.remove_item(tid)

    def clear(self):
        for w in self.items.values():
            self.listLayout.removeWidget(w)
            w.deleteLater()
        self.items.clear()
        self._update_empty()

    def retranslate(self):
        self.emptyLabel.setText(tr("convert.queue.empty"))
        self.statTotal.setText(tr("convert.queue.stats.total", n=0))
        self.statRunning.setText(tr("convert.queue.stats.running", n=0))
        self.statError.setText(tr("convert.queue.stats.error", n=0))
        for w in self.items.values():
            w.retranslate()
