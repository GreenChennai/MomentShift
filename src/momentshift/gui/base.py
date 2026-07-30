"""基础滚动界面 —— 所有功能标签页的共享基类。

提供：
- 统一标题头（标题 + 副标题 + accent 下划线）
- 折叠卡片注册与"至少保留一个展开"守卫
- 可折叠卡片的自动折叠/展开策略
- 共享 UI 组件构建器（输入卡、输出卡、滚动区、下拉框、路径展开）
  这些构建器消除了 Convert/Compress/Upscale 三大界面的重复代码。
"""

import os
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QScrollArea, QLineEdit, QFileDialog, QHBoxLayout

from ..core.qt_compat import QWidget, QVBoxLayout, QFrame
from qfluentwidgets import (
    ScrollArea, TitleLabel, CaptionLabel, isDarkTheme,
    ComboBox, SwitchButton, TransparentToolButton, FluentIcon as FIF,
)
from .theme import (
    content_bg, LIGHT_BG, DARK_BG, accent_name, ThemedCard,
    CollapsibleCard, field_row, primary_btn, ghost_btn, icon_btn,
    scrollbar_qss, muted_text,
)


class InterfaceBase(ScrollArea):
    """可滚动的导航界面基类。

    每个功能标签页（转换、压缩、放大、设置、关于、快速调用）继承此类，
    获得统一的外观、折叠卡片管理和共享 UI 构建方法。
    """

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

        # =====================================================================
        # 标题头（标题 + 可选副标题 + accent 彩色下划线）
        # =====================================================================
        self.header = QWidget()
        hb = QVBoxLayout(self.header)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(4)
        self.titleLabel = TitleLabel(title)
        hb.addWidget(self.titleLabel)
        if subtitle:
            self.subLabel = CaptionLabel(subtitle)
            hb.addWidget(self.subLabel)
        # accent 彩色下划线（品牌色 #238636）
        self.accentRule = QFrame()
        self.accentRule.setFrameShape(QFrame.Shape.HLine)
        self.accentRule.setFixedHeight(3)
        self.accentRule.setFixedWidth(38)
        self._style_accent()
        hb.addWidget(self.accentRule)
        hb.addSpacing(4)
        self.vbox.addWidget(self.header)

        # 基础主题先应用（子类在 __init__ 末尾调自己的 retheme()）
        InterfaceBase.retheme(self)

        # 折叠卡片注册 + "至少保留一个展开"守卫
        self._collapsibles: list = []
        self._collapse_ready = False

    # =========================================================================
    # 折叠卡片管理
    # =========================================================================

    def register_collapsible(self, card) -> None:
        """注册一个 CollapsibleCard 使"至少一个展开"规则生效。"""
        if card not in self._collapsibles:
            self._collapsibles.append(card)
            card.set_toggle_guard(self._can_collapse)

    def _can_collapse(self, card, want_collapse: bool) -> bool:
        """守卫：拒绝折叠最后一张可见的展开卡片。

        返回 ``True`` 表示允许切换。构造期间（``_collapse_ready=False``）
        始终允许，确保初始折叠态正常设置。
        """
        if not want_collapse or not self._collapse_ready:
            return True
        expanded = [c for c in self._collapsibles
                     if c.isVisible() and not c.isCollapsed()]
        return len(expanded) > 1

    def _style_accent(self):
        """绘制标题下方的品牌色下划线。"""
        self.accentRule.setStyleSheet(
            f"QFrame{{ background: {accent_name()}; border: none; border-radius: 2px; }}"
        )

    # =========================================================================
    # 共享 UI 组件构建器（消除三大界面重复代码的核心）
    # =========================================================================

    def _make_card(self, title_key: str, subtitle_key: str | None = None,
                   collapsed: bool = False):
        """创建一个可折叠的主题卡片。

        返回 ``(card, body_layout, title_label)`` 三元组。
        ``card`` 自动注册到折叠守卫。
        """
        title_text = self.tr(title_key)
        sub_text = self.tr(subtitle_key) if subtitle_key else ""
        card = CollapsibleCard(title_text, sub_text, self, collapsed=collapsed)
        self.register_collapsible(card)
        return card, card.body, card.titleLabel

    def _make_scroll(self, min_height: int = 280) -> QScrollArea:
        """创建一个带主题滚动条且背景透明的 QScrollArea。"""
        s = QScrollArea()
        s.setWidgetResizable(True)
        s.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        s.setStyleSheet(
            f"QScrollArea{{border:none; background:transparent;}} {scrollbar_qss()}"
        )
        s.viewport().setStyleSheet("background:transparent;")
        s.setMinimumHeight(min_height)
        return s

    def _make_combo(self, mapping: list[tuple], current, on_change) -> ComboBox:
        """构建一个选项下拉框。

        ``mapping`` 是 ``[(显示文字, 值), ...]`` 列表。
        ``_mapping`` 字典附在 ComboBox 上供后续查阅。
        """
        combo = ComboBox()
        for disp, val in mapping:
            combo.addItem(disp)
        for i, (disp, val) in enumerate(mapping):
            if val == current:
                combo.setCurrentIndex(i)
                break
        combo._mapping = dict(mapping)
        combo.currentTextChanged.connect(
            lambda t: on_change(combo._mapping.get(t, t)))
        return combo

    def _repopulate_combo(self, combo: ComboBox, mapping: list[tuple]):
        """重新填充 combo 的选项（用于语言切换时刷新翻译文字）。

        保持当前选中值不变（按 key 匹配而非按文字位置）。
        """
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

    def _expand_paths(self, paths: list[str], valid_exts: set[str]) -> list[str]:
        """展开路径列表：目录递归 + 去重。

        ``valid_exts`` 是小写后缀集合（含点号），如 ``{".jpg", ".png", ".mp4"}``。
        返回去重后的文件路径列表。
        """
        out: list[str] = []
        for p in paths:
            if os.path.isdir(p):
                for root, _, files in os.walk(p):
                    for f in files:
                        fp = os.path.join(root, f)
                        if Path(fp).suffix.lower() in valid_exts:
                            out.append(fp)
            elif os.path.isfile(p) and Path(p).suffix.lower() in valid_exts:
                out.append(p)
        # 去重，保序
        seen, uniq = set(), []
        for p in out:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    # =========================================================================
    # 共享策略方法（自动折叠/展开卡片）
    # =========================================================================

    @property
    def _auto_fold_enabled(self) -> bool:
        """检查"自动折叠"开关是否开启。"""
        from ..core.config import cfg
        return bool(cfg.autoCollapse.value)

    def _auto_collapse(self, *cards):
        """任务批次完成后自动折叠指定卡片（若自动折叠开关启用）。"""
        if not self._auto_fold_enabled:
            return
        for c in cards:
            c.setCollapsed(True)

    def _auto_expand(self, *cards):
        """用户添加新文件时自动展开指定卡片（若自动折叠开关启用）。"""
        if not self._auto_fold_enabled:
            return
        for c in cards:
            c.setCollapsed(False)

    # =========================================================================
    # 主题 / i18n
    # =========================================================================

    def retheme(self):
        """应用实心主题背景到滚动视图和 viewport。

        使用 ID 限定选择器，确保背景色仅应用于当前视图而非子孙卡片。
        QLabel 背景被强制设为透明，使卡片底色透出。
        """
        bg = DARK_BG if isDarkTheme() else LIGHT_BG
        oid = self.view.objectName() or "view"
        css = (
            f"#{oid} {{ background-color: {bg.name()}; border: none; }}"
            "QLabel, FluentLabelBase, BodyLabel, CaptionLabel, StrongBodyLabel,"
            " TitleLabel, SubtitleLabel { background-color: transparent; }"
        )
        self.view.setStyleSheet(css)
        if self.viewport():
            self.viewport().setStyleSheet(
                f"background-color: {bg.name()}; border: none;")
        # 重绘所有 ThemedCard 使其边线和底色跟随主题
        for card in self.findChildren(ThemedCard):
            card.retheme()
        self._style_accent()

    def retranslate(self, title: str = None, subtitle: str = None):
        """更新标题和可选的副标题。"""
        if title is not None:
            self.titleLabel.setText(title)
        if subtitle is not None and hasattr(self, "subLabel"):
            self.subLabel.setText(subtitle)

    @staticmethod
    def tr(key: str, **kwargs) -> str:
        """便捷 i18n 方法，避免每个文件重复 import translator。"""
        from ..i18n.translator import tr
        return tr(key, **kwargs)
