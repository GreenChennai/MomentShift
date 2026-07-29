"""FFmpeg status / acquisition card shown on the Convert (start) screen.

MomentShift no longer bundles ffmpeg (to keep the installer small), so this
card tells the user whether ffmpeg is present and offers either a link to the
official download page or a one-click download that drops ffmpeg next to the
executable (the install root).
"""

from ..core.qt_compat import QHBoxLayout, QVBoxLayout, Signal, QDesktopServices, QUrl, QThreadPool
from qfluentwidgets import (
    FluentIcon as FIF,
    StrongBodyLabel,
    CaptionLabel,
    PushButton,
    HyperlinkButton,
    ProgressBar,
    InfoBar,
    InfoBarPosition,
)
from ..core.ffmpeg import find_ffmpeg, ffmpeg_install_dir
from ..core.ffmpeg_download import FfmpegDownloadWorker
from ..i18n.translator import tr
from .theme import ThemedCard

FFMPEG_DOWNLOAD_PAGE = "https://ffmpeg.org/download.html"


class FfmpegCard(ThemedCard):
    """Compact banner: ffmpeg status + link / one-click download."""

    ffmpeg_ready = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._building = True
        self._init_ui()
        self._refresh()
        self._building = False

    def _init_ui(self):
        # Portrait-friendly vertical layout: buttons stacked below the text so
        # the card fits the 400px window width without triggering a horizontal
        # scrollbar.
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.iconLabel = StrongBodyLabel("!")
        self.iconLabel.setFixedWidth(22)
        self.titleLabel = StrongBodyLabel(tr("ffmpeg.missing"))
        top.addWidget(self.iconLabel)
        top.addWidget(self.titleLabel, 1)
        root.addLayout(top)

        self.hintLabel = CaptionLabel(tr("ffmpeg.hint"))
        self.hintLabel.setWordWrap(True)
        root.addWidget(self.hintLabel)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.linkBtn = HyperlinkButton(FFMPEG_DOWNLOAD_PAGE, tr("ffmpeg.open_site"), self)
        self.downloadBtn = PushButton(tr("ffmpeg.oneclick"), icon=FIF.DOWNLOAD)
        self.downloadBtn.clicked.connect(self._on_download)
        btn_row.addWidget(self.linkBtn)
        btn_row.addWidget(self.downloadBtn)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        self.progress = ProgressBar()
        self.progress.setRange(0, 0)  # indeterminate "busy" bar
        self.progress.hide()
        root.addWidget(self.progress)

    def _refresh(self):
        found = find_ffmpeg()
        if found:
            self.iconLabel.setText("✓")
            self.titleLabel.setText(tr("ffmpeg.ok"))
            self.hintLabel.setText(found)
            self.downloadBtn.hide()
            self.linkBtn.hide()
            self.progress.hide()
        else:
            self.iconLabel.setText("!")
            self.titleLabel.setText(tr("ffmpeg.missing"))
            self.hintLabel.setText(tr("ffmpeg.hint"))
            self.downloadBtn.show()
            self.linkBtn.show()
            self.progress.hide()

    def _on_download(self):
        self.downloadBtn.setEnabled(False)
        self.linkBtn.setEnabled(False)
        self.progress.show()
        self.titleLabel.setText(tr("ffmpeg.downloading"))
        worker = FfmpegDownloadWorker(str(ffmpeg_install_dir()))
        worker.signals.started.connect(lambda: self.titleLabel.setText(tr("ffmpeg.downloading")))
        worker.signals.finished.connect(self._on_done)
        QThreadPool.globalInstance().start(worker)

    def _on_done(self, ok: bool, msg: str):
        self.progress.hide()
        self.downloadBtn.setEnabled(True)
        self.linkBtn.setEnabled(True)
        if ok:
            InfoBar.success(
                tr("ffmpeg.done"), "", parent=self.window(),
                duration=2500, position=InfoBarPosition.TOP_RIGHT,
            )
            self._refresh()
            self.ffmpeg_ready.emit()
        else:
            InfoBar.error(
                tr("ffmpeg.failed"), msg or "", parent=self.window(),
                duration=4000, position=InfoBarPosition.TOP_RIGHT,
            )
            self._refresh()

    def retranslateUi(self):
        self.linkBtn.setText(tr("ffmpeg.open_site"))
        self.downloadBtn.setText(tr("ffmpeg.oneclick"))
        if not self._building:
            self._refresh()
