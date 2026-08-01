"""拖拽 / 点击输入区域（v0.3.4 重构：QPushButton 覆盖层替代 mouseReleaseEvent）。

之前的 mouseReleaseEvent → clicked 路径在 modal QFileDialog 关闭时
受 synthetic mouseReleaseEvent 影响，持续出现双击弹框。QPushButton
原生处理平台点击事件，不受此影响——这是从 v0.2.6 起尝试各种修复后
唯一干净且可靠的方案。
"""

from __future__ import annotations

import re
from PyQt6.QtGui import QColor, QPixmap, QPainter, QPen, QRegion
from PyQt6.QtCore import Qt, QUrl, QTimer
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton
from qfluentwidgets import FluentIcon as FIF, StrongBodyLabel, CaptionLabel

from ..core.qt_compat import Signal, QDragEnterEvent, QDropEvent
from .theme import (
    ThemedCard, muted_text, accent_name, surface, surface_pressed, border_color,
    placeholder_text, ACCENT_HEX,
)

class DropArea(ThemedCard):
    """虚线拖拽区。发出 ``filesDropped``（路径列表）信号。
    点击通过透明 QPushButton 覆盖层触发，不再使用 mouseReleaseEvent。"""

    filesDropped = Signal(list)
    # clicks 信号由内部 QPushButton 转发
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBorderRadius(14)
        self.setAcceptDrops(True)
        self._hover = False
        self._pressed = False
        self._formats = ""

        self.setStyleSheet(
            "DropArea { background-color: #F5F5F5; border-radius: 14px; }"
        )

        # 内部虚线区域
        self.inner = QWidget(self)
        self.inner.setObjectName("dropInner")
        vb = QVBoxLayout(self.inner)
        vb.setContentsMargins(18, 22, 18, 22)
        vb.setSpacing(10)
        vb.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.iconBadge = QLabel(self)
        self.iconBadge.setFixedSize(62, 62)
        self.iconBadge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.iconBadge.setMask(QRegion(self.iconBadge.rect(), QRegion.RegionType.Ellipse))
        vb.addWidget(self.iconBadge, alignment=Qt.AlignmentFlag.AlignCenter)

        self.titleLabel = StrongBodyLabel()
        self.titleLabel.setObjectName("dropTitle")
        vb.addWidget(self.titleLabel, alignment=Qt.AlignmentFlag.AlignCenter)

        self.hintLabel = CaptionLabel()
        self.hintLabel.setObjectName("dropHint")
        self.hintLabel.setStyleSheet("color: #212121;")
        vb.addWidget(self.hintLabel, alignment=Qt.AlignmentFlag.AlignCenter)

        self.chipsWrap = QWidget(self)
        self.chipsWrap.setStyleSheet("background: transparent;")
        self.chipsLayout = QHBoxLayout(self.chipsWrap)
        self.chipsLayout.setContentsMargins(0, 0, 0, 0)
        self.chipsLayout.setSpacing(6)
        vb.addWidget(self.chipsWrap, alignment=Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(self.inner)

        # --- v0.3.4: 透明覆盖按钮替代 mouseReleaseEvent ---
        self._clickBtn = QPushButton("", self)
        self._clickBtn.setStyleSheet(
            "QPushButton { background: transparent; border: none; }"
            "QPushButton:hover { background: transparent; }"
            "QPushButton:pressed { background: transparent; }"
        )
        self._clickBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clickBtn.clicked.connect(self._on_button_clicked)
        self._clickBtn.raise_()

        self.retheme()

    def _on_button_clicked(self):
        """QPushButton 原生 click 事件——不受 modal dialog 的 synthetic event 影响。"""
        self.clicked.emit()

    def _normalBackgroundColor(self):
        return surface()

    def _hoverBackgroundColor(self):
        return surface()

    def _pressedBackgroundColor(self):
        return surface()

    def _accent_tint(self) -> str:
        c = QColor(ACCENT_HEX)
        return f"rgba({c.red()},{c.green()},{c.blue()},0.14)"

    def retheme(self):
        self.iconBadge.setPixmap(
            FIF.FOLDER_ADD.icon(QColor(ACCENT_HEX)).pixmap(30, 30))
        self.iconBadge.setStyleSheet(
            f"background: {self._accent_tint()}; border-radius: 31px;")
        self._render_chips(self._parse_formats(self._formats))
        self._apply_style()
        self._position_button()

    def _apply_style(self):
        if self._pressed:
            border = ACCENT_HEX
            bg = surface_pressed().name()
        elif self._hover:
            border = ACCENT_HEX
            bg = self._accent_tint()
        else:
            border = border_color()
            bg = surface().name()
        self.inner.setStyleSheet(
            f"#dropInner{{ border: 2px dashed {border}; "
            f"border-radius: 12px; background: {bg}; }}"
        )

    @staticmethod
    def _parse_formats(text: str) -> list[str]:
        t = text
        for p in ("支持", "Supports", " supports"):
            t = t.replace(p, "")
        parts = re.split(r"[·•、,，\s]+", t)
        return [p.strip() for p in parts if p.strip()]

    def _render_chips(self, tokens: list[str]) -> None:
        while self.chipsLayout.count():
            w = self.chipsLayout.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not tokens:
            self.chipsWrap.hide()
            return
        self.chipsWrap.show()
        bg = ACCENT_HEX
        for tok in tokens:
            chip = QLabel(tok)
            chip.setObjectName("dropChip")
            chip.setStyleSheet(
                f"QLabel#dropChip{{ color: #FFFFFF; background: {bg};"
                f" border-radius: 6px; padding: 2px 9px; font-size: 12px; }}")
            self.chipsLayout.addWidget(chip)
        self.chipsLayout.addStretch(1)

    def retranslate(self, title="", hint="", formats=""):
        if title:
            self.titleLabel.setText(title)
        if hint:
            self.hintLabel.setText(hint)
        if formats:
            self._formats = formats
            self._render_chips(self._parse_formats(formats))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_button()

    def _position_button(self):
        """让透明按钮覆盖整个 DropArea，与拖拽区完全重合。"""
        self._clickBtn.setGeometry(0, 0, self.width(), self.height())

    # -- 拖拽事件（不受点击改动影响）----------------------
    def enterEvent(self, event):
        self._hover = True
        self._apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self._apply_style()
        super().leaveEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._hover = True
            self._apply_style()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._hover = False
        self._apply_style()
        super().dragLeaveEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        """接收拖入的文件。

        v0.7.3 Bug1：dropEvent 由 ``IDropTarget::Drop`` 同步调用，而资源管理器
        的 ``DoDragDrop`` 会阻塞等待它返回。若在此直接 emit，下游立刻弹出模态
        对话框（转换设置 / 文件选择器），Drop 永不返回 → 源 Explorer 窗口彻底
        卡死。因此这里只接受事件并把处理推迟到下一轮事件循环。
        """
        self._hover = False
        self._apply_style()
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            QTimer.singleShot(0, lambda p=paths: self.filesDropped.emit(p))
        else:
            event.ignore()
