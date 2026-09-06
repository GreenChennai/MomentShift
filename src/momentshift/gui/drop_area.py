"""拖拽 / 点击输入区域。

职责边界：
- 做：接收拖入的文件与文件夹、过滤有效路径、发出 filesDropped 信号。
- 不做：不递归展开文件夹（交给 InterfaceBase._expand_paths）；不判断格式是否受支持。

依赖：core/qt_compat、gui/animations、gui/theme、gui/tokens；
被依赖：gui/compress_interface、gui/convert_interface、gui/upscale_interface。

v0.8.0 B3 动效接入点：悬停 / 拖入的高亮过渡。这里**只能**改 QSS 颜色，
不能用 QGraphicsOpacityEffect —— ``iconBadge`` 走了 setMask（见 animations 铁律二）。

踩坑教训：dropEvent 里必须用 QTimer.singleShot(0, ...) 把后续处理推迟到下一轮
事件循环，在 dropEvent 内直接弹窗会让拖拽源进程一起卡死。

之前的 mouseReleaseEvent → clicked 路径在 modal QFileDialog 关闭时
受 synthetic mouseReleaseEvent 影响，持续出现双击弹框。QPushButton
原生处理平台点击事件，不受此影响——这是从 v0.2.6 起尝试各种修复后
唯一干净且可靠的方案。
"""

from __future__ import annotations

import re

from PyQt6.QtCore import Qt, QTimer, pyqtProperty
from PyQt6.QtGui import QColor, QRegion
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, StrongBodyLabel, TransparentPushButton
from qfluentwidgets import FluentIcon as FIF

from ..core.qt_compat import QDragEnterEvent, QDropEvent, Signal
from . import animations, tokens
from .theme import (
    ACCENT_HEX,
    ThemedCard,
    border_color,
    surface,
    surface_pressed,
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
        self._hover = False  # 目标态：鼠标/拖拽是否在区内
        self._hover_t = 0.0  # 渲染态：0 常态、1 高亮，中间值由动效驱动
        self._pressed = False
        self._formats = ""

        self.setStyleSheet(
            f"DropArea {{ background-color: {tokens.SURFACE}; border-radius: 14px; }}"
        )

        # 内部虚线区域
        self.inner = QWidget(self)
        self.inner.setObjectName("dropInner")
        vb = QVBoxLayout(self.inner)
        # v0.9.1 紧凑化：默认窗口（820x760）下四个主页面要免滚动，拖拽区是
        # 页面上最大的单体组件，内边距/图标/行距全面收一档（62px 徽标 → 46px）。
        vb.setContentsMargins(14, 12, 14, 12)
        vb.setSpacing(6)
        vb.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.iconBadge = QLabel(self)
        self.iconBadge.setFixedSize(46, 46)
        self.iconBadge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.iconBadge.setMask(QRegion(self.iconBadge.rect(), QRegion.RegionType.Ellipse))
        vb.addWidget(self.iconBadge, alignment=Qt.AlignmentFlag.AlignCenter)

        self.titleLabel = StrongBodyLabel()
        self.titleLabel.setObjectName("dropTitle")
        vb.addWidget(self.titleLabel, alignment=Qt.AlignmentFlag.AlignCenter)

        self.hintLabel = CaptionLabel()
        self.hintLabel.setObjectName("dropHint")
        self.hintLabel.setStyleSheet(tokens.text_qss(tokens.TEXT_STRONG))
        vb.addWidget(self.hintLabel, alignment=Qt.AlignmentFlag.AlignCenter)

        self.chipsWrap = QWidget(self)
        self.chipsWrap.setStyleSheet("background: transparent;")
        self.chipsLayout = QHBoxLayout(self.chipsWrap)
        self.chipsLayout.setContentsMargins(0, 0, 0, 0)
        self.chipsLayout.setSpacing(6)
        vb.addWidget(self.chipsWrap, alignment=Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.addWidget(self.inner)

        # --- 次级动作行：虚线区下方的居中透明按钮（如「添加文件夹」）---
        # v0.9 UI 重构：原先页面在拖拽卡外再放一条全宽主色按钮，与「点击虚线区
        # 选文件」动作重复且视觉过重。现在把次级动作收进卡片：主视觉只剩拖拽区，
        # 动作按主次分层（点击虚线区=选文件，下方文字按钮=选文件夹）。
        # 注意：不能把按钮放进 ``inner``——透明覆盖按钮 ``_clickBtn`` 盖住整个
        # 虚线区且被 raise 到顶层，点击会被它吞掉；放在 inner 之外则天然可用。
        self.footer = QWidget(self)
        self.footer.setStyleSheet("background: transparent;")
        fh = QHBoxLayout(self.footer)
        fh.setContentsMargins(0, 2, 0, 0)
        fh.addStretch(1)
        self.actionBtn = TransparentPushButton(self.footer)
        self.actionBtn.setVisible(False)
        self.actionBtn.clicked.connect(self._on_action_clicked)
        fh.addWidget(self.actionBtn)
        fh.addStretch(1)
        layout.addWidget(self.footer)

        # --- : 透明覆盖按钮替代 mouseReleaseEvent ---
        self._clickBtn = QPushButton("", self)
        self._clickBtn.setStyleSheet(
            "QPushButton { background: transparent; border: none; }"
            "QPushButton:hover { background: transparent; }"
            "QPushButton:pressed { background: transparent; }"
        )
        self._clickBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clickBtn.clicked.connect(self._on_button_clicked)
        self._clickBtn.raise_()

        self._action_cb = None
        self.retheme()

    def _on_button_clicked(self):
        """QPushButton 原生 click 事件——不受 modal dialog 的 synthetic event 影响。"""
        self.clicked.emit()

    def set_action(self, text: str, callback) -> None:
        """配置虚线区下方的次级动作按钮（如「添加文件夹」）。

        Args:
            text: 按钮文案；空串表示不显示该按钮。
            callback: 点击回调（页面自己的 ``_pick_folder`` 等，业务信号不变）。
        """
        self._action_cb = callback
        self.actionBtn.setText(text)
        self.actionBtn.setIcon(FIF.FOLDER_ADD)
        self.actionBtn.setVisible(bool(text))

    def _on_action_clicked(self):
        if self._action_cb is not None:
            self._action_cb()

    def _normalBackgroundColor(self):
        return surface()

    def _hoverBackgroundColor(self):
        return surface()

    def _pressedBackgroundColor(self):
        return surface()

    def _accent_tint(self) -> str:
        c = QColor(ACCENT_HEX)
        return f"rgba({c.red()},{c.green()},{c.blue()},0.14)"

    def _accent_tint_opaque(self) -> str:
        """``_accent_tint()`` 的不透明等效色，只用于过渡途中。

        Returns:
            形如 ``#eef4ef`` 的十六进制串。

        Notes:
            QSS 的 ``rgba`` 是在下层底色上做 alpha 合成，而这里的下层恒为
            ``DropArea`` 自己的 ``tokens.SURFACE``；所以按同样的 0.14 比例把
            两个不透明色混一下，得到的就是肉眼等价的结果。
            为什么需要它：:func:`animations.blend_color` 只能在两个不透明色之间
            插值，不认 ``rgba(...)`` 写法。两个端点仍写各自的**原文**，
            只有 0<t<1 的中途帧用这个等效色 —— 稳态字符串一字不差，
            QSS 快照不会有任何差异。
        """
        return animations.blend_color(surface().name(), ACCENT_HEX, 0.14)

    def retheme(self):
        self.iconBadge.setPixmap(FIF.FOLDER_ADD.icon(QColor(ACCENT_HEX)).pixmap(24, 24))
        self.iconBadge.setStyleSheet(f"background: {self._accent_tint()}; border-radius: 23px;")
        self._render_chips(self._parse_formats(self._formats))
        self._apply_style()
        self._position_button()

    # -- 悬停高亮过渡（v0.8.0 B3 接入点 3）------------------------------
    # 铁律二：``iconBadge`` 走了 ``setMask(QRegion(..., Ellipse))``，整块
    # DropArea 一旦挂上 ``QGraphicsOpacityEffect`` 就会与那条裁剪路径叠加，
    # 在部分平台把圆形徽标啃出黑边。所以这里**不碰任何 graphics effect**，
    # 只用 blend_color 重写 ``#dropInner`` 的两个颜色值。
    def _get_hover_t(self) -> float:
        return self._hover_t

    def _set_hover_t(self, value: float) -> None:
        self._hover_t = max(0.0, min(1.0, float(value)))
        self._paint_inner()

    hoverT = pyqtProperty(float, fget=_get_hover_t, fset=_set_hover_t)

    def _inner_colors(self) -> tuple[str, str]:
        """按当前渲染态算出内框的 (边框色, 底色)。

        Returns:
            两个可直接拼进 QSS 的颜色串。

        Notes:
            ``t`` 恰为 0 或 1 时返回的是改造前那两组**原始表达式**的结果
            （``border_color()`` / ``surface().name()`` / ``ACCENT_HEX`` /
            ``_accent_tint()``），一个字符都没变；只有中途帧才走插值。
        """
        if self._pressed:
            return ACCENT_HEX, surface_pressed().name()
        t = self._hover_t
        if t >= 1.0:
            return ACCENT_HEX, self._accent_tint()
        if t <= 0.0:
            return border_color(), surface().name()
        return (
            animations.blend_color(border_color(), ACCENT_HEX, t),
            animations.blend_color(surface().name(), self._accent_tint_opaque(), t),
        )

    def _paint_inner(self):
        """把当前颜色写进 ``#dropInner`` 的样式表。"""
        border, bg = self._inner_colors()
        self.inner.setStyleSheet(
            f"#dropInner{{ border: 2px dashed {border}; border-radius: 12px; background: {bg}; }}"
        )

    def _apply_style(self):
        """立即把内框刷成目标态（无过渡）。

        Notes:
            供 ``retheme()`` 与「必须硬切」的场合使用；带过渡的悬停切换走
            :meth:`_set_hover`。
        """
        animations.stop(self, b"hoverT")
        self._hover_t = 1.0 if self._hover else 0.0
        self._paint_inner()

    def _set_hover(self, hover: bool) -> None:
        """切换高亮态，附带一段 120ms 过渡。

        Args:
            hover: 目标是否高亮。

        Notes:
            120ms 是 ``DURATION_FAST``：用户的手正在动，这类跟随型动效超过
            ~150ms 就会被感知成「界面反应慢」，反而不如瞬切。
            目标态没变则直接返回 —— 拖拽经过时 ``dragEnterEvent`` 可能连发。
        """
        if self._hover == hover:
            return
        self._hover = hover
        if not animations.should_animate(self):
            self._apply_style()
            return
        animations.animate_value(
            self,
            b"hoverT",
            1.0 if hover else 0.0,
            duration=animations.DURATION_FAST,
            curve=animations.CURVE_SMOOTH,
            animate=True,
        )

    @staticmethod
    def _parse_formats(text: str) -> list[str]:
        t = text
        for p in ("支持", "Supports", " supports"):
            t = t.replace(p, "")
        # 注意：分隔符必须包含 ``/``——「图片 / 音频 / 视频」里的斜杠若不当分隔符，
        # 会被切成「图片」「/」「音频」「/」「视频」四个徽标（Bug #1）。
        parts = re.split(r"[·•、,，/\s]+", t)
        return [p.strip() for p in parts if p.strip()]

    def _render_chips(self, labels: list[str]) -> None:
        """重建格式徽标行。

        Args:
            labels: 形如 ``["MP4", "MKV"]`` 的格式短名列表；空列表则隐藏整行。

        Notes:
            形参原名 ``tokens``，与视觉令牌模块 ``gui.tokens`` 重名会遮蔽模块，
            故改名 ``labels``；本方法只在类内调用，无外部影响。
        """
        while self.chipsLayout.count():
            w = self.chipsLayout.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not labels:
            self.chipsWrap.hide()
            return
        self.chipsWrap.show()
        for text in labels:
            chip = QLabel(text)
            chip.setObjectName("dropChip")
            # v0.9 UI 重构：实底反白 → 淡底深字。一排并陈的静态格式徽标用
            # 轻样式（soft_chip_qss），不再与状态胶囊抢注意力。
            chip.setStyleSheet(
                f"QLabel#dropChip{{{tokens.soft_chip_qss(tokens.ACCENT_HOVER, tokens.ACCENT_SOFT)}}}"
            )
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
        """让透明按钮与虚线区完全重合（不含下方次级动作行）。"""
        self._clickBtn.setGeometry(self.inner.geometry())

    # -- 拖拽事件（不受点击改动影响）----------------------
    def enterEvent(self, event):
        self._set_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._set_hover(False)
        super().leaveEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_hover(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._set_hover(False)
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

        v0.8.0 B3：落下这一刻**不走过渡**。下一轮事件循环里紧接着就会弹出
        「转换设置」模态框，此时还在跑的高亮渐变要么被模态事件循环拖成卡顿，
        要么被盖住白播；drop 本身是一次确定的确认动作，硬切回常态最干净。
        """
        self._hover = False
        self._apply_style()
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            QTimer.singleShot(0, lambda p=paths: self.filesDropped.emit(p))
        else:
            event.ignore()
