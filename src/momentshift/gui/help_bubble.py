"""共用的帮助气泡弹层（替代 QMessageBox）。

职责边界：
- 做：提供参数说明弹窗与「给参数行挂问号按钮」的辅助函数。
- 不做：不存放说明文案本身（文案在 i18n 语言包里，按 key 取用）。

依赖：gui/theme、i18n/translator；被依赖：gui/advanced_panel、gui/compress_interface、gui/upscale_interface。

为什么自己写一个而不是用 QMessageBox：
- QMessageBox 在 Windows 上以 Information 图标弹出时会播放系统提示音，帮助气泡
  很频繁，应当静默。
- 自定义 QDialog 可以把文字排得好看（卡片 + 分隔线 + 绿色标题），而非默认朴素框。

公开 API：
- HelpDialog(text, parent=None) —— 美化、无提示音的弹层。
- attach_help(field_row, help_key, parent=None) —— 在 field_row 右侧加灰色帮助
  按钮，点击打开 HelpDialog。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import TransparentToolButton

from ..i18n.translator import tr
from . import tokens
from .theme import accent_color, muted_text


class HelpDialog(QDialog):
    """参数说明弹窗，展示单条高级参数的解释文案。

    典型用法::

        HelpDialog(tr("advanced.help.crf"), self).exec()

    为什么自绘而不用 QMessageBox：QMessageBox 在 Windows 上会播放系统提示音，
    而这里只是「看一段说明」，提示音属于打扰；同时也便于统一圆角与配色。
    """

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("advanced.help"))
        self.setMinimumWidth(340)
        self.setModal(True)
        self.setStyleSheet(
            f"QDialog{{ background:{tokens.WHITE}; border-radius:{tokens.RADIUS_LG}px; }}"
        )

        vb = QVBoxLayout(self)
        vb.setContentsMargins(22, 20, 22, 20)
        vb.setSpacing(14)

        # 标题行：图标 + 标题
        hb = QHBoxLayout()
        hb.setSpacing(10)
        ico = QLabel()
        ico.setPixmap(FIF.INFO.icon(accent_color()).pixmap(22, 22))
        ico.setFixedSize(22, 22)
        hb.addWidget(ico)
        title = QLabel(tr("advanced.help"))
        title.setStyleSheet(
            f"font-size:{tokens.FONT_SUBTITLE}px; font-weight:700; color:{tokens.TEXT_STRONG};"
        )
        hb.addWidget(title)
        hb.addStretch(1)
        vb.addLayout(hb)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color:{muted_text()};")
        vb.addWidget(sep)

        body = QLabel(text)
        body.setWordWrap(True)
        body.setStyleSheet(
            f"font-size:{tokens.FONT_BODY}px; color:{tokens.TEXT_BODY};"
            f" line-height:1.7; background:transparent;"
        )
        vb.addWidget(body)

        rb = QHBoxLayout()
        rb.addStretch(1)
        ok = QPushButton(tr("common.ok"))
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        # 与 convert_setup_dialog 的主按钮同构，但白色写法是小写 #ffffff（那边是
        # 大写 #FFFFFF）。B1 铁律 R3 禁止擅自统一近似写法，故未合并到
        # tokens.accent_button_qss()，只把色值换成令牌引用。
        ok.setStyleSheet(
            f"QPushButton{{ background:{tokens.ACCENT}; color:{tokens.WHITE};"
            f" border:none; border-radius:{tokens.RADIUS_MD}px; padding:8px 22px;"
            f" font-weight:600; font-size:{tokens.FONT_BODY}px; }}"
            f"QPushButton:hover{{ background:{tokens.ACCENT_HOVER}; }}"
            f"QPushButton:pressed{{ background:{tokens.ACCENT_PRESS}; }}"
        )
        ok.clicked.connect(self.accept)
        rb.addWidget(ok)
        vb.addLayout(rb)


def attach_help(field_row_widget, help_key: str, parent=None):
    """在参数行右侧追加一个灰色问号按钮，点击弹出说明。

    Args:
        field_row_widget: 参数行控件，必须已有 layout()。
        help_key: 说明文案的 i18n key，取文案时才解析，保证能跟随语言切换。
        parent: 弹窗的父控件，用于居中定位。

    Notes:
        用点击弹窗而不是 setToolTip：悬浮提示在触控与高 DPI 下命中困难，
        且长文案会被截断。
    """
    btn = TransparentToolButton(FIF.HELP.icon(color=QColor(tokens.ICON_MUTED)), parent)
    btn.setFixedSize(20, 20)

    def _show():
        dlg = HelpDialog(tr(help_key), parent)
        dlg.exec()

    btn.clicked.connect(_show)
    field_row_widget.layout().addWidget(btn)
