"""快速调用设置弹窗（v0.7.9 快启2）。

根据任务类型弹出对应的设置窗口：
- compress → 压缩设置（后端/质量/输出位置）
- upscale  → 放大设置（模型/输出位置/格式）
Convert 复用现有 ConvertSetupDialog。
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QDialog, QPushButton, QComboBox, QLineEdit,
)
from qfluentwidgets import (
    FluentIcon as FIF, SwitchButton, ComboBox, BodyLabel,
)
from ..core.config import cfg
from ..i18n.translator import tr
from .theme import (
    ThemedCard, primary_btn, ghost_btn, muted_text, accent_color,
    surface, field_row, CollapsibleCard,
)
from .advanced_panel import AdvancedPanel
from .drop_area import DropArea
from ..core import engines as eng_mod
from ..core.config import tools_dir


# --------------------------------------------------------------------------
class _FileListWidget(QWidget):
    """待处理文件列表。"""

    def __init__(self, files: list[str], parent=None):
        super().__init__(parent)
        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(4)
        for f in files:
            lbl = QLabel(Path(f).name)
            lbl.setStyleSheet("color: #1a1a1a; padding: 2px 0;")
            vb.addWidget(lbl)


# --------------------------------------------------------------------------
class QuickCompressDialog(QDialog):
    """创建图片压缩任务设置弹窗。"""

    def __init__(self, parent, files: list[str], on_confirm):
        super().__init__(parent)
        self._files = files
        self._on_confirm = on_confirm
        self._accepted = False

        self.setWindowTitle(tr("quick.compress.title"))
        self.resize(720, 560)
        self.setMinimumSize(560, 400)
        self.setStyleSheet(f"background-color: {surface().name()};")

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # 标题
        title = QLabel(tr("quick.compress.title"))
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #1a1a1a;")
        root.addWidget(title)

        # 文件列表
        root.addWidget(QLabel(tr("quick.files_to_process")))
        fl = _FileListWidget(files)
        root.addWidget(fl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {muted_text()};")
        root.addWidget(sep)

        # 压缩设置
        self.backendCombo = ComboBox()
        self.backendCombo.addItem(tr("compress.backend.auto"), "auto")
        self.backendCombo.addItem(tr("compress.backend.oxipng"), "oxipng")
        self.backendCombo.addItem(tr("compress.backend.jpegoptim"), "jpegoptim")
        self.backendCombo.addItem(tr("compress.backend.pillow"), "pillow")
        self.backendCombo.setCurrentIndex(0)
        root.addWidget(field_row(tr("compress.backend"), self.backendCombo))

        self.qualityCombo = ComboBox()
        self.qualityCombo.addItem(tr("compress.quality.lossless"), "lossless")
        self.qualityCombo.addItem(tr("compress.quality.high"), "high")
        self.qualityCombo.addItem(tr("compress.quality.medium"), "medium")
        self.qualityCombo.addItem(tr("compress.quality.low"), "low")
        self.qualityCombo.setCurrentIndex(1)
        root.addWidget(field_row(tr("compress.quality"), self.qualityCombo))

        # 输出位置
        self.outputSwitch = SwitchButton()
        self.outputSwitch.setChecked(cfg.compressMode.value == "same")
        self.outputSwitch.checkedChanged.connect(self._on_output_mode)
        root.addWidget(field_row(tr("compress.output.mode"), self.outputSwitch))

        self.folderEdit = QLineEdit(cfg.compressFolder.value or "")
        self.folderEdit.setPlaceholderText(tr("compress.output.folder_hint"))
        self.folderEdit.setVisible(cfg.compressMode.value != "same")
        root.addWidget(self.folderEdit)

        root.addStretch(1)

        # 按钮
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = ghost_btn(tr("quick.cancel"))
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        confirm = primary_btn(tr("quick.confirm"))
        confirm.clicked.connect(self._confirm)
        btns.addWidget(confirm)
        root.addLayout(btns)

    def _on_output_mode(self, checked):
        self.folderEdit.setVisible(not checked)

    def _confirm(self):
        mode = "same" if self.outputSwitch.isChecked() else "fixed"
        quality = self.qualityCombo.currentData()
        backend = self.backendCombo.currentData()
        folder = self.folderEdit.text().strip()
        self._accepted = True
        self._on_confirm(self._files, quality, backend, mode, folder)
        self.accept()

    def get_settings(self):
        return {
            "quality": self.qualityCombo.currentData(),
            "backend": self.backendCombo.currentData(),
            "mode": "same" if self.outputSwitch.isChecked() else "fixed",
            "folder": self.folderEdit.text().strip(),
        }


# --------------------------------------------------------------------------
class QuickUpscaleDialog(QDialog):
    """创建图片放大任务设置弹窗。"""

    def __init__(self, parent, files: list[str], on_confirm):
        super().__init__(parent)
        self._files = files
        self._on_confirm = on_confirm
        self._accepted = False

        self.setWindowTitle(tr("quick.upscale.title"))
        self.resize(720, 560)
        self.setMinimumSize(560, 400)
        self.setStyleSheet(f"background-color: {surface().name()};")

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # 标题
        title = QLabel(tr("quick.upscale.title"))
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #1a1a1a;")
        root.addWidget(title)

        # 文件列表
        root.addWidget(QLabel(tr("quick.files_to_process")))
        fl = _FileListWidget(files)
        root.addWidget(fl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {muted_text()};")
        root.addWidget(sep)

        # 放大模型（只列已安装引擎）
        self.modelCombo = ComboBox()
        self._engine_map = {}
        installed = eng_mod.installed_engines()
        for e in installed:
            label = f"{e.name}  ·  {'/'.join(e.algos)}"
            self.modelCombo.addItem(label)
            self._engine_map[label] = e.eid
        if not installed:
            self.modelCombo.addItem(tr("upscale.engine.none"))
            self.modelCombo.setEnabled(False)
        root.addWidget(field_row(tr("upscale.model"), self.modelCombo))

        # 输出格式（仅静态图片）
        self.fmtCombo = ComboBox()
        self.fmtCombo.addItem("PNG", "png")
        self.fmtCombo.addItem("JPG", "jpg")
        self.fmtCombo.addItem("WEBP", "webp")
        self.fmtCombo.setCurrentIndex(0)
        root.addWidget(field_row(tr("upscale.output.fmt"), self.fmtCombo))

        # 输出位置
        self.outputSwitch = SwitchButton()
        self.outputSwitch.setChecked(cfg.upscaleMode.value == "same")
        self.outputSwitch.checkedChanged.connect(self._on_output_mode)
        root.addWidget(field_row(tr("upscale.output.mode"), self.outputSwitch))

        self.folderEdit = QLineEdit(cfg.upscaleFolder.value or "")
        self.folderEdit.setPlaceholderText(tr("upscale.output.folder_hint"))
        self.folderEdit.setVisible(cfg.upscaleMode.value != "same")
        root.addWidget(self.folderEdit)

        root.addStretch(1)

        # 按钮
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = ghost_btn(tr("quick.cancel"))
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        confirm = primary_btn(tr("quick.confirm"))
        confirm.clicked.connect(self._confirm)
        btns.addWidget(confirm)
        root.addLayout(btns)

    def _on_output_mode(self, checked):
        self.folderEdit.setVisible(not checked)

    def _confirm(self):
        label = self.modelCombo.currentText()
        engine_id = self._engine_map.get(label, "")
        fmt = self.fmtCombo.currentData()
        mode = "same" if self.outputSwitch.isChecked() else "fixed"
        folder = self.folderEdit.text().strip()
        self._accepted = True
        self._on_confirm(self._files, engine_id, fmt, mode, folder)
        self.accept()

    def get_settings(self):
        label = self.modelCombo.currentText()
        return {
            "engine_id": self._engine_map.get(label, ""),
            "fmt": self.fmtCombo.currentData(),
            "mode": "same" if self.outputSwitch.isChecked() else "fixed",
            "folder": self.folderEdit.text().strip(),
        }
