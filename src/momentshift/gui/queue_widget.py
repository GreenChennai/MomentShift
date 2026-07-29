"""Batch queue list: one card per task with live progress and actions."""

import math
from pathlib import Path

from ..core.qt_compat import QWidget, QHBoxLayout, QVBoxLayout, QLabel, Signal, Qt, QApplication
from qfluentwidgets import (
    CardWidget,
    FluentIcon as FIF,
    ProgressBar,
    ComboBox,
    TransparentToolButton,
    StrongBodyLabel,
    CaptionLabel,
    InfoBar,
    InfoBarPosition,
)
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


class ClickLabel(QLabel):
    """A label that emits ``clicked`` on mouse press (for copy-to-clipboard)."""

    clicked = Signal()

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)


class QueueItemWidget(CardWidget):
    """A single task row inside the queue."""

    removeRequested = Signal(str)
    retryRequested = Signal(str)
    formatChanged = Signal(str, str)

    def __init__(self, task: Task, parent=None):
        super().__init__(parent)
        self.task = task
        self.setMinimumHeight(64)

        main = QHBoxLayout(self)
        main.setContentsMargins(12, 8, 12, 8)
        main.setSpacing(10)

        # icon
        self.iconLabel = QLabel()
        self.iconLabel.setPixmap(
            CATEGORY_ICON.get(task.category, FIF.DOCUMENT).icon().pixmap(26, 26)
        )
        self.iconLabel.setFixedSize(30, 30)

        # name + path + result info (size / output path, shown when done)
        textWidget = QWidget()
        tcol = QVBoxLayout(textWidget)
        tcol.setContentsMargins(0, 0, 0, 0)
        tcol.setSpacing(2)
        self.nameLabel = StrongBodyLabel(Path(task.input_path).name)
        self.subLabel = CaptionLabel(str(Path(task.input_path).parent))
        self.subLabel.setObjectName("queueSub")
        self.sizeLabel = CaptionLabel("")
        self.sizeLabel.setObjectName("queueStatus")
        self.pathLabel = ClickLabel("")
        self.pathLabel.setObjectName("queueSub")
        self.pathLabel.setCursor(Qt.CursorShape.PointingHandCursor)
        infoRow = QHBoxLayout()
        infoRow.setSpacing(8)
        infoRow.addWidget(self.sizeLabel)
        infoRow.addWidget(self.pathLabel)
        infoRow.addStretch(1)
        infoRowW = QWidget()
        infoRowW.setLayout(infoRow)
        infoRowW.setVisible(False)
        tcol.addWidget(self.nameLabel)
        tcol.addWidget(self.subLabel)
        tcol.addWidget(infoRowW)
        self.infoRowW = infoRowW
        self.pathLabel.clicked.connect(self._copy)

        # per-row target format override (same category only)
        self.formatCombo = ComboBox()
        for fmt in TARGET_GROUPS.get(task.category, []):
            self.formatCombo.addItem(fmt.upper(), userData=fmt)
        self.formatCombo.setFixedWidth(92)
        self._set_combo(task.target_format)
        self.formatCombo.currentIndexChanged.connect(self._on_format)

        # Merge tasks combine several inputs into one output: show a summary
        # instead of a single filename and hide the per-row format override.
        if task.merge:
            n = len(task.input_paths or [])
            self.nameLabel.setText(tr("convert.queue.merge_name", n=n))
            self.formatCombo.setVisible(False)

        # progress + percentage
        progCol = QVBoxLayout()
        progCol.setContentsMargins(0, 0, 0, 0)
        progCol.setSpacing(2)
        self.progress = ProgressBar()
        self.progress.setFixedWidth(150)
        self.progress.setValue(task.progress)
        self.pctLabel = CaptionLabel(f"{task.progress}%")
        self.pctLabel.setObjectName("queueStatus")
        self.pctLabel.setFixedWidth(46)
        progRow = QHBoxLayout()
        progRow.setSpacing(6)
        progRow.addWidget(self.progress)
        progRow.addWidget(self.pctLabel)
        progCol.addLayout(progRow)

        # status
        self.statusLabel = QLabel(tr(f"convert.status.{task.status}"))
        self.statusLabel.setFixedWidth(72)
        self.statusLabel.setObjectName("queueStatus")

        # actions
        self.retryBtn = TransparentToolButton(FIF.SYNC, self)
        self.retryBtn.setToolTip(tr("convert.action.retry"))
        self.retryBtn.setFixedSize(32, 32)
        self.retryBtn.setVisible(task.status == Task.FAILED)
        self.retryBtn.clicked.connect(lambda: self.retryRequested.emit(task.id))

        self.copyBtn = TransparentToolButton(FIF.COPY, self)
        self.copyBtn.setToolTip(tr("convert.result.copy"))
        self.copyBtn.setFixedSize(32, 32)
        self.copyBtn.setVisible(task.status == Task.DONE)
        self.copyBtn.clicked.connect(self._copy)

        self.removeBtn = TransparentToolButton(FIF.DELETE, self)
        self.removeBtn.setToolTip(tr("convert.action.remove"))
        self.removeBtn.setFixedSize(32, 32)
        self.removeBtn.clicked.connect(lambda: self.removeRequested.emit(task.id))

        main.addWidget(self.iconLabel)
        main.addWidget(textWidget, 1)
        main.addWidget(self.formatCombo)
        main.addLayout(progCol)
        main.addWidget(self.statusLabel)
        main.addWidget(self.retryBtn)
        main.addWidget(self.copyBtn)
        main.addWidget(self.removeBtn)

    # -- helpers ---------------------------------------------------------
    def _set_combo(self, fmt: str):
        for i in range(self.formatCombo.count()):
            self.formatCombo.setCurrentIndex(i)
            if self.formatCombo.currentData() == fmt:
                break

    # -- updates from manager --------------------------------------------
    def set_progress(self, pct: int):
        self.progress.setValue(pct)
        self.pctLabel.setText(f"{pct}%")

    def set_status(self, status: str, error: str = ""):
        self.task.status = status
        self.statusLabel.setText(tr(f"convert.status.{status}"))
        self.retryBtn.setVisible(status == Task.FAILED)
        self.copyBtn.setVisible(status == Task.DONE)
        if status == Task.DONE:
            before, after = self.task.src_size, self.task.dst_size
            if before and after and after != before:
                pct = (after - before) / before * 100
                color = "#cf5c5c" if pct > 0 else "#4a9d5b"
                self.sizeLabel.setStyleSheet(f"color: {color};")
            else:
                self.sizeLabel.setStyleSheet("")
            self.sizeLabel.setText(format_size_compare(before, after))
            self.pathLabel.setText(Path(self.task.output_path).name)
            self.pathLabel.setToolTip(self.task.output_path)
            self.infoRowW.setVisible(True)
            self.setToolTip("")
        else:
            self.infoRowW.setVisible(False)
            if status == Task.FAILED and error:
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
        self.statusLabel.setText(tr(f"convert.status.{self.task.status}"))
        self.retryBtn.setToolTip(tr("convert.action.retry"))
        self.removeBtn.setToolTip(tr("convert.action.remove"))
        self.copyBtn.setToolTip(tr("convert.result.copy"))


class QueueListWidget(QWidget):
    """Holds the queue items and keeps them in sync with the manager."""

    removeRequested = Signal(str)
    retryRequested = Signal(str)
    formatChanged = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: dict[str, QueueItemWidget] = {}
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(8)

        self.emptyLabel = QLabel(tr("convert.queue.empty"))
        self.emptyLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyLabel.setObjectName("queueEmpty")
        self.layout.addWidget(self.emptyLabel)
        self._update_empty()

    def _update_empty(self):
        self.emptyLabel.setVisible(len(self.items) == 0)

    def add_item(self, task: Task):
        if task.id in self.items:
            return
        w = QueueItemWidget(task)
        w.removeRequested.connect(self.removeRequested.emit)
        w.retryRequested.connect(self.retryRequested.emit)
        w.formatChanged.connect(self.formatChanged.emit)
        self.items[task.id] = w
        self.layout.addWidget(w)
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
            self.layout.removeWidget(w)
            w.deleteLater()
        self._update_empty()

    def sync(self, tasks: list[Task]):
        present = {t.id for t in tasks}
        for t in tasks:
            if t.id not in self.items:
                self.add_item(t)
            else:
                # keep existing rows in sync with the model
                self.items[t.id].set_format(t.target_format)
                self.items[t.id].set_status(t.status)
        for tid in list(self.items):
            if tid not in present:
                self.remove_item(tid)

    def clear(self):
        for w in self.items.values():
            self.layout.removeWidget(w)
            w.deleteLater()
        self.items.clear()
        self._update_empty()

    def retranslate(self):
        self.emptyLabel.setText(tr("convert.queue.empty"))
        for w in self.items.values():
            w.retranslate()
