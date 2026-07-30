"""转换设置弹窗（v0.3.3 重构）。
- 按媒体大类分类弹出独立窗口
- 900×700 大窗口
- 待处理列表美颜 + 清空按钮
- 高级设置顶部加"转换后压缩"总开关
"""

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame,
    QDialog, QPushButton,
)
from qfluentwidgets import (
    FluentIcon as FIF, ComboBox, SwitchButton,
)
from ..core.config import cfg
from ..core.presets import guess_category
from ..i18n.translator import tr
from .theme import (
    ThemedCard, primary_btn, ghost_btn, icon_btn,
    muted_text, accent_name, surface, scrollbar_qss,
    accent_color,
)
from .advanced_panel import AdvancedPanel

class ConvertSetupDialog(QDialog):
    """单类别格式 + 高级选��� + 待处理列表。"""

    def __init__(self, parent, manager, paths, selection, gpu_fn, category):
        super().__init__(parent)
        self.manager = manager
        self._paths = list(paths)
        self._selection = dict(selection)
        self._gpu = gpu_fn
        self._category = category  # "image" / "audio" / "video"

        cat_names = {"image": tr("category.image"), "audio": tr("category.audio"), "video": tr("category.video")}
        cat_disp = cat_names.get(category, category)

        self.setWindowTitle(f"{tr('convert.setup.title')} — {cat_disp}")
        self.resize(900, 700)
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

        # 标题行
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

        # 左右分栏
        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        # --- 左栏：待处理文件（美颜重设计）---
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

        # 清空按钮
        self.clearBtn = QPushButton(tr("convert.clear"))
        self.clearBtn.clicked.connect(self._clear_all)
        self.clearBtn.setStyleSheet(
            f"QPushButton{{ color: {muted_text()}; border: 1px solid #ddd; border-radius: 6px;"
            f" padding: 6px 16px; background: transparent; }}"
            f"QPushButton:hover{{ color: #d32f2f; border-color: #d32f2f; }}")
        left_lay.addWidget(self.clearBtn)

        body.addWidget(left_card)

        # --- 右栏：格式 + 高级设置 ---
        right_card = ThemedCard(self)
        right_lay = QVBoxLayout(right_card)
        right_lay.setContentsMargins(16, 14, 16, 14)
        right_lay.setSpacing(10)

        # 目标格式选择
        fmt_title = QLabel(tr("convert.setup.format"))
        fmt_title.setStyleSheet("font-weight: 700; color: #212121;")
        right_lay.addWidget(fmt_title)

        self.fmtCombo = ComboBox()
        self._fmt_map = self._load_formats(self._category)
        for disp in self._fmt_map:
            self.fmtCombo.addItem(disp)
        default = self._selection.get(self._category, "jpg")
        if default.upper() in self._fmt_map:
            self.fmtCombo.setCurrentText(default.upper())
        right_lay.addWidget(self.fmtCombo)

        # 高级设置
        adv_title = QLabel(tr("convert.setup.advanced"))
        adv_title.setStyleSheet("font-weight: 700; color: #212121; margin-top: 8px;")
        right_lay.addWidget(adv_title)

        # 转换后自动压缩开关（新增）
        self.postCompressSwitch = SwitchButton(tr("convert.post_compress"))
        self.postCompressSwitch.setChecked(False)
        self.postCompressSwitch.setOnText(tr("common.on"))
        self.postCompressSwitch.setOffText(tr("common.off"))
        right_lay.addWidget(self.postCompressSwitch)

        self.advancedPanel = AdvancedPanel(self)
        self.advancedPanel.refresh([self._category])
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
        fmts = TARGET_GROUPS.get(cat, [])
        return [f.upper() for f in fmts]

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
            # 交替行背景
            if i % 2 == 0:
                row_w.setStyleSheet(f"background: rgba(35,134,54,0.04); border-radius: 4px;")
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
        fmt_text = self.fmtCombo.currentText().lower()
        gpu = self._gpu()

        self.manager.add_files(
            self._paths, fmt_text,
            folder if mode == "fixed" else None,
            gpu, mode, suffix,
        )
        self.accept()

    def get_selection(self):
        fmt = self.fmtCombo.currentText().lower()
        return {self._category: fmt}
