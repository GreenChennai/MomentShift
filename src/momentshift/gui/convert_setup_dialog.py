"""转换设置弹窗：卡片化布局 + 正方形格式按钮。

职责边界：
- 做：让用户挑目标格式与高级参数，返回一份可直接入队的配置。
- 不做：不入队、不启动转换，只负责「收集并返回选择」。

依赖：core/config、core/presets、gui/advanced_panel、gui/theme、i18n/translator；被依赖：gui/convert_interface、quick_runner。

- 选择目标格式 → CollapsibleCard（可折叠）
- 高级设置 → CollapsibleCard（折叠按钮即总开关）
- 格式卡片：正方形、放大、浅绿默认/深绿选中 + 描边
"""

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)
from qfluentwidgets import (
    SwitchButton,
)

from ..core.config import cfg
from ..i18n.translator import tr
from . import tokens
from .advanced_panel import AdvancedPanel
from .theme import (
    CollapsibleCard,
    ThemedCard,
    accent_color,
    apply_plain_scroll,
    apply_text,
    apply_transparent,
    ghost_btn,
    icon_btn,
    muted_text,
    primary_btn,
    surface,
)

# 格式卡片按钮样式（：75×75 正方形）
_FMT_CARD_CSS = (
    "QPushButton{"
    f"  background: {tokens.ACCENT_TINT_BG}; border: 2px solid {tokens.ACCENT_TINT_BORDER};"
    "  border-radius: 10px;"
    f"  color: {tokens.ACCENT_TINT_TEXT}; font-weight: 700; font-size: {tokens.FONT_LARGE}px;"
    "  min-width: 150px; min-height: 75px; max-width: 150px; max-height: 75px;"
    "}"
    f"QPushButton:hover{{ background: {tokens.ACCENT_TINT_BORDER};"
    f" border-color: {tokens.ACCENT}; }}"
    "QPushButton:checked{"
    f"  border-color: {tokens.ACCENT}; border-width: 2px; background: {tokens.ACCENT};"
    f"  color: {tokens.WHITE};"
    "}"
)


class ConvertSetupDialog(QDialog):
    """单类别格式 + 高级选项 + 待处理列表。"""

    def __init__(self, parent, manager, paths, selection, gpu_fn, category):
        super().__init__(parent)
        self.manager = manager
        self._paths = list(paths)
        self._selection = dict(selection)
        self._gpu = gpu_fn
        self._category = category

        cat_names = {
            "image": tr("category.image"),
            "audio": tr("category.audio"),
            "video": tr("category.video"),
        }
        cat_disp = cat_names.get(category, category)

        self.setWindowTitle(f"{tr('convert.setup.title')} — {cat_disp}")
        self.resize(900, 720)
        self.setMinimumSize(720, 500)
        self.setObjectName("setupDlg")
        self.setStyleSheet(f"#setupDlg {{ background-color: {surface().name()}; }}")

        self._build_ui()
        self._render_staging()
        self._update_confirm()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        title = QLabel(tr("convert.setup.title"))
        apply_text(title, tokens.TEXT_TITLE, size=tokens.FONT_TITLE, weight=700)
        root.addWidget(title)
        hint = QLabel(tr("convert.setup.hint"))
        apply_text(hint, muted_text())
        root.addWidget(hint)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        # -- 左栏：待处理文件 --
        self._build_left(body)

        # -- 右栏：格式卡片 + 高级设置（CollapsibleCards）--
        self._build_right(body)

        # 底部按钮
        bar = QHBoxLayout()
        bar.addStretch(1)
        self.cancelBtn = ghost_btn(tr("convert.setup.cancel"), icon=FIF.CLOSE)
        self.cancelBtn.clicked.connect(self.reject)
        self.confirmBtn = primary_btn(tr("convert.setup.confirm"), icon=FIF.UP)
        self.confirmBtn.clicked.connect(self._on_confirm)
        bar.addWidget(self.cancelBtn)
        bar.addWidget(self.confirmBtn)
        root.addLayout(bar)

    def _build_left(self, body):
        left_card = ThemedCard(self)
        left_card.setFixedWidth(320)
        left_lay = QVBoxLayout(left_card)
        left_lay.setContentsMargins(14, 14, 10, 14)
        left_lay.setSpacing(8)

        self.stagingTitle = QLabel(tr("convert.setup.staging"))
        apply_text(self.stagingTitle, tokens.TEXT_STRONG, weight=700)
        left_lay.addWidget(self.stagingTitle)
        self.stagingCount = QLabel("")
        apply_text(self.stagingCount, muted_text(), size=tokens.FONT_SMALL)
        left_lay.addWidget(self.stagingCount)
        self.stagingScroll = QScrollArea()
        self.stagingScroll.setWidgetResizable(True)
        self.stagingScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        apply_plain_scroll(self.stagingScroll, radius=tokens.RADIUS_MD)
        self.stagingScroll.viewport().setStyleSheet(
            f"background: {surface().name()}; border-radius: {tokens.RADIUS_MD}px;"
        )
        self.stagingList = QWidget()
        self.stagingLayout = QVBoxLayout(self.stagingList)
        self.stagingLayout.setContentsMargins(0, 0, 0, 0)
        self.stagingLayout.setSpacing(4)
        self.stagingLayout.addStretch(1)
        self.stagingScroll.setWidget(self.stagingList)
        left_lay.addWidget(self.stagingScroll, 1)
        # V0.8.19 优化3：清空是次要操作，与三大模块队列清空按钮一致用 ghost 样式
        self.clearBtn = ghost_btn(tr("convert.clear"))
        self.clearBtn.clicked.connect(self._clear_all)
        left_lay.addWidget(self.clearBtn)
        body.addWidget(left_card)

    def _build_right(self, body):
        right_wrap = QWidget()
        right_lay = QVBoxLayout(right_wrap)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(12)

        # ---- 选择目标格式（2×3 网格）----
        # ODD-02：原来写成
        # fmt_card, fmt_vb, _ = self._fmt_card = CollapsibleCard(...), None, None
        # 链式赋值让 self._fmt_card 拿到的是整个三元组而不是卡片本身，而 fmt_vb
        # 从头到尾没人用。self._fmt_card 也没有任何读取方，直接去掉。
        fmt_card = CollapsibleCard(tr("convert.setup.format"), "", self, collapsed=False)
        fmt_card.body.setSpacing(10)
        self._fmt_list = self._load_formats(self._category)
        self._fmt_btns = {}
        default = self._selection.get(self._category, "").upper()
        # 格式按钮固定三列铺开；行数由 addWidget 的 i // cols 自然决定，不需要预先算。
        cols = 3
        self.fmtGrid = QGridLayout()
        self.fmtGrid.setSpacing(10)
        for i, fmt in enumerate(self._fmt_list):
            btn = QPushButton(f".{fmt}")
            btn.setCheckable(True)
            btn.setChecked(fmt == default)
            btn.setStyleSheet(_FMT_CARD_CSS)
            btn.clicked.connect(lambda checked, f=fmt: self._select_fmt(f))
            self._fmt_btns[fmt] = btn
            self.fmtGrid.addWidget(btn, i // cols, i % cols)
        fmt_card.body.addLayout(self.fmtGrid)
        right_lay.addWidget(fmt_card)

        # ---- 高级设置（：SwitchButton 替换折叠箭头）----
        adv_card = CollapsibleCard(tr("convert.setup.advanced"), "", self, collapsed=True)
        adv_card.body.setSpacing(8)

        # 隐藏原始折叠箭头，用 SwitchButton 替换
        # v0.8.0 ODD-07：改走 CollapsibleCard 的公开 API，不再直接摸它的
        # _toggleBtn / _bar 两个私有件，也不再硬编码「插到第 2 格」这个魔数。
        adv_card.hide_toggle()
        self.advMasterSwitch = SwitchButton()
        self.advMasterSwitch.setChecked(False)
        self.advMasterSwitch.setText(" ")
        self.advMasterSwitch.checkedChanged.connect(self._on_adv_master)
        self.advMasterSwitch.checkedChanged.connect(lambda: self.advMasterSwitch.setText(" "))
        adv_card.add_header_widget(self.advMasterSwitch)

        self.advancedPanel = AdvancedPanel(self)
        self.advancedPanel.refresh([self._category])
        # F3：弹窗首次打开即按默认目标格式禁用不匹配的压缩程序
        # （例如默认 .jpg 时应禁用 oxipng），避免用户看到全部可选但实际不可用。
        self.advancedPanel.on_format_change(default)
        # 视频 → 传文件上下文，动态生成「分辨率」选项
        if self._category == "video" and self._paths:
            self.advancedPanel.set_video_context(self._paths)
        adv_card.body.addWidget(self.advancedPanel)
        right_lay.addWidget(adv_card)
        self._adv_card = adv_card

        right_lay.addStretch(1)

        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setWidget(right_wrap)
        apply_plain_scroll(right_scroll)
        apply_transparent(right_scroll.viewport())
        body.addWidget(right_scroll, 1)

    def _load_formats(self, cat):
        from ..core.presets import TARGET_GROUPS

        return [f.upper() for f in TARGET_GROUPS.get(cat, [])]

    def _select_fmt(self, fmt):
        for f, btn in self._fmt_btns.items():
            btn.setChecked(f == fmt)
        # 格式切换时自动调整推荐后端
        self.advancedPanel.on_format_change(fmt)

    def _on_adv_master(self, checked: bool):
        """高级设置总开关：ON=展开+启用压缩，OFF=折叠+不压缩。

        v0.7.4：必须通过 setCollapsed 切换，否则 _collapsed 标志不会被更新，
        v0.7.3 的 _on_anim_finished 会在展开动画结束后把它重新收起。
        v0.7.5 修复：参数方向写反了（setCollapsed(checked) → 开关打开反而
        折叠）。正确语义是 checked=True → collapsed=False。
        """
        self._adv_card.setCollapsed(not checked)

    # -- 待处理列表 --
    def _render_staging(self):
        while self.stagingLayout.count():
            item = self.stagingLayout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not self._paths:
            empty = QLabel(tr("convert.setup.empty"))
            apply_text(empty, muted_text(), extra="padding: 30px 0;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.stagingLayout.insertWidget(0, empty)
            self.stagingLayout.addStretch(1)
            self.stagingCount.setText("")
            return
        self.stagingCount.setText(tr("convert.staging.count", n=len(self._paths)))
        acc = accent_color().name()
        for i, p in enumerate(self._paths):
            row_w = QWidget()
            row_w.setStyleSheet(tokens.staging_row_qss(i % 2 == 0))
            hb = QHBoxLayout(row_w)
            hb.setContentsMargins(8, 5, 4, 5)
            hb.setSpacing(8)
            ext = Path(p).suffix.upper().lstrip(".")
            ext_lbl = QLabel(ext)
            ext_lbl.setFixedWidth(42)
            ext_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ext_lbl.setStyleSheet(tokens.ext_badge_qss(acc))
            name = QLabel(Path(p).name)
            apply_text(name, tokens.TEXT_SUBTLE)
            hb.addWidget(ext_lbl)
            hb.addWidget(name, 1)
            rm = icon_btn(FIF.DELETE)
            rm.setFixedSize(26, 26)
            rm.clicked.connect(lambda _, path=p: self._remove(path))
            hb.addWidget(rm)
            self.stagingLayout.insertWidget(self.stagingLayout.count() - 1, row_w)
        self.stagingLayout.addStretch(1)

    def _remove(self, path):
        if path in self._paths:
            self._paths.remove(path)
        self._render_staging()
        self._update_confirm()
        self._sync_video_context()

    def _clear_all(self):
        self._paths.clear()
        self._render_staging()
        self._update_confirm()
        self._sync_video_context()

    def _sync_video_context(self):
        """v0.7.18：staging 文件增减后刷新视频「分辨率」选项。"""
        if self._category == "video" and hasattr(self, "advancedPanel"):
            self.advancedPanel.set_video_context(self._paths)

    def _update_confirm(self):
        self.confirmBtn.setEnabled(not getattr(self, "_loading", False) and bool(self._paths))

    def add_paths(self, paths: list[str]) -> None:
        """v0.7.24：追加待处理文件（供快速调用异步载入）。"""
        for p in paths:
            if p not in self._paths:
                self._paths.append(p)
        self._render_staging()
        self._update_confirm()
        self._sync_video_context()

    def set_loading(self, loading: bool) -> None:
        """v0.7.24：载入中 → 禁用确认按钮并显示黄色「载入中」。"""
        self._loading = loading
        if not hasattr(self, "_confirm_ss") or not self._confirm_ss:
            self._confirm_ss = self.confirmBtn.styleSheet() or ""
        if loading:
            self.confirmBtn.setEnabled(False)
            self.confirmBtn.setText(tr("quick.loading"))
            self.confirmBtn.setStyleSheet(tokens.warning_button_qss())
        else:
            self.confirmBtn.setText(tr("convert.setup.confirm"))
            self.confirmBtn.setStyleSheet(self._confirm_ss)
            self.confirmBtn.setEnabled(bool(self._paths))

    def _on_confirm(self):
        if not self._paths:
            return
        mode = cfg.outputMode.value
        suffix = cfg.outputSuffix.value
        folder = cfg.outputFolder.value or ""
        fmt_text = ""
        for f, btn in self._fmt_btns.items():
            if btn.isChecked():
                fmt_text = f.lower()
                break
        if not fmt_text:
            fmt_text = self._fmt_list[0].lower() if self._fmt_list else "jpg"
        gpu = self._gpu()
        self.manager.add_files(
            self._paths,
            fmt_text,
            folder if mode == "fixed" else None,
            gpu,
            mode,
            suffix,
            compress_enabled=self.advMasterSwitch.isChecked(),
        )
        self.accept()

    def get_selection(self):
        for f, btn in self._fmt_btns.items():
            if btn.isChecked():
                return {self._category: f.lower()}
        return {}
