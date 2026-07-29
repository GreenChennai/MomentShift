"""Before / after comparison widget for the upscaling module.

Two comparison modes, toggled by the caller:
  * ``reveal``  — a draggable vertical handle wipes between the *before* image
    (underneath) and the *after* image (clipped on top). This is the signature
    "drag to compare" interaction.
  * ``side``    — before on the left, after on the right, each scaled to fit.

Both images are loaded as pixmaps. For GIF / video the *poster* (first frame) is
shown so the comparison stays lightweight and always renderable; the original
animated file is still what gets processed / saved.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PyQt6.QtCore import Qt, QRect, QPoint, QSize, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter, QColor, QMovie, QPen, QImage
from ..core.qt_compat import QWidget, QVBoxLayout, QHBoxLayout, QLabel, Signal

from qfluentwidgets import PushButton, CaptionLabel, FluentIcon as FIF, isDarkTheme
from ..i18n.translator import tr
from .theme import ThemedCard, sub_text, muted_text


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
ANIM_EXTS = {".gif"}


def _poster(path: str) -> QPixmap | None:
    """Return a pixmap for the file: image directly, GIF first frame, or a
    video poster frame extracted via ffmpeg (best-effort)."""
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        pix = QPixmap(path)
        return pix if not pix.isNull() else None
    if ext in ANIM_EXTS:
        movie = QMovie(path)
        if movie.jumpToFrame(0):
            return movie.currentPixmap()
        return None
    # video -> extract poster frame via ffmpeg
    try:
        from ..core.ffmpeg import find_ffmpeg

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return None
        tmp = tempfile.mkdtemp(prefix="ms_poster_")
        out = os.path.join(tmp, "poster.png")
        import subprocess

        subprocess.run(
            [ffmpeg, "-y", "-i", path, "-vframes", "1", out],
            capture_output=True, timeout=60,
        )
        pix = QPixmap(out)
        return pix if not pix.isNull() else None
    except Exception:
        return None


class _RevealLabel(QLabel):
    """Stacked before/after with a draggable reveal handle."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._before: QPixmap | None = None
        self._after: QPixmap | None = None
        self._ratio = 0.5
        self._drag = False
        self._handle_color = QColor(255, 255, 255)
        self.setCursor(Qt.CursorShape.SplitHCursor)
        self.setMinimumHeight(160)

    def set_pixmaps(self, before: QPixmap | None, after: QPixmap | None):
        self._before = before
        self._after = after
        self._ratio = 0.5
        self.update()

    def _scaled(self, pix: QPixmap | None) -> QPixmap | None:
        if not pix or pix.isNull():
            return None
        return pix.scaled(
            self.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _blended(self, target: QSize) -> QPixmap:
        """Compose the two images onto a transparent canvas at ``target`` size."""
        canvas = QPixmap(target)
        canvas.fill(Qt.GlobalColor.transparent)
        p = QPainter(canvas)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._before:
            p.drawPixmap(QRect(QPoint(0, 0), target), self._before,
                         QRect(QPoint(0, 0), self._before.size()))
        if self._after:
            # clip the after image to the left of the divider
            clip = QRect(0, 0, int(target.width() * self._ratio), target.height())
            p.setClipRect(clip)
            p.drawPixmap(QRect(QPoint(0, 0), target), self._after,
                         QRect(QPoint(0, 0), self._after.size()))
        p.setClipping(False)
        # handle line
        x = int(target.width() * self._ratio)
        pen = QPen(self._handle_color, 2)
        p.setPen(pen)
        p.drawLine(x, 0, x, target.height())
        p.end()
        return canvas

    def paintEvent(self, event):
        if not self._before and not self._after:
            super().paintEvent(event)
            return
        target = self._scaled(self._after) or self._scaled(self._before)
        if not target:
            return
        canvas = self._blended(QSize(target.width(), target.height()))
        p = QPainter(self)
        # center the composed image
        x = (self.width() - canvas.width()) // 2
        y = (self.height() - canvas.height()) // 2
        p.drawPixmap(x, y, canvas)
        p.end()

    def mousePressEvent(self, event):
        self._drag = True
        self._update_ratio(event.position().x())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag:
            self._update_ratio(event.position().x())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag = False
        super().mouseReleaseEvent(event)

    def _update_ratio(self, x: float):
        if self.width() <= 0:
            return
        self._ratio = max(0.0, min(1.0, x / self.width()))
        self.update()


class CompareWidget(ThemedCard):
    """A self-contained before/after comparison card."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._before_path: str | None = None
        self._after_path: str | None = None
        self._mode = "reveal"
        self._movie: QMovie | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # mode toggle
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        self.revealBtn = PushButton(tr("upscale.compare.reveal"))
        self.sideBtn = PushButton(tr("upscale.compare.side"))
        self.revealBtn.setCheckable(True)
        self.sideBtn.setCheckable(True)
        self.revealBtn.setChecked(True)
        self.revealBtn.clicked.connect(lambda: self._set_mode("reveal"))
        self.sideBtn.clicked.connect(lambda: self._set_mode("side"))
        bar.addWidget(self.revealBtn)
        bar.addWidget(self.sideBtn)
        bar.addStretch(1)
        root.addLayout(bar)

        # reveal surface
        self.reveal = _RevealLabel()
        root.addWidget(self.reveal, 1)

        # side-by-side surface
        self.side = QWidget()
        sl = QHBoxLayout(self.side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(8)
        self.beforeLbl = QLabel()
        self.beforeLbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.afterLbl = QLabel()
        self.afterLbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sl.addWidget(self.beforeLbl, 1)
        sl.addWidget(self.afterLbl, 1)
        self.side.setVisible(False)
        root.addWidget(self.side, 1)

        self.emptyLabel = CaptionLabel(tr("upscale.compare.empty"))
        self.emptyLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyLabel.setObjectName("queueEmpty")
        root.addWidget(self.emptyLabel)

    # -- public API --------------------------------------------------------
    def set_paths(self, before: str | None, after: str | None):
        self._before_path = before
        self._after_path = after
        self._movie = None
        if before and after:
            self.emptyLabel.hide()
            self._refresh()
        else:
            self.emptyLabel.show()
            self.reveal.set_pixmaps(None, None)
            self.beforeLbl.clear()
            self.afterLbl.clear()

    def _refresh(self):
        before = _poster(self._before_path) if self._before_path else None
        after = _poster(self._after_path) if self._after_path else None

        self.reveal.set_pixmaps(before, after)

        # side-by-side (supports animated GIF via QMovie on the after label)
        if self._after_path and Path(self._after_path).suffix.lower() in ANIM_EXTS:
            self._movie = QMovie(self._after_path)
            self.afterLbl.setMovie(self._movie)
            self._movie.start()
        else:
            self.afterLbl.setMovie(None)
            if after:
                self.afterLbl.setPixmap(after)
        if before:
            self.beforeLbl.setPixmap(before)
        self._restyle()

    def _set_mode(self, mode: str):
        self._mode = mode
        self.revealBtn.setChecked(mode == "reveal")
        self.sideBtn.setChecked(mode == "side")
        self.reveal.setVisible(mode == "reveal")
        self.side.setVisible(mode == "side")
        if mode == "side" and self._after_path:
            self._refresh()

    def _restyle(self):
        fg = "rgba(255,255,255,0.9)" if isDarkTheme() else "rgba(40,40,40,0.9)"
        self.reveal._handle_color = QColor(fg)
        self.reveal.update()
