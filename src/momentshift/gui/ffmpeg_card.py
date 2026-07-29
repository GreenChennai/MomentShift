"""ffmpeg status + one-click download card (Convert screen)."""

from __future__ import annotations

from pathlib import Path
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QProgressBar, QLabel

from qfluentwidgets import FluentIcon as FIF, StrongBodyLabel, BodyLabel, PrimaryPushButton, HyperlinkButton

from ..core.qt_compat import Signal, QThreadPool
from ..core.ffmpeg import find_ffmpeg, ffmpeg_install_dir, get_version
from ..core.ffmpeg_download import FfmpegDownloadWorker
from ..i18n.translator import tr
from .theme import ThemedCard, muted_text, sub_text


class FfmpegCard(ThemedCard):
    ffmpeg_ready = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        vb = QVBoxLayout(self)
        vb.setContentsMargins(16, 14, 16, 14)
        vb.setSpacing(10)

        self.titleLbl = StrongBodyLabel(tr("ffmpeg.title"))
        vb.addWidget(self.titleLbl)

        top = QHBoxLayout()
        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        top.addWidget(self.dot)
        self.statusLbl = BodyLabel()
        top.addWidget(self.statusLbl, 1)
        vb.addLayout(top)

        self.linkBtn = HyperlinkButton("https://ffmpeg.org/download.html",
                                       tr("ffmpeg.download.page"))
        self.dlBtn = PrimaryPushButton(tr("ffmpeg.download"), icon=FIF.DOWNLOAD)
        self.dlBtn.clicked.connect(self._download)
        row = QHBoxLayout()
        row.addWidget(self.linkBtn)
        row.addStretch(1)
        row.addWidget(self.dlBtn)
        vb.addLayout(row)

        self.prog = QProgressBar()
        self.prog.setRange(0, 0)
        self.prog.setFixedHeight(4)
        self.prog.setStyleSheet("QProgressBar{background:#dcdcdc; border:none; border-radius:2px;} "
                                "QProgressBar::chunk{background:#0f6cbd; border-radius:2px;}")
        self.prog.hide()
        vb.addWidget(self.prog)

        self._refresh()

    def _refresh(self):
        path = find_ffmpeg()
        if path:
            ver = get_version(path)
            self.statusLbl.setText(
                tr("ffmpeg.found", name=Path(path).name)
                + (f"  ·  {ver.split()[0]}" if ver else "")
            )
            self.statusLbl.setStyleSheet("color:#10893e;")
            self.dot.setStyleSheet("background:#10893e; border-radius:5px;")
            # Collapse download / link buttons when ffmpeg is ready
            self.linkBtn.hide()
            self.dlBtn.hide()
            self.prog.hide()
        else:
            self.statusLbl.setText(tr("ffmpeg.missing"))
            self.statusLbl.setStyleSheet(f"color:{sub_text()};")
            self.dot.setStyleSheet("background:#e81123; border-radius:5px;")
            self.linkBtn.show()
            self.dlBtn.show()

    def _download(self):
        self.dlBtn.setEnabled(False)
        self.prog.show()
        worker = FfmpegDownloadWorker(str(ffmpeg_install_dir()))
        worker.signals.finished.connect(self._on_finished)
        QThreadPool.globalInstance().start(worker)

    def _on_finished(self, ok: bool, msg: str):
        self.prog.hide()
        self.dlBtn.setEnabled(True)
        self._refresh()
        if ok:
            self.ffmpeg_ready.emit()

    def retranslateUi(self):
        self.titleLbl.setText(tr("ffmpeg.title"))
        self.linkBtn.setText(tr("ffmpeg.download.page"))
        self.dlBtn.setText(tr("ffmpeg.download"))
        self._refresh()
