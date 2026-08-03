"""快速调用设置弹窗，左右分栏布局。

职责边界：
- 做：为 --quick 右键菜单入口提供轻量弹窗，内嵌压缩/放大界面直接开干。
- 不做：不重复实现压缩与放大逻辑，只是把完整界面塞进弹窗复用。

依赖：core/logger、gui/compress_interface、gui/theme、gui/upscale_interface、i18n/translator；被依赖：quick_runner。

- 900×750，左侧「待处理文件队列」，右侧「压缩/放大设置」
- 右侧设置区超出高度时可滚动，内容顶置（不居中）
- 设置卡片 reparent 自大组件「压缩/放大」，参数与主窗口完全一致（含输出位置）
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from ..core.logger import get_logger
from ..i18n.translator import tr
from . import tokens
from .base import combo_mapping
from .theme import (
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

log = get_logger("quick_dialogs")


class _StagingList(QWidget):
    """待处理文件列表（v0.7.15：参考「转换设置-图片」窗口 staging 风格）。

    每行 = 后缀徽标 + 文件名 + 删除按钮，斑马纹背景。
    """

    def __init__(self, files: list[str], parent=None, removable: bool = True):
        super().__init__(parent)
        self._paths = list(files)
        apply_transparent(self)
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
            apply_text(empty, muted_text(), extra="padding: 24px 0;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._vb.addWidget(empty)
            return
        acc = accent_color().name()
        for i, p in enumerate(self._paths):
            row_w = QWidget()
            row_w.setStyleSheet(tokens.staging_row_qss(i % 2 == 0))
            hb = QHBoxLayout(row_w)
            hb.setContentsMargins(8, 5, 4, 5)
            hb.setSpacing(8)
            ext = Path(p).suffix.upper().lstrip(".")
            ext_lbl = QLabel(ext or "?")
            ext_lbl.setFixedWidth(42)
            ext_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ext_lbl.setStyleSheet(tokens.ext_badge_qss(acc))
            name = QLabel(Path(p).name)
            apply_text(name, tokens.TEXT_SUBTLE, transparent=True)
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
                log.debug("移除卡片布局失败，忽略")  # 静默原因：reparent 阶段卡片可能已销毁
        if hasattr(card, "setCollapsed"):
            try:
                card.setCollapsed(False)
            except Exception:
                log.debug("展开卡片失败，忽略")  # 静默原因：卡片可能已销毁
        card.setParent(self)
        self._vb.addWidget(card)
        # 顶置：卡片之后留弹性空间
        self._vb.addStretch(1)


# --------------------------------------------------------------------------
class _QuickTaskDialog(QDialog):
    """快速调用设置弹窗公共骨架：左待处理文件 / 右设置（可滚动、顶置）。"""

    _DIALOG_W = 900
    _DIALOG_H = 630  # 压缩/放大窗口统一 900×630
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
        self._loading = False  # 异步载入状态
        self._update_confirm_enabled()

    def _build_ui(self, files, title_key, settings_title_key):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # 标题
        title = QLabel(tr(title_key))
        apply_text(title, tokens.TEXT_TITLE, size=tokens.FONT_DIALOG_TITLE, weight=700)
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
        apply_text(left_title, tokens.TEXT_SUBTLE, size=tokens.FONT_BODY, weight=600)
        lv.addWidget(left_title)
        self.staging = _StagingList(files)
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setWidget(self.staging)
        lv.addWidget(left_scroll, 1)
        body.addWidget(left)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        apply_text(sep, muted_text())
        body.addWidget(sep)

        # ===== 右：设置（可滚动 + 顶置）=====
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)
        right_title = QLabel(tr(settings_title_key))
        apply_text(right_title, tokens.TEXT_SUBTLE, size=tokens.FONT_BODY, weight=600)
        rv.addWidget(right_title)

        self.embedHost = _SettingsEmbed()
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setWidget(self.embedHost)
        rv.addWidget(right_scroll, 1)
        body.addWidget(right, 1)

        # 左右两个滚动区外观一致，一次应用
        apply_plain_scroll(left_scroll, right_scroll)

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
            self.confirmBtn.setStyleSheet(tokens.warning_button_qss())
        else:
            self.confirmBtn.setText(tr("quick.confirm"))
            self.confirmBtn.setStyleSheet(self._confirm_ss)
            self._update_confirm_enabled()

    def _update_confirm_enabled(self):
        self.confirmBtn.setEnabled(
            not getattr(self, "_loading", False) and bool(self.staging.paths())
        )

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
        super().__init__(
            parent, files, "quick.compress.title", "compress.settings.title", on_confirm
        )
        self._embed_settings(getattr(self.iface, "_settingsCard", None))

    def _postprocess_settings(self):
        """v0.7.18：移除 auto 条目、auto 专属 UI；默认 Pillow。"""
        iface = self.iface
        combo = getattr(iface, "programCombo", None)
        if combo is not None:
            # ODD-07：改走 gui/base 的公开 API，不再直接摸 combo._mapping。
            # 拿到的是拷贝，下面的裁剪只影响本地副本 —— 与改造前一致：
            # 候选项已从控件里 removeItem，映射里那条残留取不到也就不影响。
            mapping = combo_mapping(combo)
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
                    log.debug("恢复格式下拉文本失败，忽略")  # 静默原因：控件可能已销毁
        # 隐藏 auto 专属路由提示（仅 auto 模式显示）
        hint = getattr(iface, "_route_hint", None)
        if hint is not None:
            try:
                hint.hide()
            except Exception:
                log.debug("隐藏提示失败，忽略")  # 静默原因：控件可能已销毁


class QuickUpscaleDialog(_QuickTaskDialog):
    """创建图片放大任务设置弹窗（v0.7.16 左右分栏）。

    放大设置卡片 reparent 自 UpscaleInterface，参数与主窗口完全一致。
    """

    def __init__(self, parent, files, on_confirm):
        from .upscale_interface import UpscaleInterface

        self.iface = UpscaleInterface(None)
        super().__init__(parent, files, "quick.upscale.title", "upscale.settings.title", on_confirm)
        self._embed_settings(getattr(self.iface, "_settingsCard", None))
