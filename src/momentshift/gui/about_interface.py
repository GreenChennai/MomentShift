"""关于界面 —— 应用信息 + 运行环境状态。

职责边界：
- 做：展示版本与作者信息、汇总 ffmpeg 与各引擎的就绪状态、提供下载入口。
- 不做：不实现下载逻辑（交给 core/ffmpeg_download 与 gui/engine_card）。

依赖：core/ffmpeg、core/ffmpeg_download、core/logger、core/qt_compat、gui/base、gui/engine_card、gui/theme、i18n/translator、metadata；被依赖：主窗口按导航页装载。

运行环境卡片使用优雅的状态指示器布局。
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    HyperlinkButton,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    TitleLabel,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from ..core.ffmpeg import find_ffmpeg
from ..core.logger import get_logger
from ..core.qt_compat import QDesktopServices, QThreadPool, QUrl
from ..i18n.translator import tr
from ..metadata import APP_NAME, AUTHOR, RELEASE_URL, REPO_URL, VERSION
from . import tokens
from .base import InterfaceBase
from .theme import (
    CARD_MARGIN,
    ThemedCard,
    accent_name,
    app_logo_pixmap,
    danger_color,
    muted_text,
    success_color,
)

log = get_logger("about")

# 环境状态行 CSS（共用）


class AboutInterface(InterfaceBase):
    def __init__(self, parent=None):
        super().__init__("About", tr("about.title"), "", parent)

        # ---- 应用信息卡片 ----
        card = ThemedCard()
        cv = QVBoxLayout(card)
        cv.setContentsMargins(CARD_MARGIN, 20, CARD_MARGIN, 20)
        cv.setSpacing(10)

        # v0.8.14：卡片头部改为「品牌 Logo + 应用名」同行，Logo 与名字左对齐同基线
        self.logoLabel = QLabel()
        self.logoLabel.setFixedSize(58, 58)
        self.logoLabel.setStyleSheet("background-color: transparent;")
        self.logoLabel.setPixmap(app_logo_pixmap(58))
        self.logoLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.nameLabel = TitleLabel(f"{APP_NAME}  ·  {tr('app.title')}")
        self.accentRule = QFrame()
        self.accentRule.setFrameShape(QFrame.Shape.HLine)
        self.accentRule.setFixedHeight(3)
        self.accentRule.setFixedWidth(40)
        self.accentRule.setStyleSheet(
            f"QFrame{{ background: {accent_name()}; border: none; border-radius: 2px; }}"
        )

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(14)
        head.addWidget(self.logoLabel, 0, Qt.AlignmentFlag.AlignVCenter)
        head_text = QVBoxLayout()
        head_text.setContentsMargins(0, 0, 0, 0)
        head_text.setSpacing(8)
        head_text.addWidget(self.nameLabel)
        head_text.addWidget(self.accentRule)
        head.addLayout(head_text, 1)
        cv.addLayout(head)
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
        # 三行脚注（技术栈 / 许可证 / 免责声明）外观完全一致，
        # 合并成一次建样式 + 一轮装配，替代原来三处重复的 setStyleSheet
        self.techLabel = CaptionLabel(tr("about.tech"))
        self.licenseLabel = CaptionLabel(tr("about.license"))
        self.disclaimerLabel = CaptionLabel(tr("about.disclaimer"))
        footnote_qss = tokens.text_qss(muted_text())
        for lbl in (self.techLabel, self.licenseLabel, self.disclaimerLabel):
            lbl.setWordWrap(True)
            lbl.setStyleSheet(footnote_qss)
            cv.addWidget(lbl)
        self.vbox.addWidget(card)

        # ---- 运行环境卡片（：FFmpeg 独占一卡）----
        env_card = ThemedCard()
        env_vb = QVBoxLayout(env_card)
        env_vb.setContentsMargins(CARD_MARGIN, 16, CARD_MARGIN, 16)
        env_vb.setSpacing(14)

        self.envTitle = StrongBodyLabel(tr("about.env.title"))
        env_vb.addWidget(self.envTitle)
        env_vb.addSpacing(4)

        # === FFmpeg ===
        self._ff_section = self._build_env_section("FFmpeg", "")
        env_vb.addWidget(self._ff_section)

        self.vbox.addWidget(env_card)

        # ---- 超分辨率 / 插帧引擎卡片（ 新增，与 FFmpeg 分开）----
        from .engine_card import EnginesCard

        self.enginesCard = EnginesCard(self, on_changed=self._notify_engines_changed)
        self.vbox.addWidget(self.enginesCard)

        self._refresh_env()
        self.vbox.addStretch(1)
        self.retheme()

    def _notify_engines_changed(self):
        """引擎安装状态变化 → 通知「放大」界面重建设置面板。"""
        win = self.window()
        up = getattr(win, "upscaleInterface", None)
        if up is not None and hasattr(up, "reload_engines"):
            up.reload_engines()

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
            f"QProgressBar::chunk{{background:{tokens.ACCENT};border-radius:1px;}}"
        )
        prog.hide()
        sv.addWidget(prog)

        # 存储引用
        sec._dot = dot
        sec._status = status_lbl
        sec._link = link_btn
        sec._btn = action_btn
        sec._prog = prog
        sec._text = name_lbl
        return sec

    def _update_section(self, sec, ok, name, ok_msg, fail_msg, btn_text, link_url=""):
        """按检测结果刷新一条环境行的文案、配色与按钮可见性。

        Notes:
            两个分支原本各自给状态文字与状态点上色（共 4 处 setStyleSheet），
            现在只在分支里定颜色，出了分支统一应用，收敛成 2 处。
        """
        if ok:
            sec._status.setText(ok_msg)
            color = success_color().name()
            sec._link.hide()
            sec._btn.hide()
        else:
            sec._status.setText(fail_msg)
            color = danger_color().name()
            sec._link.show()
            sec._btn.setText(btn_text)
            sec._btn.show()
        sec._status.setStyleSheet(f"color:{color};font-size:{tokens.FONT_SMALL}px;")
        sec._dot.setStyleSheet(tokens.dot_qss(color, 4))
        sec._text.setText(name)

    def _refresh_env(self):
        # FFmpeg
        ff = find_ffmpeg()
        self._update_section(
            self._ff_section,
            bool(ff),
            "FFmpeg",
            tr("ffmpeg.found", name="ffmpeg"),
            tr("about.env.missing"),
            tr("ffmpeg.download"),
        )
        try:
            self._ff_section._btn.clicked.disconnect()
        except (TypeError, RuntimeError):  # 静默原因：按钮尚未连接任何槽时 disconnect 会抛错
            pass
        self._ff_section._btn.clicked.connect(self._download_ffmpeg)

        # Real-ESRGAN 已并入下方「超分辨率 / 插帧引擎」卡片
        if getattr(self, "enginesCard", None) is not None:
            self.enginesCard.rescan()

    def _download_ffmpeg(self):
        from ..core.ffmpeg_download import FfmpegDownloadWorker

        self._ff_section._btn.setEnabled(False)
        self._ff_section._prog.show()
        w = FfmpegDownloadWorker()
        w.signals.finished.connect(self._on_ff_done)
        QThreadPool.globalInstance().start(w)

    def _on_ff_done(self, ok, msg):
        self._ff_section._btn.setEnabled(True)
        self._ff_section._prog.hide()
        self._refresh_env()

    def retheme(self):
        super().retheme()
        self.accentRule.setStyleSheet(
            f"QFrame{{ background: {accent_name()}; border: none; border-radius: 2px; }}"
        )

    def retranslateUi(self):
        self.retranslate(tr("about.title"))
        self.nameLabel.setText(f"{APP_NAME}  ·  {tr('app.title')}")
        self.tagLabel.setText(tr("about.description"))
        self.verLabel.setText(f"{tr('about.version')}: {VERSION}")
        self.authorLabel.setText(f"{tr('about.author')}: {AUTHOR}")
        self.repoBtn.setText(tr("about.repo"))
        self.updateBtn.setText(tr("about.check_update"))
        self.envTitle.setText(tr("about.env.title"))
        self._ff_section._btn.setText(tr("ffmpeg.download"))
        if getattr(self, "enginesCard", None) is not None:
            self.enginesCard.retranslateUi()
        self.techLabel.setText(tr("about.tech"))
        self.licenseLabel.setText(tr("about.license"))
        self.disclaimerLabel.setText(tr("about.disclaimer"))
        self._refresh_env()
