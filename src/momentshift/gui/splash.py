"""启动屏（Logo + 应用名 + UI 加载进度条）。

职责边界：
- 做：在主窗口空框架之上盖一层遮罩，展示品牌 Logo、应用名与「界面加载进度」，
  等所有功能页构建完毕后由 :class:`~momentshift.gui.main_window.MainWindow` 关闭。
- 不做：不感知任何业务，不决定加载顺序；进度值完全由调用方推送。

依赖：gui/theme（品牌图标与背景色）、i18n/translator；被依赖：gui/main_window。

为什么子类化 qfluentwidgets 的 ``SplashScreen`` 而不是自己写 QWidget：
它已经处理好了「跟随父窗口 resize」「新子控件插入时自动 raise_」「无边框标题栏」
这三件麻烦事（见其 ``eventFilter``），自己重写等于把这些坑再踩一遍。这里只替换
它的视觉内容：隐藏内置 iconWidget，改成自己的居中面板。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ProgressBar, SplashScreen, TitleLabel

from ..i18n.translator import tr
from . import theme

LOGO_SIZE = 132
BAR_WIDTH = 248


class AppSplashScreen(SplashScreen):
    """品牌启动屏：Logo 居中，下方为应用名、加载提示与进度条。"""

    def __init__(self, parent=None):
        super().__init__(theme.app_icon(), parent=parent, enableShadow=False)
        # 基类的 iconWidget 是「只有一个图标」的极简样式，这里换成自建面板。
        self.iconWidget.hide()

        self._panel = QWidget(self)
        self._panel.setObjectName("splashPanel")
        # 裸 background-color 会级联到所有子控件（v0.2.x 血泪），用 objectName 限定。
        self._panel.setStyleSheet("#splashPanel{background-color: transparent;}")

        vb = QVBoxLayout(self._panel)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(0)

        self.logoLabel = QLabel(self._panel)
        self.logoLabel.setFixedSize(LOGO_SIZE, LOGO_SIZE)
        self.logoLabel.setStyleSheet("background-color: transparent;")
        self.logoLabel.setPixmap(theme.app_logo_pixmap(LOGO_SIZE))
        self.logoLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vb.addWidget(self.logoLabel, 0, Qt.AlignmentFlag.AlignHCenter)

        vb.addSpacing(18)
        self.titleLabel = TitleLabel(tr("app.title"), self._panel)
        self.titleLabel.setStyleSheet(
            f"color: {theme.ACCENT_HEX}; background-color: transparent;"
        )
        self.titleLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vb.addWidget(self.titleLabel, 0, Qt.AlignmentFlag.AlignHCenter)

        vb.addSpacing(22)
        self.progressBar = ProgressBar(self._panel)
        self.progressBar.setFixedWidth(BAR_WIDTH)
        self.progressBar.setRange(0, 100)
        self.progressBar.setValue(0)
        vb.addWidget(self.progressBar, 0, Qt.AlignmentFlag.AlignHCenter)

        vb.addSpacing(12)
        self.hintLabel = CaptionLabel(tr("splash.starting"), self._panel)
        self.hintLabel.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; background-color: transparent;"
        )
        self.hintLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hintLabel.setFixedWidth(BAR_WIDTH)
        vb.addWidget(self.hintLabel, 0, Qt.AlignmentFlag.AlignHCenter)

        self._panel.adjustSize()

    # ------------------------------------------------------------------ 进度
    def set_progress(self, percent: int, hint: str = "") -> None:
        """推进进度条；``hint`` 为空时保持上一条提示不变。"""
        self.progressBar.setValue(max(0, min(100, int(percent))))
        if hint:
            self.hintLabel.setText(hint)

    # ------------------------------------------------------------------ 绘制
    def resizeEvent(self, e):  # noqa: N802 (Qt 命名)
        super().resizeEvent(e)
        self._panel.adjustSize()
        self._panel.move(
            self.width() // 2 - self._panel.width() // 2,
            self.height() // 2 - self._panel.height() // 2,
        )

    def paintEvent(self, e):  # noqa: N802 (Qt 命名)
        # 基类画的是写死的纯白/纯黑，这里换成应用自己的窗口底色，避免关闭瞬间闪一下。
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.WINDOW_BG)
        painter.drawRect(self.rect())
