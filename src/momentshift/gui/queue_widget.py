"""Convert queue widgets: progress bar, status pill, item card, list widget.

Public API preserved for ``convert_interface``:
- ``human_size(n)``, ``format_size_compare(before, after)``
- ``StatusPill(status)`` + ``set_status(status)``
- ``ProgressBar()`` + ``set_value(int)`` / ``set_error(bool)``
- ``QueueItemWidget(task)`` + ``set_progress/set_status/retranslate``
  and signals ``removeRequested(str)``, ``retryRequested(str)``
- ``QueueListWidget()`` + ``add_item/update_progress/update_status/
  remove_item/sync/clear/retranslate/_update_stats`` and the same two signals.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QColor, QPainter, QBrush, QPen
from PyQt6.QtCore import Qt, QObject, QTimer, QEvent
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QHBoxLayout, QScrollArea, QSizePolicy

from qfluentwidgets import FluentIcon as FIF, CaptionLabel, BodyLabel

from ..core.qt_compat import QWidget, Signal, QApplication
from ..i18n.translator import tr
from .theme import (
    ThemedCard, icon_btn, muted_text, accent_color, sub_text,
    success_color, danger_color, border_color, ext_badge, text_strong,
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
    """返回富文本：``1.2 MB → 800.0 KB <font color>(-33%)</font>``。

    百分比颜色：变小绿(#3EB68F)，变大红(#FF7279)，几乎无变化黑(#000000)。
    """
    before, after = int(before or 0), int(after or 0)
    if not before:
        return human_size(after) if after else ""
    if not after:
        return human_size(before)
    delta = (after - before) / before * 100
    if abs(delta) < 0.5:
        pct = "±0%"
        color = "#000000"
    elif delta < 0:
        pct = f"{delta:.0f}%"
        color = "#3EB68F"
    else:
        pct = f"+{delta:.0f}%"
        color = "#FF7279"
    return (f"{human_size(before)} → {human_size(after)} "
            f"<font color=\"{color}\">({pct})</font>")


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
    "done_sw": "#3964FE",
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
        # v0.7.7 修复1：胶囊严格按内部文字定宽，绝不随 UI 宽度拉伸
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.set_status(status)

    def set_status(self, status: str, text: str = None):
        self._status = status
        bg = _STATUS_PILL_BG.get(status, _STATUS_PILL_BG["pending"])
        fg = _STATUS_PILL_FG
        label = text if text is not None else tr(f"convert.status.{status}")
        self.setText(label)
        self.setStyleSheet(
            f"color:{fg}; background:{bg}; border-radius:9px; "
            f"padding:2px 9px; font-weight:600;"
        )


class FormatPill(QLabel):
    """格式指示胶囊（v0.7.2 Feat5）：显示「.SRC → .TGT」。

    v0.7.3 调整2：底色由中性浅灰 #ECEFF1 改为品牌绿 #3EB68F，
    文字随之改为近白，保证对比度可读。
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # v0.7.7 修复1：胶囊严格按内部文字定宽，绝不随 UI 宽度拉伸
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            "color:#F5F5F5; background:#3EB68F; border-radius:9px;"
            " padding:2px 9px; font-weight:600; font-size:11px;"
        )
        self.setText(text)


# --------------------------------------------------------------------------
# QueueItemWidget
# --------------------------------------------------------------------------
class QueueItemWidget(ThemedCard):
    removeRequested = Signal(str)
    retryRequested = Signal(str)

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self._task = task
        self._build()

    def _build(self):
        vb = QVBoxLayout(self)
        vb.setContentsMargins(14, 12, 14, 12)
        vb.setSpacing(8)

        # v0.7.4 Adj1：左侧徽标显示文件后缀（矩形 + 居中文字），取代类别图标
        src_ext = Path(self._task.input_path).suffix.upper().lstrip(".")
        top = QHBoxLayout()
        self.iconLbl = ext_badge(src_ext, self)
        self.nameLbl = MarqueeName(self)
        self.nameLbl.set_text(_basename(self._task.input_path))
        self.nameLbl.setObjectName("queueName")
        top.addWidget(self.iconLbl)
        top.addWidget(self.nameLbl, 1)
        # v0.7.7 修复1：用 spacer 吸收多余空间，保证后缀/状态胶囊按文字定宽
        top.addStretch(1)
        # v0.7.2 Feat5：格式指示胶囊 .SRC → .TGT（如 .JPG → .PNG）
        tgt = (self._task.target_format or "").upper()
        self.fmtPill = FormatPill(f".{src_ext} → .{tgt}")
        top.addWidget(self.fmtPill)
        self.pill = StatusPill(self._task.status)
        top.addWidget(self.pill)
        vb.addLayout(top)

        self.prog = ProgressBar()
        vb.addWidget(self.prog)

        # 大小对比文本（独立成行，自动换行；v0.7.1 黑字 + 百分比绿/红）
        self.detailLbl = CaptionLabel()
        self.detailLbl.setObjectName("queueStatus")
        self.detailLbl.setWordWrap(True)
        self.detailLbl.setStyleSheet("color: #000000; background: transparent;")

        self.retryBtn = icon_btn(FIF.SYNC, self)
        self.retryBtn.clicked.connect(lambda: self.retryRequested.emit(self._task.id))
        self.copyBtn = icon_btn(FIF.COPY, self)
        self.copyBtn.clicked.connect(self._copy_path)
        self.delBtn = icon_btn(FIF.DELETE, self)
        self.delBtn.clicked.connect(lambda: self.removeRequested.emit(self._task.id))

        # v0.7.2 Feat6：大小对比文本与操作按钮同行右对齐，按钮水平对齐文本行
        bottom = QHBoxLayout()
        bottom.addWidget(self.detailLbl, 1)
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
        """组合「转换前后」与「压缩前后」两段对比（v0.7.0 Bug 2，v0.7.1 换行）。

        转换阶段： ``转换 1.2 MB → 900.0 KB (-25%)``
        压缩之后：两段各占一行，百分比绿/红着色。
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

        return "<br>".join(parts)

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

    def retranslate(self):
        self.pill.set_status(self._task.status)
        self.set_status(self._task.status, self._task.error)

    def _copy_path(self):
        # v0.7.2 Bug5：只复制输出文件所在文件夹路径，而非完整文件路径
        folder = str(Path(self._task.output_path).parent)
        QApplication.clipboard().setText(folder)


# --------------------------------------------------------------------------
# QueueListWidget
# --------------------------------------------------------------------------
class QueueListWidget(QWidget):
    removeRequested = Signal(str)
    retryRequested = Signal(str)

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
            w.setStyleSheet("color: #000000; font-weight:600;")
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


# --------------------------------------------------------------------------
# ScrollAutoFollow — 队列滚动自动跟随当前任务（v0.7.4 Adj2）
# --------------------------------------------------------------------------
class ScrollAutoFollow(QObject):
    """队列滚动自动跟随当前正在处理的任务。

    - ``set_active(True)`` 进入跟随模式（任务进行中）；``set_active(False)`` 退出。
    - 任务开始处理时调用 ``ensure(item_widget)`` 将条目滚入可视区域。
    - 用户手动拖动/滚轮/键盘操作滚动条时暂停跟随，停止操作后 3s 自动恢复。

    暂停判定通过事件过滤器捕获视口滚轮/键盘事件，以及滚动条滑块的
    ``sliderPressed``/``sliderMoved``（仅拖动时触发，程序化 ``ensureWidgetVisible``
    走 ``setValue`` 不会触发，故不会自我死锁）。
    """

    RESUME_DELAY_MS = 3000

    def __init__(self, scroll_area: QScrollArea, parent=None):
        super().__init__(parent or scroll_area)
        self._scroll = scroll_area
        self._active = False
        self._user_paused = False
        self._resume_timer = QTimer(self)
        self._resume_timer.setSingleShot(True)
        self._resume_timer.timeout.connect(self._on_resume)

        sb = scroll_area.verticalScrollBar()
        sb.sliderPressed.connect(self._on_user_scroll)
        sb.sliderMoved.connect(self._on_user_scroll)

        scroll_area.installEventFilter(self)
        scroll_area.viewport().installEventFilter(self)

    def set_active(self, active: bool):
        """进入/退出跟随模式。"""
        self._active = bool(active)
        self._resume_timer.stop()
        if active:
            # 新任务开始即重置用户暂停状态，重新跟随
            self._user_paused = False

    def ensure(self, widget: QWidget):
        """把 widget 滚入可视区域（仅在跟随模式且用户未手动接管时）。"""
        if not self._active or self._user_paused or widget is None:
            return
        self._scroll.ensureWidgetVisible(widget, 10, 10)

    def _on_user_scroll(self, *_):
        if not self._active or self._user_paused:
            return
        self._user_paused = True
        self._resume_timer.start(self.RESUME_DELAY_MS)

    def _on_resume(self):
        self._user_paused = False

    def eventFilter(self, obj, event):
        if self._active and not self._user_paused:
            t = event.type()
            if t == QEvent.Type.Wheel or t == QEvent.Type.KeyPress:
                self._on_user_scroll()
        return super().eventFilter(obj, event)


def _basename(path: str) -> str:
    from pathlib import Path

    return Path(path).name


# --------------------------------------------------------------------------
# MarqueeName — 文件名显示控件（v0.7.6 修复 1）
# --------------------------------------------------------------------------
class MarqueeName(QWidget):
    """文件名显示：横向滚动轮流显示超长文本。

    v0.7.6  固定 ``max_chars`` 汉字宽，超出则横向滚动。
    v0.7.8  改为自适应宽度：由外层布局决定窗口宽，``resizeEvent``
            实时更新，长文则滚动，短文则静止。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._offset = 0
        self._char_w = 1
        self._text_w = 0
        self._window_w = 0
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)
        # 水平方向填充可用空间，竖向固定高度防止纵向撑大
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        try:
            self.setFont(BodyLabel().font())
        except Exception:
            pass
        self._fm = self.fontMetrics()
        self._char_w = max(1, self._fm.horizontalAdvance("中"))
        self.setFixedHeight(self._fm.height() + 2)

    def set_text(self, text: str) -> None:
        self._text = text or ""
        self._fm = self.fontMetrics()
        self._char_w = max(1, self._fm.horizontalAdvance("中"))
        self._text_w = self._fm.horizontalAdvance(self._text)
        self._restart_timer()
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._window_w = self.width()
        self._restart_timer()

    def _restart_timer(self) -> None:
        if self._text_w > self._window_w > 0:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self._offset = 0

    def _tick(self) -> None:
        if self._text_w <= self._window_w:
            self._timer.stop()
            self._offset = 0
            self.update()
            return
        self._offset -= 2
        gap = self._char_w * 3
        if -self._offset >= self._text_w + gap:
            self._offset = self._window_w
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(self.rect())
        painter.setPen(QPen(QColor(text_strong())))
        fm = self._fm
        base_y = (self.height() + fm.ascent() - fm.descent()) // 2
        painter.drawText(int(self._offset), int(base_y), self._text)
        painter.end()


def _counts_from(items: dict) -> dict:
    out = {"total": len(items), "running": 0, "failed": 0}
    for w in items.values():
        st = w._task.status
        if st == "running":
            out["running"] += 1
        elif st == "failed":
            out["failed"] += 1
    return out
