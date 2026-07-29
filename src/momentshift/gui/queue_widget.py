"""Batch queue list: one card per task with live progress and actions."""

from pathlib import Path

from ..core.qt_compat import QWidget, QHBoxLayout, QVBoxLayout, QLabel, Signal, Qt
from qfluentwidgets import (
    CardWidget,
    FluentIcon as FIF,
    ProgressBar,
    ComboBox,
    TransparentPushButton,
    StrongBodyLabel,
    CaptionLabel,
)
from ..i18n.translator import tr
from ..core.presets import TARGET_GROUPS
from ..core.models import Task

CATEGORY_ICON = {"image": FIF.PHOTO, "audio": FIF.MUSIC, "video": FIF.VIDEO}


class QueueItemWidget(CardWidget):
    """A single task row inside the queue."""

    removeRequested = Signal(str)
    retryRequested = Signal(str)
    formatChanged = Signal(str, str)

    def __init__(self, task: Task, parent=None):
        super().__init__(parent)
        self.task = task
        self.setMinimumHeight(60)

        main = QHBoxLayout(self)
        main.setContentsMargins(12, 8, 12, 8)
        main.setSpacing(10)

        # icon
        self.iconLabel = QLabel()
        self.iconLabel.setPixmap(
            CATEGORY_ICON.get(task.category, FIF.DOCUMENT).icon().pixmap(26, 26)
        )
        self.iconLabel.setFixedSize(30, 30)

        # name + path
        textWidget = QWidget()
        tcol = QVBoxLayout(textWidget)
        tcol.setContentsMargins(0, 0, 0, 0)
        tcol.setSpacing(2)
        self.nameLabel = StrongBodyLabel(Path(task.input_path).name)
        self.subLabel = CaptionLabel(str(Path(task.input_path).parent))
        self.subLabel.setObjectName("queueSub")
        tcol.addWidget(self.nameLabel)
        tcol.addWidget(self.subLabel)

        # per-row target format override
        self.formatCombo = ComboBox()
        for fmt in TARGET_GROUPS.get(task.category, []):
            self.formatCombo.addItem(fmt.upper(), userData=fmt)
        self.formatCombo.setFixedWidth(92)
        self.formatCombo.blockSignals(True)
        for i in range(self.formatCombo.count()):
            self.formatCombo.setCurrentIndex(i)
            if self.formatCombo.currentData() == task.target_format:
                break
        self.formatCombo.blockSignals(False)
        self.formatCombo.currentIndexChanged.connect(self._on_format)

        # progress
        self.progress = ProgressBar()
        self.progress.setFixedWidth(150)
        self.progress.setValue(task.progress)

        # status
        self.statusLabel = QLabel(tr(f"convert.status.{task.status}"))
        self.statusLabel.setFixedWidth(72)
        self.statusLabel.setObjectName("queueStatus")

        # actions
        self.retryBtn = TransparentPushButton(icon=FIF.SYNC)
        self.retryBtn.setToolTip(tr("convert.action.retry"))
        self.retryBtn.setFixedSize(32, 32)
        self.retryBtn.setVisible(task.status == Task.FAILED)
        self.retryBtn.clicked.connect(
            lambda: self.retryRequested.emit(task.id)
        )
        self.removeBtn = TransparentPushButton(icon=FIF.DELETE)
        self.removeBtn.setToolTip(tr("convert.action.remove"))
        self.removeBtn.setFixedSize(32, 32)
        self.removeBtn.clicked.connect(
            lambda: self.removeRequested.emit(task.id)
        )

        main.addWidget(self.iconLabel)
        main.addWidget(textWidget, 1)
        main.addWidget(self.formatCombo)
        main.addWidget(self.progress)
        main.addWidget(self.statusLabel)
        main.addWidget(self.retryBtn)
        main.addWidget(self.removeBtn)

    # -- updates from manager --------------------------------------------
    def set_progress(self, pct: int):
        self.progress.setValue(pct)

    def set_status(self, status: str, error: str = ""):
        self.task.status = status
        self.statusLabel.setText(tr(f"convert.status.{status}"))
        self.retryBtn.setVisible(status == Task.FAILED)
        if status == Task.FAILED and error:
            self.setToolTip(error.strip().splitlines()[-1] if error.strip() else "")

    def set_format(self, fmt: str):
        self.task.target_format = fmt
        self.formatCombo.blockSignals(True)
        for i in range(self.formatCombo.count()):
            self.formatCombo.setCurrentIndex(i)
            if self.formatCombo.currentData() == fmt:
                break
        self.formatCombo.blockSignals(False)

    def _on_format(self, _index):
        fmt = self.formatCombo.currentData()
        if fmt:
            self.formatChanged.emit(self.task.id, fmt)

    def retranslate(self):
        self.statusLabel.setText(tr(f"convert.status.{self.task.status}"))
        self.retryBtn.setToolTip(tr("convert.action.retry"))
        self.removeBtn.setToolTip(tr("convert.action.remove"))


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
