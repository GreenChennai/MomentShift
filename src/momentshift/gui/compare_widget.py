"""放大界面的「前后对比」控件。

职责边界：
- 做：渲染前后对比控件、在后台线程解码封面图并回主线程贴图。
- 不做：不做放大计算；不管理任务队列。

依赖：core/ffmpeg、core/logger、core/platform、core/qt_compat、gui/theme、i18n/translator；被依赖：放大界面的对比入口。

封面图（poster）在**后台线程**里解码，见 :class:`_PosterTask`。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QBrush, QImage, QImageReader, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout
from qfluentwidgets import CaptionLabel, PushButton, StrongBodyLabel

from ..core.ffmpeg import find_ffmpeg
from ..core.logger import get_logger
from ..core.platform import run_silent
from ..core.qt_compat import QObject, QRunnable, QThreadPool, Signal
from ..i18n.translator import tr
from .theme import ThemedCard, accent_color, muted_text

log = get_logger("compare")

_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".wmv", ".m4v", ".3gp", ".ts"}

# 抽帧超时。60s 是原实现的取值，保留；关键是它现在跑在工作线程里。
_POSTER_TIMEOUT = 60


def _load_poster_image(path: str | None) -> QImage:
    """把 ``path`` 解码成一张 :class:`QImage`（**只能在工作线程调用**）。

    返回 QImage 而不是 QPixmap 是硬性要求：QPixmap 绑定图形后端，只允许在 GUI
    线程构造；QImage 是纯内存位图，跨线程安全。GUI 线程收到后再
    ``QPixmap.fromImage`` 即可。

    GIF 也走 QImageReader（默认读第一帧），不用 QMovie —— QMovie 是 QObject，
    在工作线程里创建同样越界。
    """
    if not path or not Path(path).exists():
        return QImage()
    ext = Path(path).suffix.lower()
    if ext in _VIDEO_EXTS:
        return _video_poster_image(path)
    reader = QImageReader(path)
    reader.setAutoTransform(True)  # 尊重 EXIF 方向，否则手机竖拍图会躺倒
    return reader.read() or QImage()


def _video_poster_image(path: str) -> QImage:
    """用 ffmpeg 抽第 1 秒的一帧当封面（**只能在工作线程调用**）。"""
    ff = find_ffmpeg()
    if not ff:
        return QImage()
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)  # 只借文件名；mkstemp 比 mktemp 少一个 TOCTOU 竞态
    try:
        run_silent(
            [ff, "-y", "-ss", "00:00:01", "-i", path, "-frames:v", "1", "-q:v", "2", tmp],
            capture_output=True,
            timeout=_POSTER_TIMEOUT,
        )
        return QImage(tmp)
    except (OSError, subprocess.SubprocessError):
        log.warning("对比封面抽帧失败：%s", Path(path).name)
        return QImage()
    finally:
        try:
            os.remove(tmp)
        except OSError:  # 静默原因：临时封面清理失败非致命，交给操作系统回收
            pass


class _PosterSignals(QObject):
    """工作线程 → GUI 线程的封面回传信号。

    ``generation`` 用来丢弃过期结果：用户快速点了 A 再点 B，A 的抽帧可能后完成，
    没有代次号就会把 B 的画面覆盖回 A。
    """

    ready = Signal(int, str, object)  # (generation, slot, QImage)


class _PosterTask(QRunnable):
    """在线程池里解码一张封面。

    v0.8.0 RISK-02：``_video_poster`` 原来直接在 GUI 线程调
    ``subprocess.run(..., timeout=60)``——只要用户对比的是视频，界面最长会假死
    60 秒（Windows 还会弹「程序无响应」）。现在整段搬到 QThreadPool。
    """

    def __init__(self, generation: int, slot: str, path: str | None, signals: _PosterSignals):
        super().__init__()
        self._generation = generation
        self._slot = slot
        self._path = path
        self._signals = signals

    def run(self) -> None:
        try:
            image = _load_poster_image(self._path)
        except Exception:
            # 兜底：工作线程里抛出未捕获异常会直接打爆 QThreadPool 的线程。
            log.exception("对比封面解码异常：%s", self._path)
            image = QImage()
        self._signals.ready.emit(self._generation, self._slot, image)


class _RevealLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._before: QPixmap | None = None
        self._after: QPixmap | None = None
        self._mode = "reveal"
        self._ratio = 0.5
        self._dragging = False
        self._divider = accent_color()
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setMinimumHeight(200)

    def set_pixmaps(self, before: QPixmap | None, after: QPixmap | None):
        self._before = before
        self._after = after
        self.update()

    def set_mode(self, mode: str):
        self._mode = mode
        self.update()

    def _restyle(self):
        self._divider = accent_color()
        self.update()

    def _scaled(self, pm: QPixmap, rect: QRect) -> QPixmap:
        return pm.scaled(
            rect.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _draw(self, painter: QPainter, pm: QPixmap, rect: QRect):
        scaled = self._scaled(pm, rect)
        dx = (scaled.width() - rect.width()) // 2
        dy = (scaled.height() - rect.height()) // 2
        painter.drawPixmap(rect, scaled, QRect(dx, dy, rect.width(), rect.height()))

    def paintEvent(self, event):
        if not self._before and not self._after:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        if self._after:
            self._draw(painter, self._after, rect)
        if self._before and self._mode == "reveal":
            split = int(rect.width() * self._ratio)
            painter.save()
            painter.setClipRect(QRect(rect.left(), rect.top(), split, rect.height()))
            self._draw(painter, self._before, rect)
            painter.restore()
            painter.setPen(QPen(self._divider, 2))
            painter.drawLine(split, rect.top(), split, rect.bottom())
            painter.setBrush(QBrush(self._divider))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(split - 7, rect.center().y() - 7, 14, 14)
        elif self._before and self._mode == "side":
            half = rect.width() // 2
            self._draw(painter, self._before, QRect(rect.left(), rect.top(), half, rect.height()))
            if self._after:
                self._draw(
                    painter,
                    self._after,
                    QRect(rect.left() + half, rect.top(), rect.width() - half, rect.height()),
                )
            painter.setPen(QPen(self._divider, 2))
            x = rect.left() + half
            painter.drawLine(x, rect.top(), x, rect.bottom())

    def mousePressEvent(self, event):
        if self._mode == "reveal" and self._before:
            self._dragging = True
            self._ratio = max(0.05, min(0.95, event.position().x() / self.width()))
            self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._ratio = max(0.05, min(0.95, event.position().x() / self.width()))
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._dragging = False
        super().mouseReleaseEvent(event)


class CompareWidget(ThemedCard):
    """「前后对比」卡片。

    ``set_paths`` 立即返回，封面在后台线程解码完再回填，界面不会卡。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # WorkerSignals 必须挂 parent（这里是 self）： /  都栽在
        # 无主 QObject 被 GC 回收后信号发不出、甚至崩溃上。
        self._poster_signals = _PosterSignals(self)
        self._poster_signals.ready.connect(self._on_poster_ready)
        self._poster_pool = QThreadPool(self)
        self._poster_pool.setMaxThreadCount(2)  # 前后各一张，够用
        self._poster_gen = 0
        self._pixmaps: dict[str, QPixmap | None] = {"before": None, "after": None}

        vb = QVBoxLayout(self)
        vb.setContentsMargins(16, 14, 16, 14)
        vb.setSpacing(10)

        top = QHBoxLayout()
        self.titleLbl = StrongBodyLabel(tr("upscale.compare.title"))
        top.addWidget(self.titleLbl)
        top.addStretch(1)
        self.revealBtn = PushButton(tr("upscale.compare.reveal"))
        self.revealBtn.setCheckable(True)
        self.revealBtn.setChecked(True)
        self.sideBtn = PushButton(tr("upscale.compare.side"))
        self.sideBtn.setCheckable(True)
        self.revealBtn.clicked.connect(lambda: self._set_mode("reveal"))
        self.sideBtn.clicked.connect(lambda: self._set_mode("side"))
        top.addWidget(self.revealBtn)
        top.addWidget(self.sideBtn)
        vb.addLayout(top)

        self.label = _RevealLabel()
        vb.addWidget(self.label, 1)

        self.emptyHint = CaptionLabel(tr("upscale.compare.empty"))
        self.emptyHint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyHint.setStyleSheet(f"color: {muted_text()}; padding: 40px 0;")
        vb.addWidget(self.emptyHint)

        self._mode = "reveal"
        self._restyle()

    def set_paths(self, before: str | None, after: str | None):
        """异步载入前后两张封面。

        v0.8.0 RISK-02：原实现同步调 ffmpeg 抽帧，视频对比会把 GUI 卡住最多
        60 秒。现在只投递两个后台任务，图片解码完由 ``_on_poster_ready`` 回填。
        """
        self._poster_gen += 1
        gen = self._poster_gen
        self._pixmaps = {"before": None, "after": None}
        self.label.set_pixmaps(None, None)

        has = bool(before or after)
        self.label.setVisible(has)
        # 设计上已删除「选择队列项以对比」提示，没有结果时对比区就是空的。
        self.emptyHint.setVisible(False)
        if not has:
            return

        for slot, path in (("before", before), ("after", after)):
            self._poster_pool.start(_PosterTask(gen, slot, path, self._poster_signals))

    def _on_poster_ready(self, generation: int, slot: str, image) -> None:
        """工作线程回传封面（GUI 线程执行）。

        代次号对不上说明用户已经切到别的对比项，这份结果直接丢弃。
        """
        if generation != self._poster_gen:
            return
        # QPixmap 只能在 GUI 线程构造，所以转换放在这里而不是工作线程里。
        self._pixmaps[slot] = QPixmap.fromImage(image) if not image.isNull() else None
        self.label.set_pixmaps(self._pixmaps["before"], self._pixmaps["after"])

    def _set_mode(self, mode: str):
        self._mode = mode
        self.label.set_mode(mode)
        self.revealBtn.setChecked(mode == "reveal")
        self.sideBtn.setChecked(mode == "side")

    def _restyle(self):
        self.label._restyle()
