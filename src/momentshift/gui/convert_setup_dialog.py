"""转换设置弹窗（v0.3.4 美化）。
- 方形格式卡片按钮替代 ComboBox
- 清空按钮 → 绿底白字
- 高级设置开关右对齐，默认关闭
"""

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame,
    QDialog, QPushButton,
)
from qfluentwidgets import (
    FluentIcon as FIF, SwitchButton, FlowLayout,
)
from ..core.config import cfg
from ..i18n.translator import tr
from .theme import (
    ThemedCard, primary_btn, ghost_btn, icon_btn,
    muted_text, accent_name, surface, scrollbar_qss,
    accent_color,
)
from .advanced_panel import AdvancedPanel

# 格式卡片按钮样式
_FMT_CARD_CSS = (
    "QPushButton{{"
    "  background: #f5f5f5; border: 1px solid #ddd; border-radius: 8px;"
    "  color: #333; font-weight: 700; font-size: 13px;"
    "  min-width: 70px; min-height: 54px;"
    "}}"
    "QPushButton:hover{{ border-color: #238636; background: #effff2; }}"
    "QPushButton:checked{{"
    "  border-color: #238636; background: #238636; color: #fff;"
    "}}"
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

    # -- 构建 UI -----------------------------------------------------------
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

        # --- 左栏：待处理文件 ---
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

        # 清空按钮（v0.3.4：绿底白字）
        self.clearBtn = QPushButton(tr("convert.clear"))
        self.clearBtn.clicked.connect(self._clear_all)
        self.clearBtn.setStyleSheet(
            "QPushButton{"
            "  background: #238636; color: #FFFFFF; border: none; border-radius: 8px;"
            "  padding: 8px 20px; font-weight: 600; font-size: 13px;"
            "}"
            "QPushButton:hover{ background: #2ea043; }"
            "QPushButton:pressed{ background: #196c2e; }")
        left_lay.addWidget(self.clearBtn)
        body.addWidget(left_card)

        # --- 右栏：格式 + 高级设置 ---
        right_card = ThemedCard(self)
        right_lay = QVBoxLayout(right_card)
        right_lay.setContentsMargins(16, 14, 16, 14)
        right_lay.setSpacing(10)

        # 目标格式（v0.3.4：方形卡片按钮替代 ComboBox）
        fmt_title = QLabel(tr("convert.setup.format"))
        fmt_title.setStyleSheet("font-weight: 700; color: #212121;")
        right_lay.addWidget(fmt_title)

        self.fmtGrid = FlowLayout()
        self.fmtGrid.setSpacing(8)
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
        right_lay.addWidget(fmt_w)

        # 高级设置（v0.3.4：开关右对齐，默认关闭）
        adv_row = QHBoxLayout()
        adv_title = QLabel(tr("convert.setup.advanced"))
        adv_title.setStyleSheet("font-weight: 700; color: #212121;")
        adv_row.addWidget(adv_title, 1)
        adv_row.addStretch()
        self.advSwitch = SwitchButton()
        self.advSwitch.setChecked(False)
        self.advSwitch.setOnText(tr("common.on"))
        self.advSwitch.setOffText(tr("common.off"))
        self.advSwitch.checkedChanged.connect(self._on_adv_toggle)
        adv_row.addWidget(self.advSwitch)
        right_lay.addLayout(adv_row)

        # 转换后自动压缩开关
        self.postCompressSwitch = SwitchButton(tr("convert.post_compress"))
        self.postCompressSwitch.setChecked(False)
        self.postCompressSwitch.setOnText(tr("common.on"))
        self.postCompressSwitch.setOffText(tr("common.off"))
        right_lay.addWidget(self.postCompressSwitch)

        self.advancedPanel = AdvancedPanel(self)
        self.advancedPanel.refresh([self._category])
        self.advancedPanel.hide()  # 默认关闭
        right_lay.addWidget(self.advancedPanel, 1)

        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setWidget(right_card)
        right_scroll.setStyleSheet(
            f"QScrollArea{{ border: none; background: transparent; }} {scrollbar_qss()}")
        right_scroll.viewport().setStyleSheet("background: transparent;")
        body.addWidget(right_scroll, 1)

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

    def _load_formats(self, cat):
        from ..core.presets import TARGET_GROUPS
        return [f.upper() for f in TARGET_GROUPS.get(cat, [])]

    def _select_fmt(self, fmt):
        for f, btn in self._fmt_btns.items():
            btn.setChecked(f == fmt)

    def _on_adv_toggle(self, checked):
        self.advancedPanel.setVisible(checked)

    # -- 待处理列表 ----------------------------------------------------------
    def _render_staging(self):
        while self.stagingLayout.count():
            item = self.stagingLayout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
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
            if i % 2 == 0:
                row_w.setStyleSheet("background: rgba(35,134,54,0.04); border-radius: 4px;")
            else:
                row_w.setStyleSheet("background: transparent; border-radius: 4px;")
            hb = QHBoxLayout(row_w)
            hb.setContentsMargins(8, 5, 4, 5)
            hb.setSpacing(8)

            ext = Path(p).suffix.upper().lstrip(".")
            ext_lbl = QLabel(ext)
            ext_lbl.setFixedWidth(42)
            ext_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ext_lbl.setStyleSheet(
                f"color: {acc}; font-weight: 700; font-size: 11px;"
                f" background: rgba(35,134,54,0.08); border-radius: 3px; padding: 1px 4px;")

            name = QLabel(Path(p).name)
            name.setStyleSheet("color: #333;")
            name.setToolTip(p)
            hb.addWidget(ext_lbl)
            hb.addWidget(name, 1)

            rm = icon_btn(FIF.DELETE, tr("convert.action.remove"))
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

    def _clear_all(self):
        self._paths.clear()
        self._render_staging()
        self._update_confirm()

    def _update_confirm(self):
        self.confirmBtn.setEnabled(bool(self._paths))

    # -- 确认 ---------------------------------------------------------------
    def _on_confirm(self):
        if not self._paths:
            return
        mode = cfg.outputMode.value
        suffix = cfg.outputSuffix.value
        folder = cfg.outputFolder.value or ""
        # 从格式按钮中获取当前选中的格式
        fmt_text = ""
        for f, btn in self._fmt_btns.items():
            if btn.isChecked():
                fmt_text = f.lower()
                break
        if not fmt_text:
            fmt_text = self._fmt_list[0].lower() if self._fmt_list else "jpg"
        gpu = self._gpu()

        self.manager.add_files(
            self._paths, fmt_text,
            folder if mode == "fixed" else None,
            gpu, mode, suffix,
        )
        self.accept()

    def get_selection(self):
        for f, btn in self._fmt_btns.items():
            if btn.isChecked():
                return {self._category: f.lower()}
        return {}
