"""快速调用设置弹窗（v0.7.15 重构）。

- 「待处理文件」参考「转换设置-图片」窗口的 staging 列表风格
- 「压缩/放大设置」直接嵌入大组件「压缩/放大」的压缩/放大设置 UI 组件
  （构造对应 Interface，reparent 其设置卡片，完全复用同一份代码与参数）
- 输出位置已包含在设置卡片内，不再单独放置
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QDialog, QScrollArea,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)
from ..i18n.translator import tr
from .theme import (
    primary_btn, ghost_btn, muted_text, accent_color,
    surface, scrollbar_qss, icon_btn,
)


class _StagingList(QWidget):
    """待处理文件列表（v0.7.15：参考「转换设置-图片」窗口 staging 风格）。

    每行 = 后缀徽标 + 文件名 + 删除按钮，斑马纹背景。
    """

    def __init__(self, files: list[str], parent=None, removable: bool = True):
        super().__init__(parent)
        self._paths = list(files)
        self.setStyleSheet("background: transparent;")
        self._vb = QVBoxLayout(self)
        self._vb.setContentsMargins(0, 0, 0, 0)
        self._vb.setSpacing(4)
        self._removable = removable
        self._render()

    def _render(self):
        # 清空
        while self._vb.count():
            item = self._vb.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not self._paths:
            empty = QLabel(tr("convert.setup.empty"))
            empty.setStyleSheet(f"color: {muted_text()}; padding: 24px 0;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._vb.addWidget(empty)
            return
        acc = accent_color().name()
        for i, p in enumerate(self._paths):
            row_w = QWidget()
            row_w.setStyleSheet(
                "background: rgba(35,134,54,0.04); border-radius: 4px;"
                if i % 2 == 0 else "background: transparent; border-radius: 4px;")
            hb = QHBoxLayout(row_w)
            hb.setContentsMargins(8, 5, 4, 5)
            hb.setSpacing(8)
            ext = Path(p).suffix.upper().lstrip(".")
            ext_lbl = QLabel(ext or "?")
            ext_lbl.setFixedWidth(42)
            ext_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ext_lbl.setStyleSheet(
                f"color: {acc}; font-weight: 700; font-size: 11px;"
                f" background: rgba(35,134,54,0.08); border-radius: 3px;"
                " padding: 1px 4px;")
            name = QLabel(Path(p).name)
            name.setStyleSheet("color: #333; background: transparent;")
            hb.addWidget(ext_lbl)
            hb.addWidget(name, 1)
            if self._removable:
                rm = icon_btn(FIF.DELETE)
                rm.setFixedSize(26, 26)
                rm.clicked.connect(lambda _, path=p: self._remove(path))
                hb.addWidget(rm)
            self._vb.addWidget(row_w)
        self._vb.addStretch(1)

    def _remove(self, path):
        if path in self._paths:
            self._paths.remove(path)
        self._render()

    def paths(self) -> list[str]:
        return list(self._paths)


class _SettingsEmbed(QWidget):
    """承载 reparent 过来的大组件设置卡片。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._vb = QVBoxLayout(self)
        self._vb.setContentsMargins(0, 0, 0, 0)
        self._vb.setSpacing(0)

    def embed(self, card) -> None:
        """把设置卡片从源 Interface 布局中移除并嵌入本容器。"""
        parent_iface = card.parentWidget()
        if parent_iface is not None:
            try:
                parent_iface.vbox.removeWidget(card)
            except Exception:
                pass
        if hasattr(card, "setCollapsed"):
            try:
                card.setCollapsed(False)
            except Exception:
                pass
        card.setParent(self)
        self._vb.addWidget(card)


# --------------------------------------------------------------------------
class QuickCompressDialog(QDialog):
    """创建图片压缩任务设置弹窗（v0.7.15）。

    直接构造 CompressInterface 并 reparent 其「压缩设置」卡片到本窗口，
    参数与主窗口「压缩」页完全一致（含输出位置）。
    """

    def __init__(self, parent, files: list[str], on_confirm):
        super().__init__(parent)
        self._on_confirm = on_confirm
        self.setWindowTitle(tr("quick.compress.title"))
        self.resize(780, 640)
        self.setMinimumSize(640, 520)
        self.setObjectName("quickDlg")
        self.setStyleSheet(f"#quickDlg {{ background-color: {surface().name()}; }}")

        # 构造压缩界面实例（仅取设置卡片，不显示）
        from .compress_interface import CompressInterface
        self.iface = CompressInterface(None)

        self._build_ui(files)
        self._embed_settings()

    def _build_ui(self, files):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        title = QLabel(tr("quick.compress.title"))
        title.setStyleSheet("font-size: 17px; font-weight: 700; color: #1a1a1a;")
        root.addWidget(title)

        # 待处理文件
        files_label = QLabel(tr("quick.files_to_process"))
        files_label.setStyleSheet("font-size: 13px; font-weight: 600; color: #333;")
        root.addWidget(files_label)
        self.staging = _StagingList(files)
        staging_scroll = QScrollArea()
        staging_scroll.setWidgetResizable(True)
        staging_scroll.setFixedHeight(150)
        staging_scroll.setStyleSheet(
            f"QScrollArea{{border:none;background:transparent;}}"
            f" {scrollbar_qss()}")
        staging_scroll.setWidget(self.staging)
        root.addWidget(staging_scroll)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {muted_text()};")
        root.addWidget(sep)

        # 设置卡片容器（reparent 的压缩设置卡片放这里）
        self.embedHost = _SettingsEmbed()
        root.addWidget(self.embedHost, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = ghost_btn(tr("quick.cancel"))
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.confirmBtn = primary_btn(tr("quick.confirm"))
        self.confirmBtn.clicked.connect(self._confirm)
        btns.addWidget(self.confirmBtn)
        root.addLayout(btns)

    def _embed_settings(self):
        card = getattr(self.iface, "_settingsCard", None)
        if card is not None:
            self.embedHost.embed(card)

    def _confirm(self):
        self._on_confirm(self.staging.paths(), self.iface)
        self.accept()


# --------------------------------------------------------------------------
class QuickUpscaleDialog(QDialog):
    """创建图片放大任务设置弹窗（v0.7.15）。

    直接构造 UpscaleInterface 并 reparent 其「放大设置」卡片到本窗口，
    参数与主窗口「放大」页完全一致（含输出位置）。
    """

    def __init__(self, parent, files: list[str], on_confirm):
        super().__init__(parent)
        self._on_confirm = on_confirm
        self.setWindowTitle(tr("quick.upscale.title"))
        self.resize(780, 640)
        self.setMinimumSize(640, 520)
        self.setObjectName("quickDlg")
        self.setStyleSheet(f"#quickDlg {{ background-color: {surface().name()}; }}")

        from .upscale_interface import UpscaleInterface
        self.iface = UpscaleInterface(None)

        self._build_ui(files)
        self._embed_settings()

    def _build_ui(self, files):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        title = QLabel(tr("quick.upscale.title"))
        title.setStyleSheet("font-size: 17px; font-weight: 700; color: #1a1a1a;")
        root.addWidget(title)

        files_label = QLabel(tr("quick.files_to_process"))
        files_label.setStyleSheet("font-size: 13px; font-weight: 600; color: #333;")
        root.addWidget(files_label)
        self.staging = _StagingList(files)
        staging_scroll = QScrollArea()
        staging_scroll.setWidgetResizable(True)
        staging_scroll.setFixedHeight(150)
        staging_scroll.setStyleSheet(
            f"QScrollArea{{border:none;background:transparent;}}"
            f" {scrollbar_qss()}")
        staging_scroll.setWidget(self.staging)
        root.addWidget(staging_scroll)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {muted_text()};")
        root.addWidget(sep)

        self.embedHost = _SettingsEmbed()
        root.addWidget(self.embedHost, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = ghost_btn(tr("quick.cancel"))
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.confirmBtn = primary_btn(tr("quick.confirm"))
        self.confirmBtn.clicked.connect(self._confirm)
        btns.addWidget(self.confirmBtn)
        root.addLayout(btns)

    def _embed_settings(self):
        card = getattr(self.iface, "_settingsCard", None)
        if card is not None:
            self.embedHost.embed(card)

    def _confirm(self):
        self._on_confirm(self.staging.paths(), self.iface)
        self.accept()
