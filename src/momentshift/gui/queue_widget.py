"""Convert queue widgets: progress bar, status pill, item card, list widget.

Public API preserved for ``convert_interface``:
- ``human_size(n)``, ``format_size_compare(before, after)``
- ``StatusPill(status)`` + ``set_status(status)``
- ``ProgressBar()`` + ``set_value(int)`` / ``set_error(bool)``
- ``QueueItemWidget(task)`` + ``set_progress/set_status/set_format/retranslate``
  and signals ``removeRequested(str)``, ``retryRequested(str)``, ``formatChanged(str,str)``
- ``QueueListWidget()`` + ``add_item/update_progress/update_status/update_format/
  remove_item/sync/clear/retranslate/_update_stats`` and the same three signals.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPainter, QBrush, QPen
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QComboBox, QVBoxLayout, QHBoxLayout, QSpacerItem, QSizePolicy

from qfluentwidgets import FluentIcon as FIF, ComboBox, CaptionLabel, BodyLabel

from ..core.qt_compat import QWidget, Signal, QApplication
from ..core.presets import TARGET_GROUPS
from ..i18n.translator import tr
from .theme import (
    ThemedCard, icon_btn, muted_text, accent_color, sub_text,
    success_color, danger_color, border_color,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def human_size(n: int) -> str:
    n = int(n or 0)
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}"


def format_size_compare(before: int, after: int) -> str:
    """``1.2 MB → 800.0 KB (-33%)``；变大时显示 ``+N%``（v0.7.0）。"""
    before, after = int(before or 0), int(after or 0)
    if not before:
        return human_size(after) if after else ""
    if not after:
        return human_size(before)
    delta = (after - before) / before * 100
    if abs(delta) < 0.5:
        pct = "±0%"
    else:
        pct = f"{delta:+.0f}%"
    return f"{human_size(before)} → {human_size(after)}  ({pct})"


_CATEGORY_ICON = {
    "image": FIF.PHOTO,
    "video": FIF.VIDEO,
    "audio": FIF.MUSIC,
}

# Status pill colours. The pill background is the status colour (vivid, theme
# independent); the text is the inverse (near-white) so it reads clearly on any
# status colour. This matches the requested "胶囊 = 状态色, 文字 = 反色" rule.
#
# v0.7.0 状态流转：
#   等待中(灰) → 转换中(蓝) → 已完成(绿) →〔开启压缩时〕压缩中(黄) → 压缩完成(蓝)
_STATUS_PILL_BG = {
    "pending": "#8A8A8A",
    "running": "#2F98FF",
    "done": "#3EB68F",
    "failed": "#FF7279",
    "canceled": "#8A8A8A",
    "compressing": "#C7920A",
    "compress_done": "#3964FE",
}
_STATUS_PILL_FG = "#F5F5F5"


# --------------------------------------------------------------------------
# ProgressBar
# --------------------------------------------------------------------------
class ProgressBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0
        self._error = False
        self.setFixedHeight(6)

    def set_value(self, v: int):
        self._value = max(0, min(100, int(v)))
        self.update()

    def set_error(self, b: bool):
        self._error = bool(b)
        self.update()

    def paintEvent(self, event):
        from PyQt6.QtCore import QRect

        w, h = self.width(), self.height()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        track = QColor(border_color())
        painter.setBrush(QBrush(track))
        painter.drawRoundedRect(QRect(0, 0, w, h), h // 2, h // 2)
        fw = int(w * self._value / 100)
        if fw <= 0:
            return
        if self._error:
            fill = danger_color()
        elif self._value >= 100:
            fill = success_color()
        else:
            fill = accent_color()
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(QRect(0, 0, fw, h), h // 2, h // 2)


# --------------------------------------------------------------------------
# StatusPill
# --------------------------------------------------------------------------
class StatusPill(QLabel):
    def __init__(self, status: str = "pending", parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_status(status)

    def set_status(self, status: str):
        self._status = status
        bg = _STATUS_PILL_BG.get(status, _STATUS_PILL_BG["pending"])
        fg = _STATUS_PILL_FG
        self.setText(tr(f"convert.status.{status}"))
        self.setStyleSheet(
            f"color:{fg}; background:{bg}; border-radius:9px; "
            f"padding:2px 9px; font-weight:600;"
        )


# --------------------------------------------------------------------------
# QueueItemWidget
# --------------------------------------------------------------------------
class QueueItemWidget(ThemedCard):
    removeRequested = Signal(str)
    retryRequested = Signal(str)
    formatChanged = Signal(str, str)

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self._task = task
        self._build()

    def _build(self):
        vb = QVBoxLayout(self)
        vb.setContentsMargins(14, 12, 14, 12)
        vb.setSpacing(8)

        top = QHBoxLayout()
        icon = _CATEGORY_ICON.get(self._task.category, FIF.DOCUMENT)
        self.iconLbl = QLabel()
        self.iconLbl.setPixmap(icon.icon(accent_color()).pixmap(20, 20))
        self.nameLbl = BodyLabel(_basename(self._task.input_path))
        self.nameLbl.setObjectName("queueName")
        self.nameLbl.setToolTip(self._task.input_path)
        top.addWidget(self.iconLbl)
        top.addWidget(self.nameLbl, 1)
        self.pill = StatusPill(self._task.status)
        top.addWidget(self.pill)
        vb.addLayout(top)

        self.prog = ProgressBar()
        vb.addWidget(self.prog)

        bottom = QHBoxLayout()
        self.detailLbl = CaptionLabel()
        self.detailLbl.setObjectName("queueStatus")
        self.detailLbl.setStyleSheet(f"color: {muted_text()};")
        bottom.addWidget(self.detailLbl, 1)

        self.fmtCombo = ComboBox()
        for f in TARGET_GROUPS.get(self._task.category, []):
            self.fmtCombo.addItem(f.upper())
        self.fmtCombo.setCurrentText(self._task.target_format.upper())
        self.fmtCombo.setFixedWidth(78)
        self.fmtCombo.currentTextChanged.connect(
            lambda t: self.formatChanged.emit(self._task.id, t.lower())
        )
        bottom.addWidget(self.fmtCombo)

        self.retryBtn = icon_btn(FIF.SYNC, tr("convert.action.retry"), self)
        self.retryBtn.clicked.connect(lambda: self.retryRequested.emit(self._task.id))
        self.copyBtn = icon_btn(FIF.COPY, tr("convert.action.copy"), self)
        self.copyBtn.clicked.connect(self._copy_path)
        self.delBtn = icon_btn(FIF.DELETE, tr("convert.action.remove"), self)
        self.delBtn.clicked.connect(lambda: self.removeRequested.emit(self._task.id))
        bottom.addWidget(self.retryBtn)
        bottom.addWidget(self.copyBtn)
        bottom.addWidget(self.delBtn)
        vb.addLayout(bottom)

        self.set_status(self._task.status, self._task.error)
        self.set_progress(self._task.progress)

    def set_progress(self, pct: int):
        self.prog.set_value(pct)

    # -- 详情行 ---------------------------------------------------------
    def _convert_sizes(self) -> tuple[int, int]:
        """(转换前, 转换后)。压缩已跑过时，转换后大小存在 pre_compress_size。"""
        before = int(getattr(self._task, "src_size", 0) or 0)
        pre = int(getattr(self._task, "pre_compress_size", 0) or 0)
        after = pre or int(getattr(self._task, "dst_size", 0) or 0)
        return before, after

    def _compress_sizes(self) -> tuple[int, int]:
        """(压缩前, 压缩后)。"""
        pre = int(getattr(self._task, "pre_compress_size", 0) or 0)
        post = int(getattr(self._task, "dst_size", 0) or 0)
        return pre, post

    def _detail_text(self) -> str:
        """组合「转换前后」与「压缩前后」两段对比（v0.7.0 Bug 2）。

        转换阶段： ``转换 1.2 MB → 900.0 KB (-25%)``
        压缩之后： ``转换 … · 压缩 900.0 KB → 700.0 KB (-22%)``
        """
        parts: list[str] = []

        cb, ca = self._convert_sizes()
        conv = format_size_compare(cb, ca)
        if conv:
            parts.append(f"{tr('convert.label.convert')} {conv}")

        if getattr(self._task, "compress_done", False):
            pb, pa = self._compress_sizes()
            comp = format_size_compare(pb, pa)
            if comp:
                parts.append(f"{tr('convert.label.compress')} {comp}")

        return "   ·   ".join(parts)

    # -- 状态 -----------------------------------------------------------
    def set_status(self, status: str, error: str = ""):
        """更新状态胶囊与详情行。

        v0.7.0：压缩相关状态也走这里统一上色，不再用 setStyleSheet 打补丁。
        任务已进入压缩阶段后，再收到 ``done`` 不会把胶囊刷回绿色。
        """
        compressed = getattr(self._task, "compress_done", False)
        if status == "done" and compressed:
            status = "compress_done"

        if status not in ("compressing", "compress_done"):
            self._task.status = status
        self.pill.set_status(status)
        self.prog.set_error(status == "failed")
        self.retryBtn.setVisible(status in ("failed", "canceled"))

        if status in ("done", "compress_done"):
            self.detailLbl.setText(self._detail_text())
        elif status == "failed":
            self.detailLbl.setText((error or tr("convert.status.failed"))[:60])
        elif status == "running":
            self.detailLbl.setText(f"{self._task.progress}%")
        elif status == "compressing":
            self.detailLbl.setText(self._detail_text())
        else:
            self.detailLbl.setText("")

    def set_compress(self, pct: int, done: bool = False):
        """压缩阶段：进度 + 黄「压缩中」/ 蓝「压缩完成」胶囊。"""
        self.prog.set_value(pct)
        self.prog.set_error(False)
        self.pill.set_status("compress_done" if done else "compressing")
        if done:
            self.detailLbl.setText(self._detail_text())
        else:
            base = self._detail_text()
            self.detailLbl.setText(f"{base}   ·   {pct}%" if base else f"{pct}%")

    def restore_after_compress(self):
        """回到「已完成」态（压缩过的任务保持蓝色压缩完成）。"""
        self.set_status("done")

    def set_format(self, fmt: str):
        self._task.target_format = fmt
        self.fmtCombo.blockSignals(True)
        self.fmtCombo.setCurrentText(fmt.upper())
        self.fmtCombo.blockSignals(False)

    def retranslate(self):
        self.pill.set_status(self._task.status)
        self.retryBtn.setToolTip(tr("convert.action.retry"))
        self.copyBtn.setToolTip(tr("convert.action.copy"))
        self.delBtn.setToolTip(tr("convert.action.remove"))
        self.set_status(self._task.status, self._task.error)

    def _copy_path(self):
        QApplication.clipboard().setText(self._task.output_path)


# --------------------------------------------------------------------------
# QueueListWidget
# --------------------------------------------------------------------------
class QueueListWidget(QWidget):
    removeRequested = Signal(str)
    retryRequested = Signal(str)
    formatChanged = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: dict[str, QueueItemWidget] = {}

        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(8)

        self.statsBar = QWidget()
        hb = QHBoxLayout(self.statsBar)
        hb.setContentsMargins(2, 0, 2, 0)
        hb.setSpacing(14)
        self.statTotal = CaptionLabel()
        self.statRun = CaptionLabel()
        self.statErr = CaptionLabel()
        for w in (self.statTotal, self.statRun, self.statErr):
            w.setStyleSheet(f"color: {muted_text()}; font-weight:600;")
            hb.addWidget(w)
        hb.addStretch(1)
        vb.addWidget(self.statsBar)

        self.listWidget = QWidget()
        self.listLayout = QVBoxLayout(self.listWidget)
        self.listLayout.setContentsMargins(0, 0, 0, 0)
        self.listLayout.setSpacing(8)
        self.listLayout.addStretch(1)
        vb.addWidget(self.listWidget, 1)

        self.emptyHint = CaptionLabel()
        self.emptyHint.setObjectName("queueEmpty")
        self.emptyHint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyHint.setStyleSheet(f"color: {muted_text()}; padding: 24px 0;")
        self.emptyHint.setText(tr("convert.queue.empty"))
        vb.addWidget(self.emptyHint)
        self._refresh_empty()

    def _refresh_empty(self):
        # The "queue empty" hint text was removed by design; keep the label
        # hidden so no empty gap is left behind.
        self.emptyHint.setVisible(False)

    def add_item(self, task):
        if task.id in self.items:
            return
        w = QueueItemWidget(task)
        w.removeRequested.connect(self.removeRequested)
        w.retryRequested.connect(self.retryRequested)
        w.formatChanged.connect(self.formatChanged)
        self.items[task.id] = w
        # insert before the trailing stretch
        self.listLayout.insertWidget(self.listLayout.count() - 1, w)
        self._refresh_empty()

    def update_progress(self, task_id: str, pct: int):
        w = self.items.get(task_id)
        if w:
            w.set_progress(pct)

    def update_status(self, task_id: str, status: str, error: str = ""):
        w = self.items.get(task_id)
        if w:
            w.set_status(status, error)
            if status in ("done", "failed", "canceled"):
                self._update_stats(_counts_from(self.items))

    def update_format(self, task_id: str, fmt: str):
        w = self.items.get(task_id)
        if w:
            w.set_format(fmt)

    def update_compress(self, task_id: str, pct: int, done: bool = False):
        """v0.6.0：更新压缩阶段 UI（蓝色进度条）。"""
        w = self.items.get(task_id)
        if w:
            w.set_compress(pct, done)

    def restore_compress(self, task_id: str):
        """压缩完成后恢复绿色已完成状态。"""
        w = self.items.get(task_id)
        if w:
            w.restore_after_compress()

    def update_compress_start(self, task_id: str):
        """压缩开始 → 黄色「压缩中」。"""
        w = self.items.get(task_id)
        if w:
            w.set_compress(0, done=False)

    def update_compress_waiting(self, task_id: str):
        """排队等待压缩 → 灰色「等待中」。"""
        w = self.items.get(task_id)
        if w:
            w.pill.set_status("pending")
            w.prog.set_value(0)

    def update_compress_done(self, task_id: str):
        """压缩完成 → 蓝色「压缩完成」+ 双段大小对比。"""
        w = self.items.get(task_id)
        if w:
            w.set_compress(100, done=True)
            self._update_stats(_counts_from(self.items))

    def remove_item(self, task_id: str):
        w = self.items.pop(task_id, None)
        if w:
            w.deleteLater()
        self._refresh_empty()
        self._update_stats(_counts_from(self.items))

    def sync(self, tasks):
        ids = {t.id for t in tasks}
        for tid in list(self.items):
            if tid not in ids:
                self.remove_item(tid)
        for t in tasks:
            if t.id not in self.items:
                self.add_item(t)
        self._update_stats(_counts_from(self.items))

    def clear(self):
        for w in self.items.values():
            w.deleteLater()
        self.items.clear()
        self._refresh_empty()
        self._update_stats(_counts_from(self.items))

    def retranslate(self):
        for w in self.items.values():
            w.retranslate()
        self.emptyHint.setText(tr("convert.queue.empty"))
        self._update_stats(_counts_from(self.items))

    def _update_stats(self, counts: dict):
        self.statTotal.setText(tr("convert.queue.total", n=counts.get("total", 0)))
        self.statRun.setText(tr("convert.queue.running", n=counts.get("running", 0)))
        self.statErr.setText(tr("convert.queue.failed", n=counts.get("failed", 0)))


def _basename(path: str) -> str:
    from pathlib import Path

    return Path(path).name


def _counts_from(items: dict) -> dict:
    out = {"total": len(items), "running": 0, "failed": 0}
    for w in items.values():
        st = w._task.status
        if st == "running":
            out["running"] += 1
        elif st == "failed":
            out["failed"] += 1
    return out
