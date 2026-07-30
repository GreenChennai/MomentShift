"""关于界面 — 应用信息 + 运行环境状态（v0.3.4 美化）。

运行环境卡片使用优雅的状态指示器布局。
"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtWidgets import (
    QVBoxLayout, QFrame, QHBoxLayout, QLabel, QProgressBar, QWidget, QSizePolicy,
)
from PyQt6.QtGui import QFont

from qfluentwidgets import (
    FluentIcon as FIF, TitleLabel, BodyLabel, StrongBodyLabel,
    CaptionLabel, PushButton, PrimaryPushButton, HyperlinkButton,
)
from ..core.qt_compat import QDesktopServices, QUrl
from ..i18n.translator import tr
from ..metadata import APP_NAME, VERSION, AUTHOR, REPO_URL, RELEASE_URL
from .base import InterfaceBase
from .theme import (
    ThemedCard, accent_name, CARD_MARGIN, muted_text, ACCENT_HEX,
    success_color, danger_color
)
from ..core.ffmpeg import find_ffmpeg
from ..core import upscaler
from ..core.qt_compat import Signal, QRunnable, QThreadPool

# 环境状态行 CSS（共用）
_ENV_ROW_NORMAL = (
    "QWidget#envRow{{"
    "  background: #fafafa; border: 1px solid #eee; border-radius: 10px;"
    "  padding: 2px;"
    "}}"
    "QWidget#envRow:hover{{ background: #f0f7f0; border-color: #c5e4c5; }}"
)

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
        row.addStretch(1); row.addWidget(self.repoBtn); row.addSpacing(10)
        row.addWidget(self.updateBtn); row.addStretch(1)
        cv.addLayout(row)

        cv.addSpacing(8)
        self.techLabel = CaptionLabel(tr("about.tech"))
        self.techLabel.setWordWrap(True)
        self.techLabel.setStyleSheet(f"color: {muted_text()};")
        cv.addWidget(self.techLabel)
        self.licenseLabel = CaptionLabel(tr("about.license"))
        self.licenseLabel.setWordWrap(True)
        self.licenseLabel.setStyleSheet(f"color: {muted_text()};")
        cv.addWidget(self.licenseLabel)
        self.disclaimerLabel = CaptionLabel(tr("about.disclaimer"))
        self.disclaimerLabel.setWordWrap(True)
        self.disclaimerLabel.setStyleSheet(f"color: {muted_text()};")
        cv.addWidget(self.disclaimerLabel)
        self.vbox.addWidget(card)

        # ---- 运行环境卡片（v0.3.4 美化）----
        env_card = ThemedCard()
        env_vb = QVBoxLayout(env_card)
        env_vb.setContentsMargins(CARD_MARGIN, 16, CARD_MARGIN, 16)
        env_vb.setSpacing(0)

        env_title = StrongBodyLabel(tr("about.env.title"))
        env_vb.addWidget(env_title)
        env_sub = CaptionLabel(tr("about.env.subtitle"))
        env_sub.setStyleSheet(f"color: {muted_text()};")
        env_vb.addWidget(env_sub)
        env_vb.addSpacing(14)

        # FFmpeg 行（v0.3.5：去图标）
        self._ff_row, _, self._ff_text, self._ff_status, \
            self._ff_link, self._ff_btn, self._ff_prog = \
            self._build_env_row(FIF.VIDEO, "FFmpeg")
        env_vb.addWidget(self._ff_row)
        env_vb.addWidget(self._ff_prog)

        env_vb.addSpacing(8)

        # Real-ESRGAN 行（v0.3.5：去图标）
        self._re_row, _, self._re_text, self._re_status, \
            self._re_link, self._re_btn, self._re_prog = \
            self._build_env_row(FIF.ZOOM, tr("about.env.upscaler"))
        env_vb.addWidget(self._re_row)
        env_vb.addWidget(self._re_prog)

        # 连线
        self._ff_btn.clicked.connect(self._download_ffmpeg)
        self._re_btn.clicked.connect(self._download_upscaler)

        self.vbox.addWidget(env_card)
        self._refresh_env()
        self.vbox.addStretch(1)
        self.retheme()

    def _build_env_row(self, icon, name):
        """构建单行环境状态（v0.3.5：无图标，全宽）。"""
        row = QWidget()
        row.setObjectName("envRow")
        row.setStyleSheet(_ENV_ROW_NORMAL)
        hb = QHBoxLayout(row)
        hb.setContentsMargins(14, 12, 14, 12)
        hb.setSpacing(10)

        # 名称 + 状态
        inner = QVBoxLayout()
        inner.setSpacing(2)
        text_lbl = StrongBodyLabel(name)
        status_lbl = CaptionLabel("")
        inner.addWidget(text_lbl)
        inner.addWidget(status_lbl)
        hb.addLayout(inner, 1)

        # 下载链接
        link_btn = HyperlinkButton("", tr("about.env.download"))
        link_btn.hide()
        hb.addWidget(link_btn)

        # 一键下载按钮
        action_btn = PrimaryPushButton("", icon=FIF.DOWNLOAD)
        action_btn.setFixedHeight(30)
        action_btn.hide()
        hb.addWidget(action_btn)

        # 进度条
        prog = QProgressBar()
        prog.setRange(0, 0)
        prog.setFixedHeight(3)
        prog.setTextVisible(False)
        prog.setStyleSheet(
            "QProgressBar{ border: none; background: transparent; border-radius: 1px; }"
            "QProgressBar::chunk{ background: #238636; border-radius: 1px; }")
        prog.hide()

        return row, None, text_lbl, status_lbl, link_btn, action_btn, prog

    def _refresh_env(self):
        # FFmpeg
        ff = find_ffmpeg()
        if ff:
            self._ff_text.setText("FFmpeg")
            self._ff_status.setText(tr("ffmpeg.found", name="ffmpeg"))
            self._ff_status.setStyleSheet(f"color: {success_color().name()};")
            self._ff_link.hide(); self._ff_btn.hide()
            self._ff_row.setStyleSheet(_ENV_ROW_NORMAL)
        else:
            self._ff_text.setText("FFmpeg")
            self._ff_status.setText(tr("about.env.missing"))
            self._ff_status.setStyleSheet(f"color: {danger_color().name()};")
            self._ff_link.show(); self._ff_btn.setText(tr("ffmpeg.download")); self._ff_btn.show()
            self._ff_row.setStyleSheet(
                _ENV_ROW_NORMAL.replace("#fafafa", "#fff5f5")
                .replace("#eee", "#fdd").replace("#f0f7f0", "#fff0f0")
                .replace("#c5e4c5", "#fcc"))

        # Upscaler
        if upscaler.find_upscaler():
            n = len(upscaler.available_models())
            self._re_text.setText(tr("about.env.upscaler"))
            self._re_status.setText(tr("upscale.engine.ok", n=n))
            self._re_status.setStyleSheet(f"color: {success_color().name()};")
            self._re_link.hide(); self._re_btn.hide()
            self._re_row.setStyleSheet(_ENV_ROW_NORMAL)
        else:
            self._re_text.setText(tr("about.env.upscaler"))
            self._re_status.setText(tr("upscale.engine.missing"))
            self._re_status.setStyleSheet(f"color: {danger_color().name()};")
            self._re_link.show(); self._re_btn.setText(tr("upscale.engine.oneclick")); self._re_btn.show()
            self._re_row.setStyleSheet(
                _ENV_ROW_NORMAL.replace("#fafafa", "#fff5f5")
                .replace("#eee", "#fdd").replace("#f0f7f0", "#fff0f0")
                .replace("#c5e4c5", "#fcc"))

    def _download_ffmpeg(self):
        from ..core.ffmpeg_download import FfmpegDownloadWorker
        self._ff_btn.setEnabled(False); self._ff_prog.show()
        w = FfmpegDownloadWorker()
        w.signals.finished.connect(self._on_ff_done)
        QThreadPool.globalInstance().start(w)

    def _on_ff_done(self, ok, msg):
        self._ff_btn.setEnabled(True); self._ff_prog.hide()
        self._refresh_env()

    def _download_upscaler(self):
        self._re_btn.setEnabled(False); self._re_prog.show()
        w = upscaler.UpscalerDownloadWorker(str(upscaler.realesrgan_dir()))
        w.signals.finished.connect(self._on_re_done)
        QThreadPool.globalInstance().start(w)

    def _on_re_done(self, ok, msg):
        self._re_btn.setEnabled(True); self._re_prog.hide()
        self._refresh_env()

    def retheme(self):
        super().retheme()
        self.accentRule.setStyleSheet(
            f"QFrame{{ background: {accent_name()}; border: none; border-radius: 2px; }}")

    def retranslateUi(self):
        self.retranslate(tr("about.title"))
        self.nameLabel.setText(f"{APP_NAME}  ·  {tr('app.title')}")
        self.tagLabel.setText(tr("about.description"))
        self.verLabel.setText(f"{tr('about.version')}: {VERSION}")
        self.authorLabel.setText(f"{tr('about.author')}: {AUTHOR}")
        self.repoBtn.setText(tr("about.repo"))
        self.updateBtn.setText(tr("about.check_update"))
        self._ff_btn.setText(tr("ffmpeg.download"))
        self._re_btn.setText(tr("upscale.engine.oneclick"))
        self.techLabel.setText(tr("about.tech"))
        self.licenseLabel.setText(tr("about.license"))
        self.disclaimerLabel.setText(tr("about.disclaimer"))
        self._refresh_env()
