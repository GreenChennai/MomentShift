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

        # ---- 运行环境卡片（v0.3.6：上下结构 + 分隔线）----
        env_card = ThemedCard()
        env_vb = QVBoxLayout(env_card)
        env_vb.setContentsMargins(CARD_MARGIN, 16, CARD_MARGIN, 16)
        env_vb.setSpacing(14)

        env_title = StrongBodyLabel(tr("about.env.title"))
        env_vb.addWidget(env_title)
        env_vb.addSpacing(4)

        # === FFmpeg ===
        self._ff_section = self._build_env_section("FFmpeg", "")
        env_vb.addWidget(self._ff_section)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("QFrame{ color: #e8e8e8; }")
        env_vb.addWidget(sep)

        # === Real-ESRGAN ===
        self._re_section = self._build_env_section(tr("about.env.upscaler"), "")
        env_vb.addWidget(self._re_section)

        self.vbox.addWidget(env_card)
        self._refresh_env()
        self.vbox.addStretch(1)
        self.retheme()

    def _build_env_section(self, name: str, ok_text: str):
        """构建单条环境（v0.3.6：上下结构，按钮移入内部）。"""
        sec = QWidget()
        sv = QVBoxLayout(sec)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(6)

        # 第一行：名称 + 状态点
        row = QHBoxLayout()
        row.setSpacing(8)
        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet("border-radius: 4px;")
        row.addWidget(dot)
        name_lbl = StrongBodyLabel(name)
        row.addWidget(name_lbl, 1)
        status_lbl = CaptionLabel("")
        row.addWidget(status_lbl)
        sv.addLayout(row)

        # 链接 + 按钮行
        btns = QHBoxLayout()
        btns.setSpacing(8)
        link_btn = HyperlinkButton("", tr("about.env.download"))
        btns.addWidget(link_btn)
        btns.addStretch(1)
        action_btn = PrimaryPushButton("", icon=FIF.DOWNLOAD)
        action_btn.setFixedHeight(28)
        btns.addWidget(action_btn)
        sv.addLayout(btns)

        # 进度条
        prog = QProgressBar()
        prog.setRange(0, 0)
        prog.setFixedHeight(3)
        prog.setTextVisible(False)
        prog.setStyleSheet(
            "QProgressBar{border:none;background:transparent;border-radius:1px;}"
            "QProgressBar::chunk{background:#238636;border-radius:1px;}")
        prog.hide()
        sv.addWidget(prog)

        # 存储引用
        sec._dot = dot; sec._status = status_lbl; sec._link = link_btn
        sec._btn = action_btn; sec._prog = prog; sec._text = name_lbl
        return sec

    def _update_section(self, sec, ok, name, ok_msg, fail_msg, btn_text, link_url=""):
        if ok:
            sec._status.setText(ok_msg)
            sec._status.setStyleSheet(f"color:{success_color().name()};font-size:12px;")
            sec._dot.setStyleSheet(
                f"background:{success_color().name()};border-radius:4px;")
            sec._link.hide(); sec._btn.hide()
        else:
            sec._status.setText(fail_msg)
            sec._status.setStyleSheet(f"color:{danger_color().name()};font-size:12px;")
            sec._dot.setStyleSheet(
                f"background:{danger_color().name()};border-radius:4px;")
            sec._link.show()
            sec._btn.setText(btn_text); sec._btn.show()
        sec._text.setText(name)

    def _refresh_env(self):
        # FFmpeg
        ff = find_ffmpeg()
        self._update_section(
            self._ff_section, bool(ff), "FFmpeg",
            tr("ffmpeg.found", name="ffmpeg"),
            tr("about.env.missing"), tr("ffmpeg.download"))
        try: self._ff_section._btn.clicked.disconnect()
        except: pass
        self._ff_section._btn.clicked.connect(self._download_ffmpeg)

        # Real-ESRGAN
        ok = bool(upscaler.find_upscaler())
        n = len(upscaler.available_models()) if ok else 0
        self._update_section(
            self._re_section, ok, tr("about.env.upscaler"),
            tr("upscale.engine.ok", n=n),
            tr("upscale.engine.missing"), tr("upscale.engine.oneclick"))
        try: self._re_section._btn.clicked.disconnect()
        except: pass
        self._re_section._btn.clicked.connect(self._download_upscaler)

    def _download_ffmpeg(self):
        from ..core.ffmpeg_download import FfmpegDownloadWorker
        self._ff_section._btn.setEnabled(False); self._ff_section._prog.show()
        w = FfmpegDownloadWorker()
        w.signals.finished.connect(self._on_ff_done)
        QThreadPool.globalInstance().start(w)

    def _on_ff_done(self, ok, msg):
        self._ff_section._btn.setEnabled(True); self._ff_section._prog.hide()
        self._refresh_env()

    def _download_upscaler(self):
        self._re_section._btn.setEnabled(False); self._re_section._prog.show()
        w = upscaler.UpscalerDownloadWorker(str(upscaler.realesrgan_dir()))
        w.signals.finished.connect(self._on_re_done)
        QThreadPool.globalInstance().start(w)

    def _on_re_done(self, ok, msg):
        self._re_section._btn.setEnabled(True); self._re_section._prog.hide()
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
        self._ff_section._btn.setText(tr("ffmpeg.download"))
        self._re_section._btn.setText(tr("upscale.engine.oneclick"))
        self.techLabel.setText(tr("about.tech"))
        self.licenseLabel.setText(tr("about.license"))
        self.disclaimerLabel.setText(tr("about.disclaimer"))
        self._refresh_env()
