"""关于界面 — 应用信息 + 运行环境状态（v0.3.3 重构）。
将 ffmpeg 和放大引擎检测从原功能页面移至此。
"""

from PyQt6.QtWidgets import QVBoxLayout, QFrame, QHBoxLayout, QLabel, QProgressBar, QWidget

from qfluentwidgets import (
    FluentIcon as FIF, TitleLabel, BodyLabel, StrongBodyLabel,
    CaptionLabel, PushButton, PrimaryPushButton, HyperlinkButton,
)
from ..core.qt_compat import QDesktopServices, QUrl
from ..i18n.translator import tr
from ..metadata import APP_NAME, VERSION, AUTHOR, REPO_URL, RELEASE_URL
from .base import InterfaceBase
from .theme import (
    ThemedCard, accent_name, CARD_MARGIN, muted_text, panel,
    success_color, danger_color, accent_color, border_color,
)
from ..core.ffmpeg import find_ffmpeg
from ..core import upscaler
from ..core.qt_compat import Signal, QRunnable, QThreadPool

class AboutInterface(InterfaceBase):
    def __init__(self, parent=None):
        super().__init__("About", tr("about.title"), "", parent)

        # ---- 应用信息卡片 ----
        card = ThemedCard()
        cv = QVBoxLayout(card)
        cv.setContentsMargins(CARD_MARGIN, 20, CARD_MARGIN, 20)
        cv.setSpacing(10)

        self.nameLabel = TitleLabel(f"{APP_NAME}  ·  {tr('app.title')}")
        cv.addWidget(self.nameLabel)
        self.accentRule = QFrame()
        self.accentRule.setFrameShape(QFrame.Shape.HLine)
        self.accentRule.setFixedHeight(3)
        self.accentRule.setFixedWidth(40)
        self.accentRule.setStyleSheet(
            f"QFrame{{ background: {accent_name()}; border: none; border-radius: 2px; }}")
        cv.addWidget(self.accentRule)
        cv.addSpacing(6)

        self.tagLabel = BodyLabel(tr("about.description"))
        self.tagLabel.setWordWrap(True)
        self.verLabel = StrongBodyLabel(f"{tr('about.version')}: {VERSION}")
        self.authorLabel = BodyLabel(f"{tr('about.author')}: {AUTHOR}")
        cv.addWidget(self.tagLabel)
        cv.addWidget(self.verLabel)
        cv.addWidget(self.authorLabel)

        cv.addSpacing(8)
        self.repoBtn = PushButton(tr("about.repo"), icon=FIF.GITHUB)
        self.repoBtn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(REPO_URL)))
        self.updateBtn = PushButton(tr("about.check_update"), icon=FIF.UPDATE)
        self.updateBtn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(RELEASE_URL)))
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.repoBtn)
        row.addSpacing(10)
        row.addWidget(self.updateBtn)
        row.addStretch(1)
        cv.addLayout(row)

        cv.addSpacing(8)
        self.techLabel = CaptionLabel(tr("about.tech"))
        self.licenseLabel = CaptionLabel(tr("about.license"))
        self.disclaimerLabel = CaptionLabel(tr("about.disclaimer"))
        for lbl in (self.techLabel, self.licenseLabel, self.disclaimerLabel):
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color: {muted_text()};")
            cv.addWidget(lbl)
        self.vbox.addWidget(card)

        # ---- 运行环境卡片 ----
        env_card, env_vb = panel(tr("about.env.title"), tr("about.env.subtitle"))
        # FFmpeg
        self.ff_label = BodyLabel("")
        env_vb.addWidget(self._env_row("ffmpeg", self.ff_label))
        self.ff_link = HyperlinkButton("https://ffmpeg.org/download.html", tr("about.env.download"))
        self.ff_oneclick = PrimaryPushButton(tr("ffmpeg.download"), icon=FIF.DOWNLOAD)
        self.ff_oneclick.clicked.connect(self._download_ffmpeg)
        self.ff_prog = QProgressBar()
        self.ff_prog.setRange(0, 0)
        self.ff_prog.setFixedHeight(4)
        self.ff_prog.hide()
        ff_row = QHBoxLayout()
        ff_row.addWidget(self.ff_link)
        ff_row.addStretch()
        ff_row.addWidget(self.ff_oneclick)
        env_vb.addLayout(ff_row)
        env_vb.addWidget(self.ff_prog)

        # Real-ESRGAN
        self.re_label = BodyLabel("")
        env_vb.addWidget(self._env_row(tr("about.env.upscaler"), self.re_label))
        self.re_link = HyperlinkButton(upscaler.ENGINE_PAGE, tr("about.env.download"))
        self.re_oneclick = PrimaryPushButton(tr("upscale.engine.oneclick"), icon=FIF.DOWNLOAD)
        self.re_oneclick.clicked.connect(self._download_upscaler)
        self.re_prog = QProgressBar()
        self.re_prog.setRange(0, 0)
        self.re_prog.setFixedHeight(4)
        self.re_prog.hide()
        re_row = QHBoxLayout()
        re_row.addWidget(self.re_link)
        re_row.addStretch()
        re_row.addWidget(self.re_oneclick)
        env_vb.addLayout(re_row)
        env_vb.addWidget(self.re_prog)
        self.vbox.addWidget(env_card)

        self._refresh_env()
        self.vbox.addStretch(1)
        self.retheme()

    def _env_row(self, name, label):
        row = QWidget()
        hb = QHBoxLayout(row)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(8)
        dot = QLabel("●")
        dot.setFixedWidth(16)
        hb.addWidget(dot)
        hb.addWidget(label, 1)
        row._dot = dot
        return row

    def _refresh_env(self):
        # FFmpeg
        ff = find_ffmpeg()
        if ff:
            self.ff_label.setText(tr("ffmpeg.found", name="ffmpeg"))
            self.ff_label.setStyleSheet(f"color:{success_color().name()};")
            self.ff_label.parentWidget()._dot.setStyleSheet(f"color:{success_color().name()};")
            self.ff_link.hide(); self.ff_oneclick.hide(); self.ff_prog.hide()
        else:
            self.ff_label.setText(tr("about.env.missing"))
            self.ff_label.setStyleSheet(f"color:{danger_color().name()};")
            self.ff_label.parentWidget()._dot.setStyleSheet(f"color:{danger_color().name()};")
            self.ff_link.show(); self.ff_oneclick.show()

        # Upscaler
        if upscaler.find_upscaler():
            n = len(upscaler.available_models())
            self.re_label.setText(tr("upscale.engine.ok", n=n))
            self.re_label.setStyleSheet(f"color:{success_color().name()};")
            self.re_label.parentWidget()._dot.setStyleSheet(f"color:{success_color().name()};")
            self.re_link.hide(); self.re_oneclick.hide(); self.re_prog.hide()
        else:
            self.re_label.setText(tr("upscale.engine.missing"))
            self.re_label.setStyleSheet(f"color:{danger_color().name()};")
            self.re_label.parentWidget()._dot.setStyleSheet(f"color:{danger_color().name()};")
            self.re_link.show(); self.re_oneclick.show()

    def _download_ffmpeg(self):
        from ..core.ffmpeg_download import FfmpegDownloadWorker
        self.ff_oneclick.setEnabled(False); self.ff_prog.show()
        w = FfmpegDownloadWorker()
        w.signals.finished.connect(self._on_ff_done)
        QThreadPool.globalInstance().start(w)

    def _on_ff_done(self, ok, msg):
        self.ff_oneclick.setEnabled(True); self.ff_prog.hide()
        self._refresh_env()

    def _download_upscaler(self):
        self.re_oneclick.setEnabled(False); self.re_prog.show()
        w = upscaler.UpscalerDownloadWorker(str(upscaler.realesrgan_dir()))
        w.signals.finished.connect(self._on_re_done)
        QThreadPool.globalInstance().start(w)

    def _on_re_done(self, ok, msg):
        self.re_oneclick.setEnabled(True); self.re_prog.hide()
        self._refresh_env()

    def retheme(self):
        super().retheme()
        self.accentRule.setStyleSheet(
            f"QFrame{{ background: {accent_name()}; border: none; border-radius: 2px; }}")
        for lbl in (self.techLabel, self.licenseLabel, self.disclaimerLabel):
            lbl.setStyleSheet(f"color: {muted_text()};")

    def retranslateUi(self):
        self.retranslate(tr("about.title"))
        self.nameLabel.setText(f"{APP_NAME}  ·  {tr('app.title')}")
        self.tagLabel.setText(tr("about.description"))
        self.verLabel.setText(f"{tr('about.version')}: {VERSION}")
        self.authorLabel.setText(f"{tr('about.author')}: {AUTHOR}")
        self.repoBtn.setText(tr("about.repo"))
        self.techLabel.setText(tr("about.tech"))
        self.licenseLabel.setText(tr("about.license"))
        self.disclaimerLabel.setText(tr("about.disclaimer"))
        self.updateBtn.setText(tr("about.check_update"))
        self.ff_oneclick.setText(tr("ffmpeg.download"))
        self.re_oneclick.setText(tr("upscale.engine.oneclick"))
        self._refresh_env()
