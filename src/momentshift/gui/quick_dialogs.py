"""快速调用设置弹窗（v0.7.16 左右分栏）。

- 900×750，左侧「待处理文件队列」，右侧「压缩/放大设置」
- 右侧设置区超出高度时可滚动，内容顶置（不居中）
- 设置卡片 reparent 自大组件「压缩/放大」，参数与主窗口完全一致（含输出位置）
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

    def add_files(self, files: list[str]) -> None:
        """v0.7.24：追加文件（供快速调用异步载入）。"""
        for f in files:
            if f not in self._paths:
                self._paths.append(f)
        self._render()

    def paths(self) -> list[str]:
        return list(self._paths)


class _SettingsEmbed(QWidget):
    """承载 reparent 过来的大组件设置卡片（顶置不居中）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._vb = QVBoxLayout(self)
        self._vb.setContentsMargins(0, 0, 0, 0)
        self._vb.setSpacing(0)

    def embed(self, card) -> None:
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
        # 顶置：卡片之后留弹性空间
        self._vb.addStretch(1)


# --------------------------------------------------------------------------
class _QuickTaskDialog(QDialog):
    """快速调用设置弹窗公共骨架：左待处理文件 / 右设置（可滚动、顶置）。"""

    _DIALOG_W = 900
    _DIALOG_H = 630   # v0.7.18：压缩/放大窗口统一 900×630
    _LEFT_W = 300

    def __init__(self, parent, files, title_key, settings_title_key, on_confirm):
        super().__init__(parent)
        self._on_confirm = on_confirm
        self.setWindowTitle(tr(title_key))
        self.resize(self._DIALOG_W, self._DIALOG_H)
        self.setMinimumSize(760, 600)
        self.setObjectName("quickDlg")
        self.setStyleSheet(f"#quickDlg {{ background-color: {surface().name()}; }}")
        self._build_ui(files, title_key, settings_title_key)
        self._loading = False   # v0.7.24：异步载入状态
        self._update_confirm_enabled()

    def _build_ui(self, files, title_key, settings_title_key):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # 标题
        title = QLabel(tr(title_key))
        title.setStyleSheet("font-size: 17px; font-weight: 700; color: #1a1a1a;")
        root.addWidget(title)

        # 左右分栏
        body = QHBoxLayout()
        body.setSpacing(16)

        # ===== 左：待处理文件队列 =====
        left = QWidget()
        left.setFixedWidth(self._LEFT_W)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(8)
        left_title = QLabel(tr("quick.files_to_process"))
        left_title.setStyleSheet("font-size: 13px; font-weight: 600; color: #333;")
        lv.addWidget(left_title)
        self.staging = _StagingList(files)
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet(
            f"QScrollArea{{border:none;background:transparent;}}"
            f" {scrollbar_qss()}")
        left_scroll.setWidget(self.staging)
        lv.addWidget(left_scroll, 1)
        body.addWidget(left)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(f"color: {muted_text()};")
        body.addWidget(sep)

        # ===== 右：设置（可滚动 + 顶置）=====
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)
        right_title = QLabel(tr(settings_title_key))
        right_title.setStyleSheet("font-size: 13px; font-weight: 600; color: #333;")
        rv.addWidget(right_title)

        self.embedHost = _SettingsEmbed()
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setStyleSheet(
            f"QScrollArea{{border:none;background:transparent;}}"
            f" {scrollbar_qss()}")
        right_scroll.setWidget(self.embedHost)
        rv.addWidget(right_scroll, 1)
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
        self._confirm_ss = self.confirmBtn.styleSheet() or ""

    def add_paths(self, paths: list[str]) -> None:
        """v0.7.24：异步追加待处理文件。"""
        self.staging.add_files(paths)
        self._update_confirm_enabled()

    def set_loading(self, loading: bool) -> None:
        """v0.7.24：载入中 → 禁用并显示黄色「载入中」。"""
        self._loading = loading
        if loading:
            self.confirmBtn.setEnabled(False)
            self.confirmBtn.setText(tr("quick.loading"))
            self.confirmBtn.setStyleSheet(
                "QPushButton{background:#C7920A;color:#FFFFFF;border:none;"
                "border-radius:6px;padding:0 24px;font-weight:600;}")
        else:
            self.confirmBtn.setText(tr("quick.confirm"))
            self.confirmBtn.setStyleSheet(self._confirm_ss)
            self._update_confirm_enabled()

    def _update_confirm_enabled(self):
        self.confirmBtn.setEnabled(
            not getattr(self, "_loading", False) and bool(self.staging.paths()))

    def _embed_settings(self, card):
        if card is not None:
            self.embedHost.embed(card)

    def _confirm(self):
        self._on_confirm(self.staging.paths(), self.iface)
        self.accept()


class QuickCompressDialog(_QuickTaskDialog):
    """创建图片压缩任务设置弹窗（v0.7.16 左右分栏）。

    压缩设置卡片 reparent 自 CompressInterface，参数与主窗口完全一致。
    v0.7.18：移除「自动选择」条目及 auto 专属 UI，默认 Pillow。
    """

    def __init__(self, parent, files, on_confirm):
        from .compress_interface import CompressInterface
        self.iface = CompressInterface(None)
        self._postprocess_settings()
        super().__init__(parent, files, "quick.compress.title",
                         "compress.settings.title", on_confirm)
        self._embed_settings(getattr(self.iface, "_settingsCard", None))

    def _postprocess_settings(self):
        """v0.7.18：移除 auto 条目、auto 专属 UI；默认 Pillow。"""
        iface = self.iface
        combo = getattr(iface, "programCombo", None)
        if combo is not None:
            mapping = dict(getattr(combo, "_mapping", {}) or {})
            # 移除「自动选择」
            for disp, val in list(mapping.items()):
                if val == "auto":
                    idx = combo.findText(disp)
                    if idx >= 0:
                        combo.removeItem(idx)
                    mapping.pop(disp, None)
                    break
            # 默认 Pillow（setCurrentText 触发 _on_program → 只显示 Pillow 参数组）
            pill_disp = next((d for d, v in mapping.items() if v == "pillow"), None)
            if pill_disp:
                try:
                    combo.setCurrentText(pill_disp)
                except Exception:
                    pass
        # 隐藏 auto 专属路由提示（仅 auto 模式显示）
        hint = getattr(iface, "_route_hint", None)
        if hint is not None:
            try:
                hint.hide()
            except Exception:
                pass


class QuickUpscaleDialog(_QuickTaskDialog):
    """创建图片放大任务设置弹窗（v0.7.16 左右分栏）。

    放大设置卡片 reparent 自 UpscaleInterface，参数与主窗口完全一致。
    """

    def __init__(self, parent, files, on_confirm):
        from .upscale_interface import UpscaleInterface
        self.iface = UpscaleInterface(None)
        super().__init__(parent, files, "quick.upscale.title",
                         "upscale.settings.title", on_confirm)
        self._embed_settings(getattr(self.iface, "_settingsCard", None))
