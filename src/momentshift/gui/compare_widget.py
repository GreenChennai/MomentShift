"""Before/after compare widget for the Upscale screen."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from PyQt6.QtGui import QPixmap, QPainter, QPen, QBrush, QColor, QMovie
from PyQt6.QtCore import Qt, QRect
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QHBoxLayout

from qfluentwidgets import FluentIcon as FIF, PushButton, StrongBodyLabel, CaptionLabel, isDarkTheme

from ..core.ffmpeg import find_ffmpeg
from ..i18n.translator import tr
from .theme import ThemedCard, muted_text, accent_color, sub_text


_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".wmv", ".m4v", ".3gp", ".ts"}


def _poster(path: str | None) -> QPixmap:
    if not path or not Path(path).exists():
        return QPixmap()
    ext = Path(path).suffix.lower()
    if ext == ".gif":
        mv = QMovie(path)
        if mv.jumpToFrame(0):
            return mv.currentPixmap()
        return QPixmap()
    if ext in _VIDEO_EXTS:
        return _video_poster(path)
    return QPixmap(path)


def _video_poster(path: str) -> QPixmap:
    ff = find_ffmpeg()
    if not ff:
        return QPixmap()
    tmp = tempfile.mktemp(suffix=".png")
    try:
        subprocess.run(
            [ff, "-y", "-ss", "00:00:01", "-i", path, "-frames:v", "1", "-q:v", "2", tmp],
            capture_output=True, timeout=60,
        )
        return QPixmap(tmp)
    except Exception:
        return QPixmap()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


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
        return pm.scaled(rect.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                         Qt.TransformationMode.SmoothTransformation)

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
                self._draw(painter, self._after,
                            QRect(rect.left() + half, rect.top(), rect.width() - half, rect.height()))
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
    def __init__(self, parent=None):
        super().__init__(parent)
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
        self.label.set_pixmaps(_poster(before), _poster(after))
        has = bool(before or after)
        self.label.setVisible(has)
        # The former "select a queue item to compare" hint has been removed per
        # design; the compare area simply stays empty until a result exists.
        self.emptyHint.setVisible(False)

    def _set_mode(self, mode: str):
        self._mode = mode
        self.label.set_mode(mode)
        self.revealBtn.setChecked(mode == "reveal")
        self.sideBtn.setChecked(mode == "side")

    def _restyle(self):
        self.label._restyle()
