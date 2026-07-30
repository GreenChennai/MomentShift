"""基础滚动界面 —— 所有功能标签页的共享基类（v0.3.2 简化：仅浅色主题）。

提供：
- 统一标题头（标题 + 副标题 + accent 下划线）
- 折叠卡片注册与"至少保留一个展开"守卫
- 可折叠卡片的自动折叠/展开策略
- 共享 UI 组件构建器
"""

import os
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QScrollArea, QLineEdit, QFileDialog, QHBoxLayout

from ..core.qt_compat import QWidget, QVBoxLayout, QFrame
from qfluentwidgets import (
    ScrollArea, TitleLabel, CaptionLabel,
    ComboBox, SwitchButton, TransparentToolButton, FluentIcon as FIF,
)
from .theme import (
    content_bg, WINDOW_BG, accent_name, ThemedCard,
    CollapsibleCard, field_row, primary_btn, ghost_btn, icon_btn,
    scrollbar_qss, muted_text,
)

class InterfaceBase(ScrollArea):
    def __init__(self, object_name: str, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setWidgetResizable(True)

        self.view = QWidget()
        self.view.setObjectName(object_name + "View")
        self.setWidget(self.view)

        self.vbox = QVBoxLayout(self.view)
        self.vbox.setContentsMargins(16, 14, 16, 14)
        self.vbox.setSpacing(12)

        # 标题头
        self.header = QWidget()
        hb = QVBoxLayout(self.header)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(4)
        self.titleLabel = TitleLabel(title)
        hb.addWidget(self.titleLabel)
        if subtitle:
            self.subLabel = CaptionLabel(subtitle)
            hb.addWidget(self.subLabel)
        self.accentRule = QFrame()
        self.accentRule.setFrameShape(QFrame.Shape.HLine)
        self.accentRule.setFixedHeight(3)
        self.accentRule.setFixedWidth(38)
        self._style_accent()
        hb.addWidget(self.accentRule)
        hb.addSpacing(4)
        self.vbox.addWidget(self.header)

        InterfaceBase.retheme(self)

        self._collapsibles: list = []
        self._collapse_ready = False

    def register_collapsible(self, card) -> None:
        if card not in self._collapsibles:
            self._collapsibles.append(card)
            card.set_toggle_guard(self._can_collapse)

    def _can_collapse(self, card, want_collapse: bool) -> bool:
        if not want_collapse or not self._collapse_ready:
            return True
        expanded = [c for c in self._collapsibles
                     if c.isVisible() and not c.isCollapsed()]
        return len(expanded) > 1

    def _style_accent(self):
        self.accentRule.setStyleSheet(
            f"QFrame{{ background: {accent_name()}; border: none; border-radius: 2px; }}")

    # 共享 UI 组件构建器
    def _make_card(self, title_key, subtitle_key=None, collapsed=False):
        from ..i18n.translator import tr
        title_text = tr(title_key)
        sub_text = tr(subtitle_key) if subtitle_key else ""
        card = CollapsibleCard(title_text, sub_text, self, collapsed=collapsed)
        self.register_collapsible(card)
        return card, card.body, card.titleLabel

    def _make_scroll(self, min_height: int = 280) -> QScrollArea:
        s = QScrollArea()
        s.setWidgetResizable(True)
        s.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        s.setStyleSheet(f"QScrollArea{{border:none; background:transparent;}} {scrollbar_qss()}")
        s.viewport().setStyleSheet("background:transparent;")
        s.setMinimumHeight(min_height)
        return s

    def _make_combo(self, mapping, current, on_change) -> ComboBox:
        combo = ComboBox()
        for disp, val in mapping:
            combo.addItem(disp)
        for i, (disp, val) in enumerate(mapping):
            if val == current:
                combo.setCurrentIndex(i)
                break
        combo._mapping = dict(mapping)
        combo.currentTextChanged.connect(lambda t: on_change(combo._mapping.get(t, t)))
        return combo

    def _repopulate_combo(self, combo: ComboBox, mapping):
        current_val = combo._mapping.get(combo.currentText(), combo.currentText())
        combo.blockSignals(True)
        combo.clear()
        combo._mapping = dict(mapping)
        for disp, val in mapping:
            combo.addItem(disp)
        for i, (disp, val) in enumerate(mapping):
            if val == current_val:
                combo.setCurrentIndex(i)
                break
        combo.blockSignals(False)

    def _expand_paths(self, paths, valid_exts):
        out = []
        for p in paths:
            if os.path.isdir(p):
                for root, _, files in os.walk(p):
                    for f in files:
                        fp = os.path.join(root, f)
                        if Path(fp).suffix.lower() in valid_exts:
                            out.append(fp)
            elif os.path.isfile(p) and Path(p).suffix.lower() in valid_exts:
                out.append(p)
        seen, uniq = set(), []
        for p in out:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    def _auto_collapse(self, *cards):
        from ..core.config import cfg
        if not cfg.autoCollapse.value:
            return
        for c in cards:
            c.setCollapsed(True)

    def _auto_expand(self, *cards):
        from ..core.config import cfg
        if not cfg.autoCollapse.value:
            return
        for c in cards:
            c.setCollapsed(False)

    def retheme(self):
        bg = WINDOW_BG
        oid = self.view.objectName() or "view"
        css = (
            f"#{oid} {{ background-color: {bg.name()}; border: none; }}"
            "QLabel, FluentLabelBase, BodyLabel, CaptionLabel, StrongBodyLabel,"
            " TitleLabel, SubtitleLabel { background-color: transparent; }"
        )
        self.view.setStyleSheet(css)
        if self.viewport():
            self.viewport().setStyleSheet(f"background-color: {bg.name()}; border: none;")
        for card in self.findChildren(ThemedCard):
            card.retheme()
        self._style_accent()

    def retranslate(self, title=None, subtitle=None):
        if title is not None:
            self.titleLabel.setText(title)
        if subtitle is not None and hasattr(self, "subLabel"):
            self.subLabel.setText(subtitle)
