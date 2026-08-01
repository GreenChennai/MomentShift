"""放大界面 —— 批量 AI 超分辨率放大 / 视频插帧。

v0.7.5 重构：不再硬编码 Real-ESRGAN，改为由 :mod:`momentshift.core.engines`
的引擎注册表驱动 ——
- 「放大模型」下拉只列出**已安装**的引擎（``tools/<engine-id>/`` 下检测到可执行文件）
- 一个引擎都没有时：下拉禁用并显示「无模型 / 算法可用，请下载」，其余设置项
  全部隐藏，只留一个「检测环境」按钮跳转到关于页
- 有引擎时：按该引擎的参数 schema **动态生成**设置行（模型 / 降噪 / 倍率 /
  分块 / GPU / TTA / 插帧倍率 …），不同引擎参数完全不同
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QFileDialog, QScrollArea,
    QLabel, QMessageBox, QProgressBar, QDoubleSpinBox, QSpinBox,
)
from PyQt6.QtCore import Qt, QTimer

from qfluentwidgets import (
    FluentIcon as FIF, PushButton, PrimaryPushButton, SwitchButton, ComboBox,
    CaptionLabel, StrongBodyLabel, BodyLabel, HyperlinkButton,
)

from ..core.config import cfg
from ..core import engines as eng_mod
from qfluentwidgets import qconfig
from ..core.qt_compat import Signal, QObject, QRunnable, QThreadPool
from ..i18n.translator import tr
from .theme import (
    ThemedCard, CollapsibleCard, field_row, primary_btn, ghost_btn, icon_btn,
    muted_text, sub_text, CARD_MARGIN, scrollbar_qss,
    success_color, danger_color, accent_color, border_color, ext_badge,
)
from .base import InterfaceBase
from .drop_area import DropArea
from .help_bubble import attach_help
from .queue_widget import ProgressBar, StatusPill, human_size, format_size_compare, ScrollAutoFollow, MarqueeName, FormatPill
from .compare_window import CompareWindow

# 放大模块支持的视频格式
_VIDEO_EXTS = set(eng_mod.VIDEO_EXTS)
# 放大模块总支持格式
_UPSCALE_EXTS = eng_mod.IMAGE_EXTS | eng_mod.ANIM_EXTS | _VIDEO_EXTS


def _opt_label(raw: str) -> str:
    """解析 schema 里的候选项文案。

    ``@key`` → ``tr(key)``；``Foo (@key)`` → ``Foo (译文)``；其余原样返回。
    """
    if not raw:
        return raw
    if raw.startswith("@"):
        return tr(raw[1:])
    if "(@" in raw and raw.endswith(")"):
        head, _, tail = raw.partition("(@")
        return f"{head.strip()} ({tr(tail[:-1])})"
    return raw


# =============================================================================
# 动态引擎参数面板
# =============================================================================
class EngineParamPanel(QWidget):
    """按引擎的参数 schema 动态生成设置行。"""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._vb = QVBoxLayout(self)
        self._vb.setContentsMargins(0, 0, 0, 0)
        self._vb.setSpacing(8)
        self._engine: eng_mod.Engine | None = None
        self._controls: dict[str, tuple] = {}   # key -> (kind, widget)
        self._rows: list[QWidget] = []

    # -- 构建 --
    def build(self, engine: eng_mod.Engine | None, values: dict | None = None) -> None:
        self._clear()
        self._engine = engine
        if engine is None:
            return
        values = values or {}
        for p in engine.params:
            widget = self._make_control(p, values.get(p.key, p.default))
            if widget is None:
                continue
            self._controls[p.key] = (p.kind, widget)
            row = field_row(tr(p.label_key), widget)
            self._rows.append(row)
            self._vb.addWidget(row)
            # v0.7.6 功能1：各参数附帮助说明（对齐压缩设置）
            attach_help(row, f"engine.help.{p.key}")

    def _make_control(self, p: eng_mod.Param, current):
        if p.kind == "choice":
            combo = ComboBox()
            mapping = {}
            idx = 0
            for i, (val, label) in enumerate(p.choices):
                text = _opt_label(label)
                combo.addItem(text)
                mapping[text] = val
                if val == current:
                    idx = i
            combo.setCurrentIndex(idx)
            combo._mapping = mapping
            combo.currentTextChanged.connect(lambda _t: self.changed.emit())
            return combo
        if p.kind == "bool":
            sw = SwitchButton()
            sw.setChecked(bool(current))
            sw.setText(" ")
            sw.checkedChanged.connect(lambda _c, s=sw: (s.setText(" "),
                                                        self.changed.emit()))
            return sw
        if p.kind == "float":
            spin = QDoubleSpinBox()
            spin.setRange(float(p.minimum), float(p.maximum))
            spin.setSingleStep(float(p.step))
            spin.setDecimals(2)
            spin.setValue(float(current))
            spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
            spin.valueChanged.connect(lambda _v: self.changed.emit())
            return spin
        if p.kind == "int":
            spin = QSpinBox()
            spin.setRange(int(p.minimum), int(p.maximum))
            spin.setSingleStep(int(p.step) or 1)
            spin.setValue(int(current))
            spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            spin.valueChanged.connect(lambda _v: self.changed.emit())
            return spin
        return None

    def _clear(self):
        for row in self._rows:
            self._vb.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._controls.clear()

    # -- 取值 --
    def values(self) -> dict:
        out: dict = {}
        if self._engine is None:
            return out
        for p in self._engine.params:
            entry = self._controls.get(p.key)
            if entry is None:
                out[p.key] = p.default
                continue
            kind, w = entry
            if kind == "choice":
                out[p.key] = w._mapping.get(w.currentText(), p.default)
            elif kind == "bool":
                out[p.key] = w.isChecked()
            else:
                out[p.key] = w.value()
        return out

    def retranslateUi(self) -> None:
        """语言切换后整块重建（候选项文案也需要翻译）。"""
        if self._engine is not None:
            current = self.values()
            self.build(self._engine, current)


# =============================================================================
# 放大 Worker / 队列组件
# =============================================================================
class _WorkerSignals(QObject):
    progress = Signal(str, int)
    finished = Signal(str, bool, int, str)


class UpscaleWorker(QRunnable):
    """单个放大 / 插帧任务，在 QThreadPool 线程中执行。"""

    def __init__(self, item_id, src, out, engine_id, values):
        super().__init__()
        self.setAutoDelete(True)
        self.item_id = item_id
        self.src = src
        self.out = out
        self.engine_id = engine_id
        self.values = dict(values or {})
        self.signals = _WorkerSignals()

    def run(self):
        # v0.7.7 修复3：流式进度回调，进度条不再卡在 0
        cb = lambda p: self.signals.progress.emit(self.item_id, p)
        self.signals.progress.emit(self.item_id, 0)
        try:
            ok, detail = eng_mod.process_media(
                self.engine_id, self.src, self.out, self.values, progress_cb=cb)
        except Exception as exc:
            ok, detail = False, str(exc)
        if ok:
            self.signals.progress.emit(self.item_id, 100)
        saved = 0
        try:
            if ok and Path(self.out).exists():
                saved = Path(self.src).stat().st_size - Path(self.out).stat().st_size
        except OSError:
            saved = 0
        self.signals.finished.emit(self.item_id, ok, saved, detail)


class UpscaleItemWidget(ThemedCard):
    """放大队列中的单个任务卡片。

    v0.7.6 翻新：对齐「转换队列」视觉 —— 后缀徽标 + 8 字滚动文件名 +
    格式胶囊(.SRC → .TGT) + 状态胶囊 / 进度条 / 大小对比行 + 复制/对比/删除。
    """
    removeRequested = Signal(str)
    compareRequested = Signal(str)

    def __init__(self, item_id: str, src: str, out: str = "", parent=None):
        super().__init__(parent)
        self._id = item_id
        self._src = src
        self._out = out
        self._status = "pending"
        # v0.7.8 调整1：耗时计时
        self._start_time = None
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._on_elapsed_tick)

        vb = QVBoxLayout(self)
        vb.setContentsMargins(14, 12, 14, 12)
        vb.setSpacing(8)

        src_ext = Path(src).suffix.upper().lstrip(".")
        top = QHBoxLayout()
        # v0.7.4 Adj1：后缀矩形徽标（与转换/压缩队列统一风格）
        self.iconLbl = ext_badge(src_ext, self)
        top.addWidget(self.iconLbl)
        self.nameLbl = MarqueeName(self)
        self.nameLbl.set_text(Path(src).name)
        self.nameLbl.setObjectName("queueName")
        top.addWidget(self.nameLbl, 1)
        # v0.7.8 调整1：格式胶囊改为任务耗时显示
        self.timeLbl = QLabel(tr("upscale.elapsed.pending"))
        self.timeLbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timeLbl.setStyleSheet(
            "color:#F5F5F5; background:#3EB68F; border-radius:9px;"
            " padding:2px 9px; font-weight:600; font-size:11px;")
        top.addWidget(self.timeLbl)
        self.pill = StatusPill("pending")
        top.addWidget(self.pill)
        vb.addLayout(top)

        self.prog = ProgressBar()
        vb.addWidget(self.prog)

        bottom = QHBoxLayout()
        self.detailLbl = CaptionLabel()
        self.detailLbl.setStyleSheet("color: #000000; background: transparent;")
        bottom.addWidget(self.detailLbl, 1)
        self.copyBtn = icon_btn(FIF.COPY)
        self.copyBtn.clicked.connect(self._copy_path)
        bottom.addWidget(self.copyBtn)
        self.cmpBtn = icon_btn(FIF.SEARCH)
        self.cmpBtn.clicked.connect(lambda: self.compareRequested.emit(self._id))
        bottom.addWidget(self.cmpBtn)
        self.delBtn = icon_btn(FIF.DELETE)
        self.delBtn.clicked.connect(lambda: self.removeRequested.emit(self._id))
        bottom.addWidget(self.delBtn)
        vb.addLayout(bottom)

        self.set_status("pending")
        self.set_progress(0)

    def _copy_path(self):
        folder = str(Path(self._out or self._src).parent)
        QApplication.clipboard().setText(folder)

    def set_progress(self, pct: int):
        self.prog.set_value(pct)

    def set_status(self, status: str, saved: int = 0, detail: str = ""):
        self._status = status
        self.pill.set_status(status)
        self.prog.set_error(status == "failed")
        if status == "running":
            if self._start_time is None:
                import time as _time
                self._start_time = _time.monotonic()
            self._elapsed_timer.start()
            self.detailLbl.setText(tr("upscale.status.upscaling"))
        else:
            self._elapsed_timer.stop()
            if status in ("done", "failed"):
                self._update_elapsed_text()  # 定格最终耗时
        if status == "done":
            # v0.7.7 修复2+3：用 format_size_compare 显示绿/红百分比；进度条满格
            self.set_progress(100)
            src_size = Path(self._src).stat().st_size if Path(self._src).exists() else 0
            dst_size = src_size - saved
            self.detailLbl.setText(format_size_compare(src_size, dst_size))
        elif status == "failed":
            self.detailLbl.setText((detail or tr("convert.status.failed"))[:80])
        elif status not in ("running",):
            self.detailLbl.setText("")

    def _on_elapsed_tick(self):
        self._update_elapsed_text()

    def _update_elapsed_text(self):
        import time as _time
        secs = int(_time.monotonic() - (self._start_time or 0))
        m, s = divmod(max(0, secs), 60)
        self.timeLbl.setText(f"{tr('upscale.elapsed.prefix')} {m}:{s:02d}")

    def retranslate(self):
        self.pill.set_status(self._status)
        self.set_status(self._status)


class UpscaleListWidget(QWidget):
    """放大任务列表（带统计栏）。"""
    removeRequested = Signal(str)
    compareRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: dict[str, UpscaleItemWidget] = {}
        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(8)

        self.statsBar = QWidget()
        hb = QHBoxLayout(self.statsBar)
        hb.setContentsMargins(2, 0, 2, 0)
        hb.setSpacing(14)
        self.statTotal = CaptionLabel()
        self.statDone = CaptionLabel()
        self.statErr = CaptionLabel()
        for w in (self.statTotal, self.statDone, self.statErr):
            w.setStyleSheet("color: #000000; font-weight:600;")
            hb.addWidget(w)
        hb.addStretch(1)
        vb.addWidget(self.statsBar)

        self.listWidget = QWidget()
        self.listLayout = QVBoxLayout(self.listWidget)
        self.listLayout.setContentsMargins(0, 0, 0, 0)
        self.listLayout.setSpacing(8)
        self.listLayout.addStretch(1)
        vb.addWidget(self.listWidget, 1)
        self.emptyHint = CaptionLabel(tr("upscale.queue.empty"))
        self.emptyHint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyHint.setStyleSheet(f"color: {muted_text()}; padding: 24px 0;")
        vb.addWidget(self.emptyHint)
        self._refresh_empty()

    def _refresh_empty(self):
        self.emptyHint.setVisible(False)

    def _update_stats(self):
        total = len(self.items)
        done = sum(1 for w in self.items.values() if w._status == "done")
        failed = sum(1 for w in self.items.values() if w._status == "failed")
        self.statTotal.setText(tr("upscale.queue.stats.total", n=total))
        self.statDone.setText(tr("upscale.queue.stats.done", n=done))
        self.statErr.setText(tr("upscale.queue.stats.error", n=failed))

    def add_item(self, item_id: str, src: str, out: str = ""):
        if item_id in self.items:
            return
        w = UpscaleItemWidget(item_id, src, out)
        w.removeRequested.connect(self.removeRequested)
        w.compareRequested.connect(self.compareRequested)
        self.items[item_id] = w
        self.listLayout.insertWidget(self.listLayout.count() - 1, w)
        self._refresh_empty()
        self._update_stats()

    def set_progress(self, item_id: str, pct: int):
        w = self.items.get(item_id)
        if w:
            w.set_progress(pct)

    def set_status(self, item_id: str, status: str, saved: int = 0, detail: str = ""):
        w = self.items.get(item_id)
        if w:
            w.set_status(status, saved, detail)
            self._update_stats()

    def remove_item(self, item_id: str):
        w = self.items.pop(item_id, None)
        if w:
            w.deleteLater()
        self._refresh_empty()
        self._update_stats()

    def clear(self):
        for w in self.items.values():
            w.deleteLater()
        self.items.clear()
        self._refresh_empty()
        self._update_stats()

    def retranslate(self):
        for w in self.items.values():
            w.retranslate()
        self.emptyHint.setText(tr("upscale.queue.empty"))
        self._update_stats()


# =============================================================================
# 放大界面
# =============================================================================
class UpscaleInterface(InterfaceBase):
    """AI 超分辨率放大标签页。

    自管任务队列（QRunnable + QThreadPool），使用 Real-ESRGAN 引擎。
    媒体文件直传队列（无暂存步骤），全局设置驱动整队参数。
    """

    def __init__(self, parent=None):
        super().__init__("Upscale", tr("nav.upscale"), tr("upscale.tagline"), parent)

        self._items: dict[str, dict] = {}
        self._active: set[str] = set()
        self._pending: list[str] = []
        self._running = False
        self._paused = False
        # 重入防护（v0.3.0）：防止模态对话框事件循环触发二次弹框
        self._picking = False

        # 放大参数默认值（v0.7.5：引擎由注册表驱动）
        self._engine_id = ""
        self._fmt = "png"
        self._output_mode = cfg.upscaleMode.value
        self._suffix = cfg.upscaleSuffix.value
        self._folder = cfg.upscaleFolder.value or ""

        # =====================================================================
        # 输入卡片
        # =====================================================================
        card, vb, self.tInput = self._make_card("upscale.input.title")
        self.dropArea = DropArea(self)
        self.dropArea.filesDropped.connect(self._on_files)
        self.dropArea.clicked.connect(self._pick_files)
        vb.addWidget(self.dropArea)
        tools = QHBoxLayout()
        self.addFolderBtn = primary_btn(tr("upscale.add_folder"), icon=FIF.FOLDER_ADD)
        self.addFolderBtn.clicked.connect(self._pick_folder)
        tools.addWidget(self.addFolderBtn)
        vb.addLayout(tools)
        self.vbox.addWidget(card)
        self._inputCard = card

        # =====================================================================
        # 放大设置卡片（v0.7.5：引擎驱动的动态参数面板）
        # =====================================================================
        setc, setvb, self.tSettings = self._make_card("upscale.settings.title")

        # -- 「放大模型」：只列已安装的引擎 --
        self.modelCombo = ComboBox()
        # v0.7.9 修复5：不设固定最大宽度，让 field_row 决定（对齐下方设置条目）
        self.modelCombo.currentTextChanged.connect(self._on_engine_change)
        self.modelRow = field_row(tr("upscale.model"), self.modelCombo)
        setvb.addWidget(self.modelRow)

        # -- 引擎缺失时的提示 + 检测环境按钮 --
        self.noEngineBox = QWidget(self)
        self.noEngineBox.setStyleSheet("background: transparent;")
        nb = QVBoxLayout(self.noEngineBox)
        nb.setContentsMargins(0, 4, 0, 0)
        nb.setSpacing(10)
        self.noEngineHint = CaptionLabel(tr("upscale.engine.none_hint"))
        self.noEngineHint.setWordWrap(True)
        self.noEngineHint.setStyleSheet(
            f"color: {muted_text()}; background: transparent;")
        nb.addWidget(self.noEngineHint)
        self.detectBtn = primary_btn(tr("upscale.engine.detect"), icon=FIF.SEARCH)
        self.detectBtn.clicked.connect(self._goto_about)
        nb.addWidget(self.detectBtn)
        setvb.addWidget(self.noEngineBox)

        # -- 动态参数面板 --
        self.paramPanel = EngineParamPanel(self)
        setvb.addWidget(self.paramPanel)

        # -- 输出相关（引擎缺失时整体隐藏）--
        self.outputBox = QWidget(self)
        self.outputBox.setStyleSheet("background: transparent;")
        ob = QVBoxLayout(self.outputBox)
        ob.setContentsMargins(0, 0, 0, 0)
        ob.setSpacing(8)

        self.fmtCombo = self._make_combo(
            [(tr("upscale.fmt.png"), "png"), (tr("upscale.fmt.jpg"), "jpg"),
             (tr("upscale.fmt.webp"), "webp")], self._fmt,
            lambda v: setattr(self, "_fmt", v))
        self.fmtRow = field_row(tr("upscale.output.fmt"), self.fmtCombo)
        ob.addWidget(self.fmtRow)

        self.outputSwitch = SwitchButton()
        self.outputSwitch.checkedChanged.connect(self._on_output_mode)
        ob.addWidget(field_row(tr("upscale.output.mode"), self.outputSwitch))
        self.suffixEdit = QLineEdit(self._suffix)
        self.suffixEdit.setPlaceholderText(tr("upscale.output.suffix_hint"))
        self.suffixEdit.textChanged.connect(
            lambda t: (setattr(self, "_suffix", t),
                       setattr(cfg.upscaleSuffix, "value", t),
                       qconfig.save()))
        self.suffixRow = field_row(tr("upscale.output.suffix"), self.suffixEdit)
        ob.addWidget(self.suffixRow)
        self.folderEdit = QLineEdit(self._folder)
        self.folderEdit.setReadOnly(True)
        self.browseBtn = icon_btn(FIF.FOLDER)
        self.browseBtn.clicked.connect(self._pick_output)
        frow = QHBoxLayout()
        frow.addWidget(self.folderEdit, 1)
        frow.addWidget(self.browseBtn)
        self.folderRow = field_row(tr("upscale.output.folder"), frow)
        ob.addWidget(self.folderRow)
        setvb.addWidget(self.outputBox)

        self._apply_output_mode()
        self.vbox.addWidget(setc)

        # =====================================================================
        # 队列卡片
        # =====================================================================
        qcard, qvb, self.tQueue = self._make_card(
            "upscale.queue.title", "upscale.queue.hint")
        self.listWidget = UpscaleListWidget(self)
        self.listWidget.removeRequested.connect(self._on_remove)
        self.listWidget.compareRequested.connect(self._on_compare)
        self.queueScroll = self._make_scroll(280)
        self.queueScroll.setWidget(self.listWidget)
        qvb.addWidget(self.queueScroll)
        # v0.7.4 Adj2：队列自动跟随当前处理任务
        self._queue_auto_follow = ScrollAutoFollow(self.queueScroll)
        ctrl = QHBoxLayout()
        self.startBtn = primary_btn(tr("convert.start"), icon=FIF.PLAY)
        self.startBtn.clicked.connect(self._on_start)
        self.pauseBtn = ghost_btn(tr("convert.pause"), icon=FIF.PAUSE)
        self.pauseBtn.clicked.connect(self._on_pause)
        self.clearBtn = ghost_btn(tr("convert.clear"), icon=FIF.DELETE)
        self.clearBtn.clicked.connect(self._on_clear)
        ctrl.addWidget(self.startBtn, 1)
        ctrl.addWidget(self.pauseBtn)
        ctrl.addWidget(self.clearBtn)
        qvb.addLayout(ctrl)
        # 引擎扫描必须放在队列控制按钮之后：_update_controls 依赖 startBtn 等
        self.reload_engines()
        self.vbox.addWidget(qcard)

        # =====================================================================
        # 前后对比组件
        # =====================================================================

        self._update_controls()
        self.vbox.addStretch(1)
        self._collapse_ready = True
        self.retheme()

    # =========================================================================
    # 输入处理（文件直传队列，无暂存）
    # =========================================================================

    def _on_files(self, paths):
        self._add_to_queue(self._expand_paths(paths, _UPSCALE_EXTS))

    def _pick_files(self):
        """弹出媒体文件选择器（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            files = self._ask_open_files(tr("upscale.btn.add"), _UPSCALE_EXTS)
            if files:
                self._add_to_queue(self._expand_paths(files, _UPSCALE_EXTS))
        finally:
            self._picking = False

    def _pick_folder(self):
        """弹出文件夹选择器（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            d = self._ask_directory(tr("upscale.add_folder"))
            if d:
                self._add_to_queue(self._expand_paths([d], _UPSCALE_EXTS))
        finally:
            self._picking = False

    def _add_to_queue(self, paths):
        if not paths:
            return
        for p in paths:
            if p not in self._items:
                self._items[p] = {"src": p, "out": self._out_path(p),
                                  "status": "pending", "saved": 0}
                self.listWidget.add_item(p, p, self._items[p]["out"])
        self._update_controls()

    # =========================================================================
    # 引擎装载与切换（v0.7.5）
    # =========================================================================

    def reload_engines(self) -> None:
        """重新扫描 ``tools/`` 并重建「放大模型」下拉与参数面板。

        无任何引擎时：下拉禁用并显示「无模型 / 算法可用，请下载」，参数面板与
        输出设置全部隐藏，只保留「检测环境」按钮。
        """
        installed = eng_mod.installed_engines()
        self.modelCombo.blockSignals(True)
        self.modelCombo.clear()
        self._engine_map: dict[str, str] = {}

        if not installed:
            self._engine_id = ""
            self.modelCombo.addItem(tr("upscale.engine.none"))
            self.modelCombo.setEnabled(False)
            self.modelCombo.blockSignals(False)
            self.paramPanel.build(None)
            self.paramPanel.setVisible(False)
            self.outputBox.setVisible(False)
            self.noEngineBox.setVisible(True)
            self._update_controls()
            return

        for e in installed:
            full_label = f"{e.name}  ·  {'/'.join(e.algos)}"
            if e.is_interp:
                full_label = f"{full_label}  [{tr('engine.group.interp')}]"
            # v0.7.10：超出 32 字截断 + ...；全文经 tooltip 可见
            label = full_label if len(full_label) <= 32 else full_label[:31] + "…"
            self.modelCombo.addItem(label)
            self.modelCombo.setItemData(
                self.modelCombo.count() - 1, full_label, Qt.ItemDataRole.ToolTipRole)
            self._engine_map[label] = e.eid
        if self._engine_id not in [e.eid for e in installed]:
            self._engine_id = installed[0].eid
        for i, e in enumerate(installed):
            if e.eid == self._engine_id:
                self.modelCombo.setCurrentIndex(i)
                break
        self.modelCombo.setEnabled(True)
        self.modelCombo.blockSignals(False)

        self.noEngineBox.setVisible(False)
        self.paramPanel.setVisible(True)
        self.outputBox.setVisible(True)
        self._rebuild_params()
        self._update_controls()

    def _on_engine_change(self, text: str) -> None:
        eid = getattr(self, "_engine_map", {}).get(text)
        if not eid or eid == self._engine_id:
            return
        self._engine_id = eid
        self._rebuild_params()
        self._update_controls()

    def _rebuild_params(self) -> None:
        engine = eng_mod.ENGINE_BY_ID.get(self._engine_id)
        self.paramPanel.build(engine, eng_mod.default_values(self._engine_id))
        # 插帧引擎不吃静态图片，输出格式行没有意义
        self.fmtRow.setVisible(not (engine and engine.is_interp))

    def _goto_about(self) -> None:
        win = self.window()
        if hasattr(win, "goto_about"):
            win.goto_about()

    # =========================================================================
    # 设置交互
    # =========================================================================

    def _on_output_mode(self, checked):
        self._output_mode = "same" if checked else "fixed"
        cfg.upscaleMode.value = self._output_mode
        qconfig.save()
        self._apply_output_mode()

    def _apply_output_mode(self):
        same = self._output_mode == "same"
        self.outputSwitch.setChecked(same)
        self.outputSwitch.setText(
            tr("convert.output.same") if same else tr("convert.output.fixed"))
        self.suffixRow.setVisible(same)
        self.folderRow.setVisible(not same)

    def _pick_output(self):
        """浏览选择固定输出目录（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            d = self._ask_directory(tr("convert.output.browse"),
                                    self._folder or "")
            if d:
                self._folder = d
                cfg.upscaleFolder.value = d
                qconfig.save()
                self.folderEdit.setText(d)
        finally:
            self._picking = False

    def _out_path(self, src: str) -> str:
        p = Path(src)
        src_ext = p.suffix.lower()
        # v0.7.5：GIF / 视频保持原容器，只有静态图片才套用「输出格式」
        if src_ext in eng_mod.IMAGE_EXTS:
            ext = "." + self._fmt
        else:
            ext = src_ext or ".mp4"
        if self._output_mode == "same":
            out_dir = p.parent
            stem = p.stem + (self._suffix or "")
        else:
            out_dir = Path(self._folder) if self._folder else p.parent
            stem = p.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / (stem + ext)
        i = 1
        while out.exists():
            out = out_dir / f"{stem}_{i}{ext}"
            i += 1
        return str(out)

    def _max_threads(self) -> int:
        return max(1, min(int(cfg.maxThreads.value), 4))

    # =========================================================================
    # 前后对比
    # =========================================================================

    def _on_compare(self, item_id):
        item = self._items.get(item_id)
        if item:
            self._show_compare(item["src"], item.get("out", ""))

    def _show_compare(self, src, out):
        """弹出 1280×720 放大前后对比窗口（v0.3.5）。"""
        dlg = CompareWindow(src, out, parent=self.window())
        dlg.exec()

    # =========================================================================
    # 任务运行管理（自管线程池循环）
    # =========================================================================

    def _on_start(self):
        if not self._engine_id or not eng_mod.find_engine(self._engine_id):
            QMessageBox.warning(
                self, tr("common.warning"), tr("upscale.toast.no_engine"))
            return
        if not self._items:
            return
        self._pending = [k for k, v in self._items.items()
                         if v["status"] in ("pending", "failed")]
        if not self._pending:
            return
        self._running = True
        self._paused = False
        # 入队瞬间快照当前参数，运行中改设置不影响已启动的任务
        self._run_values = self.paramPanel.values()
        self._queue_auto_follow.set_active(True)
        self._launch_next()

    def _launch_next(self):
        while (self._running and not self._paused
               and len(self._active) < self._max_threads() and self._pending):
            src = self._pending.pop(0)
            self._active.add(src)
            out = self._out_path(src)
            self._items[src]["out"] = out
            self._items[src]["status"] = "running"
            self.listWidget.set_status(src, "running")
            self._queue_auto_follow.ensure(self.listWidget.items[src])
            worker = UpscaleWorker(
                src, src, out, self._engine_id,
                getattr(self, "_run_values", None) or self.paramPanel.values())
            worker.signals.progress.connect(self.listWidget.set_progress)
            worker.signals.finished.connect(self._on_finished)
            QThreadPool.globalInstance().start(worker)

    def _on_finished(self, item_id, ok, saved, detail):
        self._active.discard(item_id)
        status = "done" if ok else "failed"
        self._items[item_id]["status"] = status
        self._items[item_id]["saved"] = saved
        self.listWidget.set_status(item_id, status, saved, detail)
        if self._running and not self._paused:
            self._launch_next()
        if not self._pending and not self._active:
            self._running = False
            self._queue_auto_follow.set_active(False)
        self._update_controls()

    def _on_pause(self):
        if self._running and not self._paused:
            self._paused = True
        else:
            self._paused = False
            if self._running:
                self._launch_next()
        self._update_controls()

    def _on_clear(self):
        self._items.clear()
        self._pending.clear()
        self._active.clear()
        self._running = False
        self._paused = False
        self.listWidget.clear()
        self._update_controls()

    def _on_remove(self, item_id):
        self._items.pop(item_id, None)
        if item_id in self._pending:
            self._pending.remove(item_id)
        self._active.discard(item_id)
        self.listWidget.remove_item(item_id)

    def _update_controls(self):
        ready = bool(self._engine_id) and bool(eng_mod.find_engine(self._engine_id))
        self.startBtn.setEnabled(
            ready and bool(self._items)
            and not (self._running and not self._paused))
        self.pauseBtn.setEnabled(self._running)
        self.clearBtn.setEnabled(bool(self._items))
        self.pauseBtn.setText(tr("convert.resume")
            if (self._running and self._paused) else tr("convert.pause"))

    # =========================================================================
    # 主题 / i18n
    # =========================================================================

    def retheme(self):
        super().retheme()
        self.dropArea.retheme()

    def retranslateUi(self):
        self.titleLabel.setText(tr("nav.upscale"))
        self.subLabel.setText(tr("upscale.tagline"))
        self.tInput.setText(tr("upscale.input.title"))
        self.tSettings.setText(tr("upscale.settings.title"))
        self.tQueue.setText(tr("upscale.queue.title"))
        self.dropArea.retranslate(
            tr("upscale.drop.title"), tr("upscale.drop.hint"),
            tr("upscale.drop.formats"))
        self.addFolderBtn.setText(tr("upscale.add_folder"))
        self.noEngineHint.setText(tr("upscale.engine.none_hint"))
        self.detectBtn.setText(tr("upscale.engine.detect"))
        self.reload_engines()
        self._apply_output_mode()
        self.startBtn.setText(tr("convert.start"))
        self.pauseBtn.setText(tr("convert.pause"))
        self.clearBtn.setText(tr("convert.clear"))
        self.listWidget.retranslate()
        self._update_controls()
