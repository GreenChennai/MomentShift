"""快速调用设置弹窗（v0.7.12 重建）。

参照「转换设置-图片/音频/视频」窗口风格：
- 左侧：待处理文件列表
- 右侧：压缩/放大参数设置卡片 + 输出位置卡片（可修改，默认读大模块配置）
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QDialog, QPushButton, QLineEdit, QScrollArea, QFileDialog,
)
from qfluentwidgets import (
    FluentIcon as FIF, SwitchButton, ComboBox,
)
from ..core.config import cfg
from ..i18n.translator import tr
from .theme import (
    ThemedCard, primary_btn, ghost_btn, muted_text, accent_color,
    surface, field_row, CollapsibleCard, scrollbar_qss, CARD_MARGIN,
    icon_btn,
)


# --------------------------------------------------------------------------
class _FileListCard(ThemedCard):
    """待处理文件列表卡片。"""

    def __init__(self, files: list[str], parent=None):
        super().__init__(parent)
        vb = QVBoxLayout(self)
        vb.setContentsMargins(CARD_MARGIN, 12, CARD_MARGIN, 12)
        vb.setSpacing(6)
        title = QLabel(f"{tr('quick.files_to_process')}  ({len(files)})")
        title.setStyleSheet("font-size: 14px; font-weight: 700; color: #1a1a1a;")
        vb.addWidget(title)
        for f in files[:8]:
            row = QLabel(f"· {Path(f).name}")
            row.setStyleSheet(
                "color: #444; background: rgba(35,134,54,0.05);"
                " border-radius: 4px; padding: 3px 8px;")
            vb.addWidget(row)
        if len(files) > 8:
            more = QLabel(f"+{len(files) - 8} …")
            more.setStyleSheet(f"color: {muted_text()}; font-size: 11px;")
            vb.addWidget(more)


class _OutputCard(CollapsibleCard):
    """输出位置卡片：same/fixed 开关 + 文件夹选择。"""

    def __init__(self, parent, init_mode: str, init_folder: str,
                 mode_title: str, folder_hint: str):
        super().__init__(mode_title, "", parent, collapsed=False)
        self._folder = init_folder or ""
        body = self.body
        body.setSpacing(8)

        # same 开关：ON=与源相同目录，OFF=自定义文件夹
        self.sameSwitch = SwitchButton()
        self.sameSwitch.setChecked(init_mode == "same")
        self.sameSwitch.checkedChanged.connect(self._on_mode)
        body.addWidget(field_row(mode_title, self.sameSwitch))

        # 文件夹选择行
        fr = QHBoxLayout()
        fr.setSpacing(8)
        self.folderEdit = QLineEdit(self._folder)
        self.folderEdit.setPlaceholderText(folder_hint)
        self.folderEdit.setEnabled(init_mode != "same")
        fr.addWidget(self.folderEdit, 1)
        pick = icon_btn(FIF.FOLDER)
        pick.clicked.connect(self._pick_folder)
        fr.addWidget(pick)
        body.addLayout(fr)

    def _on_mode(self, checked: bool):
        self.folderEdit.setEnabled(not checked)

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(
            self.window(), tr("quick.choose_folder"), self.folderEdit.text() or "")
        if d:
            self.folderEdit.setText(d)

    def value(self) -> tuple[str, str]:
        """返回 (mode, folder)。"""
        if self.sameSwitch.isChecked():
            return "same", ""
        return "fixed", self.folderEdit.text().strip()


# --------------------------------------------------------------------------
class QuickCompressDialog(QDialog):
    """创建图片压缩任务设置弹窗（v0.7.12）。"""

    def __init__(self, parent, files: list[str], on_confirm):
        super().__init__(parent)
        self._files = files
        self._on_confirm = on_confirm
        self.setWindowTitle(tr("quick.compress.title"))
        self.resize(760, 560)
        self.setMinimumSize(620, 440)
        self.setObjectName("quickDlg")
        self.setStyleSheet(f"#quickDlg {{ background-color: {surface().name()}; }}")
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        title = QLabel(tr("quick.compress.title"))
        title.setStyleSheet("font-size: 17px; font-weight: 700; color: #1a1a1a;")
        root.addWidget(title)

        body = QHBoxLayout()
        body.setSpacing(16)

        # 左：文件列表
        left = QScrollArea()
        left.setWidgetResizable(True)
        left.setFixedWidth(250)
        left.setStyleSheet(f"QScrollArea{{border:none;background:transparent;}}"
                           f" {scrollbar_qss()}")
        left.setWidget(_FileListCard(self._files))
        body.addWidget(left)

        # 右：设置
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(10)

        # 压缩参数
        self.cfgCard = CollapsibleCard(
            tr("compress.settings.title"), "", right, collapsed=False)
        cb = self.cfgCard.body
        cb.setSpacing(8)

        self.backendCombo = ComboBox()
        self.backendCombo.addItem(tr("advanced.compression.auto"), None, "auto")
        self.backendCombo.addItem(tr("advanced.compression.oxipng"), None, "oxipng")
        self.backendCombo.addItem(tr("advanced.compression.jpegoptim"), None, "jpegoptim")
        self.backendCombo.addItem(tr("advanced.compression.pillow"), None, "pillow")
        cb.addWidget(field_row(tr("compress.backend"), self.backendCombo))

        self.modeCombo = ComboBox()
        self.modeCombo.addItem(tr("compress.mode.lossless"), None, "lossless")
        self.modeCombo.addItem(tr("compress.mode.lossy"), None, "lossy")
        if cfg.compressMode.value == "lossy":
            self.modeCombo.setCurrentIndex(1)
        cb.addWidget(field_row(tr("compress.mode"), self.modeCombo))
        rv.addWidget(self.cfgCard)

        # 输出位置
        self.outputCard = _OutputCard(
            right,
            cfg.compressOutMode.value if hasattr(cfg, "compressOutMode")
            else cfg.compressMode.value,
            cfg.compressFolder.value or "",
            tr("compress.output.mode"),
            tr("quick.folder_hint"))
        rv.addWidget(self.outputCard)

        rv.addStretch(1)
        body.addWidget(right, 1)
        root.addLayout(body, 1)

        # 底部按钮
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = ghost_btn(tr("quick.cancel"))
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.confirmBtn = primary_btn(tr("quick.confirm"))
        self.confirmBtn.clicked.connect(self._confirm)
        btns.addWidget(self.confirmBtn)
        root.addLayout(btns)

    def _confirm(self):
        settings = {
            "backend": self.backendCombo.currentData(),
            "mode": self.modeCombo.currentData(),
        }
        mode, folder = self.outputCard.value()
        settings["output_mode"] = mode
        settings["folder"] = folder
        self._on_confirm(self._files, settings)
        self.accept()


# --------------------------------------------------------------------------
class QuickUpscaleDialog(QDialog):
    """创建图片放大任务设置弹窗（v0.7.12）。"""

    def __init__(self, parent, files: list[str], on_confirm):
        super().__init__(parent)
        self._files = files
        self._on_confirm = on_confirm
        self.setWindowTitle(tr("quick.upscale.title"))
        self.resize(760, 560)
        self.setMinimumSize(620, 440)
        self.setObjectName("quickDlg")
        self.setStyleSheet(f"#quickDlg {{ background-color: {surface().name()}; }}")
        self._build_ui()

    def _build_ui(self):
        from ..core import engines as eng_mod

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        title = QLabel(tr("quick.upscale.title"))
        title.setStyleSheet("font-size: 17px; font-weight: 700; color: #1a1a1a;")
        root.addWidget(title)

        body = QHBoxLayout()
        body.setSpacing(16)

        left = QScrollArea()
        left.setWidgetResizable(True)
        left.setFixedWidth(250)
        left.setStyleSheet(f"QScrollArea{{border:none;background:transparent;}}"
                           f" {scrollbar_qss()}")
        left.setWidget(_FileListCard(self._files))
        body.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(10)

        # 放大参数
        self.cfgCard = CollapsibleCard(
            tr("upscale.settings.title"), "", right, collapsed=False)
        cb = self.cfgCard.body
        cb.setSpacing(8)

        self.modelCombo = ComboBox()
        self._engine_map = {}
        installed = eng_mod.installed_engines()
        for e in installed:
            label = f"{e.name}  ·  {'/'.join(e.algos)}"
            label = label if len(label) <= 32 else label[:31] + "…"
            self.modelCombo.addItem(label)
            self._engine_map[label] = e.eid
        if not installed:
            self.modelCombo.addItem(tr("upscale.engine.none"))
            self.modelCombo.setEnabled(False)
        cb.addWidget(field_row(tr("upscale.model"), self.modelCombo))

        self.fmtCombo = ComboBox()
        self.fmtCombo.addItem("PNG", None, "png")
        self.fmtCombo.addItem("JPG", None, "jpg")
        self.fmtCombo.addItem("WEBP", None, "webp")
        cb.addWidget(field_row(tr("upscale.output.fmt"), self.fmtCombo))
        rv.addWidget(self.cfgCard)

        # 输出位置
        self.outputCard = _OutputCard(
            right,
            cfg.upscaleMode.value,
            cfg.upscaleFolder.value or "",
            tr("upscale.output.mode"),
            tr("quick.folder_hint"))
        rv.addWidget(self.outputCard)

        rv.addStretch(1)
        body.addWidget(right, 1)
        root.addLayout(body, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = ghost_btn(tr("quick.cancel"))
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.confirmBtn = primary_btn(tr("quick.confirm"))
        self.confirmBtn.clicked.connect(self._confirm)
        btns.addWidget(self.confirmBtn)
        root.addLayout(btns)

    def _confirm(self):
        label = self.modelCombo.currentText()
        settings = {
            "engine_id": self._engine_map.get(label, ""),
            "fmt": self.fmtCombo.currentData(),
        }
        mode, folder = self.outputCard.value()
        settings["output_mode"] = mode
        settings["folder"] = folder
        self._on_confirm(self._files, settings)
        self.accept()
