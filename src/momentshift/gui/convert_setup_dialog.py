"""转换设置弹窗（v0.3.5 卡片化 + 正方形格式按钮 + 开关合并）。

- 选择目标格式 → CollapsibleCard（可折叠）
- 高级设置 → CollapsibleCard（折叠按钮即总开关）
- 格式卡片：正方形、放大、浅绿默认/深绿选中 + 描边
"""

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame,
    QDialog, QPushButton,
)
from qfluentwidgets import (
    FluentIcon as FIF, FlowLayout, SwitchButton,
)
from ..core.config import cfg
from ..i18n.translator import tr
from .theme import (
    ThemedCard, primary_btn, ghost_btn, icon_btn,
    muted_text, accent_name, surface, scrollbar_qss,
    accent_color, CollapsibleCard,
)
from .advanced_panel import AdvancedPanel

# 格式卡片按钮样式（v0.3.7：75×75 正方形）
_FMT_CARD_CSS = (
    "QPushButton{"
    "  background: #e8f5e9; border: 2px solid #c8e6c9; border-radius: 10px;"
    "  color: #2e7d32; font-weight: 700; font-size: 16px;"
    "  min-width: 75px; min-height: 75px; max-width: 75px; max-height: 75px;"
    "}"
    "QPushButton:hover{ background: #c8e6c9; border-color: #238636; }"
    "QPushButton:checked{"
    "  border-color: #238636; border-width: 2px; background: #238636;"
    "  color: #fff;"
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

        cat_names = {"image": tr("category.image"), "audio": tr("category.audio"),
                     "video": tr("category.video")}
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
        title.setStyleSheet("font-size: 18px; font-weight: 700; color: #1a1a1a;")
        root.addWidget(title)
        hint = QLabel(tr("convert.setup.hint"))
        hint.setStyleSheet(f"color: {muted_text()};")
        root.addWidget(hint)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {muted_text()};")
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
        self.stagingTitle.setStyleSheet("font-weight: 700; color: #212121;")
        left_lay.addWidget(self.stagingTitle)
        self.stagingCount = QLabel("")
        self.stagingCount.setStyleSheet(f"color: {muted_text()}; font-size: 12px;")
        left_lay.addWidget(self.stagingCount)
        self.stagingScroll = QScrollArea()
        self.stagingScroll.setWidgetResizable(True)
        self.stagingScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stagingScroll.setStyleSheet(
            f"QScrollArea{{ border: none; background: transparent; border-radius: 8px; }} {scrollbar_qss()}")
        self.stagingScroll.viewport().setStyleSheet(
            f"background: {surface().name()}; border-radius: 8px;")
        self.stagingList = QWidget()
        self.stagingLayout = QVBoxLayout(self.stagingList)
        self.stagingLayout.setContentsMargins(0, 0, 0, 0)
        self.stagingLayout.setSpacing(4)
        self.stagingLayout.addStretch(1)
        self.stagingScroll.setWidget(self.stagingList)
        left_lay.addWidget(self.stagingScroll, 1)
        self.clearBtn = QPushButton(tr("convert.clear"))
        self.clearBtn.clicked.connect(self._clear_all)
        self.clearBtn.setStyleSheet(
            "QPushButton{ background: #238636; color: #FFFFFF; border: none; border-radius: 8px;"
            " padding: 8px 20px; font-weight: 600; font-size: 13px; }"
            "QPushButton:hover{ background: #2ea043; }"
            "QPushButton:pressed{ background: #196c2e; }")
        left_lay.addWidget(self.clearBtn)
        body.addWidget(left_card)

    def _build_right(self, body):
        right_wrap = QWidget()
        right_lay = QVBoxLayout(right_wrap)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(12)

        # ---- 选择目标格式（CollapsibleCard）----
        fmt_card, fmt_vb, _ = self._fmt_card = CollapsibleCard(
            tr("convert.setup.format"), "", self, collapsed=False), None, None
        fmt_card.body.setSpacing(10)
        self.fmtGrid = FlowLayout()
        self.fmtGrid.setSpacing(10)
        self._fmt_list = self._load_formats(self._category)
        self._fmt_btns = {}
        default = self._selection.get(self._category, "").upper()
        for fmt in self._fmt_list:
            btn = QPushButton(f".{fmt}")
            btn.setCheckable(True)
            btn.setChecked(fmt == default)
            btn.setStyleSheet(_FMT_CARD_CSS)
            btn.clicked.connect(lambda checked, f=fmt: self._select_fmt(f))
            self._fmt_btns[fmt] = btn
            self.fmtGrid.addWidget(btn)
        fmt_w = QWidget()
        fmt_w.setLayout(self.fmtGrid)
        fmt_card.body.addWidget(fmt_w)
        right_lay.addWidget(fmt_card)

        # ---- 高级设置（v0.4.4：SwitchButton 替换折叠箭头）----
        adv_card = CollapsibleCard(tr("convert.setup.advanced"), "", self, collapsed=True)
        adv_card.body.setSpacing(8)

        # 隐藏原始折叠箭头，用 SwitchButton 替换
        adv_card._toggleBtn.hide()
        self.advMasterSwitch = SwitchButton()
        self.advMasterSwitch.setChecked(False)
        self.advMasterSwitch.checkedChanged.connect(self._on_adv_master)
        # 插入到 header bar 中（在标题和 stretch 之间）
        adv_card._bar.layout().insertWidget(2, self.advMasterSwitch)

        self.advancedPanel = AdvancedPanel(self)
        self.advancedPanel.refresh([self._category])
        adv_card.body.addWidget(self.advancedPanel)
        right_lay.addWidget(adv_card)
        self._adv_card = adv_card

        right_lay.addStretch(1)

        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setWidget(right_wrap)
        right_scroll.setStyleSheet(
            f"QScrollArea{{ border: none; background: transparent; }} {scrollbar_qss()}")
        right_scroll.viewport().setStyleSheet("background: transparent;")
        body.addWidget(right_scroll, 1)

    def _load_formats(self, cat):
        from ..core.presets import TARGET_GROUPS
        return [f.upper() for f in TARGET_GROUPS.get(cat, [])]

    def _select_fmt(self, fmt):
        for f, btn in self._fmt_btns.items():
            btn.setChecked(f == fmt)
        # v0.4.3：格式切换时自动调整推荐后端
        self.advancedPanel.on_format_change(fmt)

    def _on_adv_master(self, checked: bool):
        """高级设置总开关：ON=展开+启用，OFF=折叠+不使用。"""
        if checked:
            self._adv_card._apply_expanded()
        else:
            self._adv_card._apply_collapsed()

    # -- 待处理列表 --
    def _render_staging(self):
        while self.stagingLayout.count():
            item = self.stagingLayout.takeAt(0)
            w = item.widget()
            if w: w.deleteLater()
        if not self._paths:
            empty = QLabel(tr("convert.setup.empty"))
            empty.setStyleSheet(f"color: {muted_text()}; padding: 30px 0;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.stagingLayout.insertWidget(0, empty)
            self.stagingLayout.addStretch(1)
            self.stagingCount.setText("")
            return
        self.stagingCount.setText(tr("convert.staging.count", n=len(self._paths)))
        acc = accent_color().name()
        for i, p in enumerate(self._paths):
            row_w = QWidget()
            row_w.setStyleSheet(
                "background: rgba(35,134,54,0.04); border-radius: 4px;" if i % 2 == 0
                else "background: transparent; border-radius: 4px;")
            hb = QHBoxLayout(row_w)
            hb.setContentsMargins(8, 5, 4, 5); hb.setSpacing(8)
            ext = Path(p).suffix.upper().lstrip(".")
            ext_lbl = QLabel(ext)
            ext_lbl.setFixedWidth(42); ext_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ext_lbl.setStyleSheet(
                f"color: {acc}; font-weight: 700; font-size: 11px;"
                f" background: rgba(35,134,54,0.08); border-radius: 3px; padding: 1px 4px;")
            name = QLabel(Path(p).name)
            name.setStyleSheet("color: #333;"); name.setToolTip(p)
            hb.addWidget(ext_lbl); hb.addWidget(name, 1)
            rm = icon_btn(FIF.DELETE, tr("convert.action.remove"))
            rm.setFixedSize(26, 26)
            rm.clicked.connect(lambda _, path=p: self._remove(path))
            hb.addWidget(rm)
            self.stagingLayout.insertWidget(self.stagingLayout.count() - 1, row_w)
        self.stagingLayout.addStretch(1)

    def _remove(self, path):
        if path in self._paths: self._paths.remove(path)
        self._render_staging(); self._update_confirm()

    def _clear_all(self):
        self._paths.clear()
        self._render_staging(); self._update_confirm()

    def _update_confirm(self):
        self.confirmBtn.setEnabled(bool(self._paths))

    def _on_confirm(self):
        if not self._paths: return
        mode = cfg.outputMode.value
        suffix = cfg.outputSuffix.value
        folder = cfg.outputFolder.value or ""
        fmt_text = ""
        for f, btn in self._fmt_btns.items():
            if btn.isChecked(): fmt_text = f.lower(); break
        if not fmt_text: fmt_text = self._fmt_list[0].lower() if self._fmt_list else "jpg"
        gpu = self._gpu()
        self.manager.add_files(
            self._paths, fmt_text,
            folder if mode == "fixed" else None, gpu, mode, suffix)
        self.accept()

    def get_selection(self):
        for f, btn in self._fmt_btns.items():
            if btn.isChecked(): return {self._category: f.lower()}
        return {}
