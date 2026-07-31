"""压缩界面 —— 批量图片压缩（v0.2.9 重写）。

自管任务队列（QRunnable 线程池模型），支持多种压缩后端（pillow/oxipng/jpegoptim）。
v0.7.0 改动：后端改为 auto/oxipng/jpegoptim/pillow 三套独立参数面板，移除 imagecodecs/optipng/mozjpeg。
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QFileDialog, QScrollArea,
    QSlider, QLabel, QMessageBox, QSpinBox,
)
from PyQt6.QtCore import Qt

from qfluentwidgets import (
    FluentIcon as FIF, PushButton, PrimaryPushButton, SwitchButton, ComboBox,
    CaptionLabel, StrongBodyLabel,
)

from ..core.config import cfg
from ..core import compressor
from qfluentwidgets import qconfig
from ..core.presets import IMAGE_EXTS
from ..core.qt_compat import Signal, QObject, QRunnable, QThreadPool
from ..core.tools_download import ToolsDownloadWorker
from ..core.logger import get_logger
from ..i18n.translator import tr

log = get_logger("compress")
from .theme import (
    ThemedCard, CollapsibleCard, panel, field_row, primary_btn, ghost_btn, icon_btn,
    muted_text, sub_text, CARD_MARGIN, scrollbar_qss,
)
from .base import InterfaceBase
from .drop_area import DropArea
from .help_bubble import attach_help
from .queue_widget import ProgressBar, StatusPill, human_size


# =============================================================================
# 压缩 Worker（QRunnable 线程池）
# =============================================================================
class _WorkerSignals(QObject):
    progress = Signal(str, int)
    # id, ok, saved_bytes, detail, effective_backend
    finished = Signal(str, bool, int, str, str)


class CompressWorker(QRunnable):
    """单个图片压缩任务。在 QThreadPool 线程中执行。"""

    def __init__(self, item_id, src, out, target_fmt, mode, quality, preferred, opts=None):
        super().__init__()
        self.setAutoDelete(True)
        self.item_id = item_id
        self.src = src
        self.out = out
        self.target_fmt = target_fmt
        self.mode = mode
        self.quality = quality
        self.preferred = preferred  # "pillow"/"oxipng"/"jpegoptim" 或 None
        self.opts = opts or {}
        self.signals = _WorkerSignals()

    def run(self):
        """在线程池中执行压缩。"""
        self.signals.progress.emit(self.item_id, 0)
        src_ext = Path(self.src).suffix.lower().lstrip(".")
        # "same" → 用源文件扩展名
        effective = src_ext if self.target_fmt in ("same", "", None) else self.target_fmt
        # 解析「实际使用的后端」：若用户所选程序无法处理该格式，回退到能处理的程序
        backend = compressor.best_backend(effective, self.mode, self.preferred)
        backend = compressor._fallback_to_pillow(backend, self.src)
        log.info(
            "[compress] start id=%s src=%s ext=%s target=%s effective=%s mode=%s "
            "quality=%s preferred=%s resolved=%s",
            self.item_id, self.src, src_ext, self.target_fmt, effective,
            self.mode, self.quality, self.preferred, backend,
        )
        try:
            if compressor.needs_conversion(src_ext, effective):
                ok, detail, saved = compressor.transcode_and_compress(
                    self.src, self.out, effective, self.mode,
                    self.quality, self.opts, preferred=backend)
            else:
                ok, detail, saved = compressor.compress_auto(
                    self.src, self.out, self.mode, self.quality, self.opts,
                    preferred=backend)
        except Exception:
            log.exception("[compress] task %s raised an exception", self.item_id)
            ok, detail, saved = False, "exception (see log)", 0
        log.info("[compress] finished id=%s ok=%s saved=%d backend=%s",
                 self.item_id, ok, saved, backend)
        self.signals.finished.emit(self.item_id, ok, saved, detail, backend)


# =============================================================================
# 队列列表组件
# =============================================================================
# 后端显示名（用于「自动切换」后的状态提示，如 "jpegoptim 完成"）
BACKEND_NAMES = {"oxipng": "oxipng", "jpegoptim": "jpegoptim", "pillow": "Pillow"}


class CompressItemWidget(ThemedCard):
    """压缩队列中的单个任务卡片。"""
    removeRequested = Signal(str)

    def __init__(self, item_id: str, src: str, selected: str = "auto", parent=None):
        super().__init__(parent)
        self._id = item_id
        self._src = src
        self._saved = 0
        self._status = "pending"
        self._selected = selected  # 用户选择的压缩程序（用于判断是否"自动切换"）

        vb = QVBoxLayout(self)
        vb.setContentsMargins(14, 12, 14, 12)
        vb.setSpacing(8)

        top = QHBoxLayout()
        self.nameLbl = QLabel(Path(src).name)
        self.nameLbl.setObjectName("queueName")
        self.nameLbl.setToolTip(src)
        top.addWidget(self.nameLbl, 1)
        self.pill = StatusPill("pending")
        top.addWidget(self.pill)
        vb.addLayout(top)

        self.prog = ProgressBar()
        vb.addWidget(self.prog)

        bottom = QHBoxLayout()
        self.detailLbl = CaptionLabel()
        self.detailLbl.setStyleSheet(f"color: {muted_text()};")
        bottom.addWidget(self.detailLbl, 1)
        self.delBtn = icon_btn(FIF.DELETE, tr("compress.action.remove"))
        self.delBtn.clicked.connect(lambda: self.removeRequested.emit(self._id))
        bottom.addWidget(self.delBtn)
        vb.addLayout(bottom)

        self.set_status("pending")
        self.set_progress(0)

    def set_progress(self, pct: int):
        self.prog.set_value(pct)

    def set_status(self, status: str, saved: int = 0, detail: str = "",
                   backend: str = ""):
        self._status = status
        if status == "done":
            # v0.7.1：若实际使用的后端与所选不同（自动切换），提示具体程序名
            if (backend and self._selected in ("oxipng", "jpegoptim", "pillow")
                    and backend != self._selected):
                name = BACKEND_NAMES.get(backend, backend)
                self.pill.set_status(
                    "done_sw", text=f"{name} {tr('compress.done.by')}")
            else:
                self.pill.set_status("done")
            src_size = Path(self._src).stat().st_size if Path(self._src).exists() else 0
            if saved:
                self.detailLbl.setText(
                    f"{human_size(src_size)} → {human_size(src_size - saved)}")
            else:
                self.detailLbl.setText(tr("compress.done"))
        else:
            self.pill.set_status(status)
            self.prog.set_error(status == "failed")
            if status == "failed":
                self.detailLbl.setText((detail or tr("compress.failed"))[:60])
            else:
                self.detailLbl.setText("")

    def retranslate(self):
        self.pill.set_status(self._status)
        self.delBtn.setToolTip(tr("compress.action.remove"))


class CompressListWidget(QWidget):
    """压缩任务列表（带统计栏）。"""
    removeRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: dict[str, CompressItemWidget] = {}
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
        self.emptyHint = CaptionLabel(tr("compress.queue.empty"))
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
        self.statTotal.setText(tr("compress.queue.stats.total", n=total))
        self.statDone.setText(tr("compress.queue.stats.done", n=done))
        self.statErr.setText(tr("compress.queue.stats.error", n=failed))

    def add_item(self, item_id: str, src: str, selected: str = "auto"):
        if item_id in self.items:
            return
        w = CompressItemWidget(item_id, src, selected)
        w.removeRequested.connect(self.removeRequested)
        self.items[item_id] = w
        self.listLayout.insertWidget(self.listLayout.count() - 1, w)
        self._refresh_empty()
        self._update_stats()

    def set_progress(self, item_id: str, pct: int):
        w = self.items.get(item_id)
        if w:
            w.set_progress(pct)

    def set_status(self, item_id: str, status: str, saved: int = 0,
                   detail: str = "", backend: str = ""):
        w = self.items.get(item_id)
        if w:
            w.set_status(status, saved, detail, backend)
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
        self.emptyHint.setText(tr("compress.queue.empty"))
        self._update_stats()


# =============================================================================
# 压缩界面
# =============================================================================
class CompressInterface(InterfaceBase):
    """图片压缩标签页。

    自管任务队列（QRunnable + QThreadPool），支持 auto/oxipng/jpegoptim/pillow
    多种压缩后端，以及无损/有损两种模式。
    """

    def __init__(self, parent=None):
        super().__init__("Compress", tr("nav.compress"), tr("compress.subtitle"), parent)

        # 内部状态
        self._items: dict[str, dict] = {}
        self._pending: list[str] = []
        self._active: set[str] = set()
        self._running = False
        self._paused = False
        # 重入防护（v0.3.0）：防止模态对话框事件循环触发二次弹框
        self._picking = False

        # 压缩参数默认值（v0.7.0：oxipng / jpegoptim / pillow 三后端）
        self._program = "auto"
        self._tool_opts = {
            "oxipng": {"level": 3, "interlace": True, "strip": "all",
                       "filter": 0, "zc": 6, "alpha": False},
            "jpegoptim": {"jo_mode": "lossless", "jo_max": 85, "jo_strip": "all",
                          "jo_progressive": "auto", "jo_threshold": 0,
                          "jo_preserve": True, "jo_retry": False},
            "pillow": {"pil_quality": 95, "pil_optimize": True,
                       "pil_progressive": True, "pil_subsampling": "4:4:4"},
        }
        self._target = "same"
        self._switches: list = []
        self._output_mode = cfg.compressMode.value
        self._suffix = cfg.compressSuffix.value
        self._folder = cfg.compressFolder.value or ""

        # =====================================================================
        # 输入卡片
        # =====================================================================
        card, vb, self.tInput = self._make_card("compress.input.title")
        self.dropArea = DropArea(self)
        self.dropArea.filesDropped.connect(self._on_files)
        self.dropArea.clicked.connect(self._pick_files)
        vb.addWidget(self.dropArea)
        tools = QHBoxLayout()
        self.addFolderBtn = primary_btn(tr("compress.add.folder"), icon=FIF.FOLDER_ADD)
        self.addFolderBtn.clicked.connect(self._pick_folder)
        tools.addWidget(self.addFolderBtn)
        vb.addLayout(tools)
        self.vbox.addWidget(card)
        self._inputCard = card

        # =====================================================================
        # 压缩设置卡片
        # =====================================================================
        scard, svb, self.tSettings = self._make_card(
            "compress.settings.title", collapsed=True)

        # 压缩后端选择（v0.7.0：auto / oxipng / jpegoptim / pillow）
        self.programCombo = self._make_combo(
            [(tr("advanced.compression.auto"), "auto"),
             (tr("advanced.compression.oxipng"), "oxipng"),
             (tr("advanced.compression.jpegoptim"), "jpegoptim"),
             (tr("advanced.compression.pillow"), "pillow")],
            self._program, lambda v: self._on_program(v))
        svb.addWidget(field_row(tr("advanced.compression.backend"), self.programCombo))

        # 路由提示（仅 auto 时显示）
        rhint = CaptionLabel(tr("advanced.compression.route"))
        rhint.setWordWrap(True)
        rhint.setStyleSheet(f"color: {muted_text()}; background: transparent;")
        svb.addWidget(rhint)
        self._route_hint = rhint

        # 通用压缩参数（目标格式）
        self.paramsGroup = QWidget()
        # 强制透明背景，防止在深/浅色主题下出现异常色块 (v0.3.1, #6)
        self.paramsGroup.setStyleSheet("background: transparent;")
        fq = QVBoxLayout(self.paramsGroup)
        fq.setContentsMargins(0, 0, 0, 0)
        fq.setSpacing(6)
        self.targetCombo = self._make_combo(
            [(tr("compress.target.same"), "same"), ("PNG", "png"), ("JPG", "jpg"),
             ("WebP", "webp"), ("BMP", "bmp"), ("TIFF", "tiff")],
            self._target, lambda v: setattr(self, "_target", v))
        fq.addWidget(field_row(tr("compress.target"), self.targetCombo))
        svb.addWidget(self.paramsGroup)

        # 各后端专用参数组（v0.7.0：三后端独立面板）
        # v0.7.2 F1：auto 模式下三个组直接堆进 svb 会挤在一起，
        # 这里统一收进 _backend_container（纵向间距 16），三后端同显时留出呼吸感。
        self._backend_container = QWidget()
        self._backend_container.setStyleSheet("background: transparent;")
        _bcont_ly = QVBoxLayout(self._backend_container)
        _bcont_ly.setContentsMargins(0, 0, 0, 0)
        _bcont_ly.setSpacing(16)
        self.oxipngGroup = self._build_oxipng()
        _bcont_ly.addWidget(self.oxipngGroup)
        self.joGroup = self._build_jpegoptim()
        _bcont_ly.addWidget(self.joGroup)
        self.pilGroup = self._build_pillow()
        _bcont_ly.addWidget(self.pilGroup)
        svb.addWidget(self._backend_container)

        # 输出位置
        self.outputSwitch = SwitchButton(tr("compress.output.same"))
        self.outputSwitch.checkedChanged.connect(self._on_output_mode)
        svb.addWidget(field_row(tr("compress.output.mode"), self.outputSwitch))
        self.suffixEdit = QLineEdit(self._suffix)
        self.suffixEdit.textChanged.connect(
            lambda t: (setattr(self, "_suffix", t),
                       setattr(cfg.compressSuffix, "value", t),
                       qconfig.save()))
        self.suffixRow = field_row(tr("compress.output.suffix"), self.suffixEdit)
        svb.addWidget(self.suffixRow)
        self.folderEdit = QLineEdit(self._folder)
        self.folderEdit.setReadOnly(True)
        self.browseBtn = icon_btn(FIF.FOLDER, tr("compress.output.browse"))
        self.browseBtn.clicked.connect(self._pick_output)
        frow = QHBoxLayout()
        frow.addWidget(self.folderEdit, 1)
        frow.addWidget(self.browseBtn)
        self.folderRow = field_row(tr("compress.output.folder"), frow)
        svb.addWidget(self.folderRow)
        self._apply_output_mode()

        # 工具下载按钮（仅非 pillow 后端且缺失时显示）
        self.toolsBtn = primary_btn(tr("compress.tools.download"), icon=FIF.DOWNLOAD)
        self.toolsBtn.clicked.connect(self._on_download_tools)
        self.toolsStatus = CaptionLabel()
        self.toolsStatus.setStyleSheet(f"color: {muted_text()};")
        svb.addWidget(self.toolsBtn)
        svb.addWidget(self.toolsStatus)
        self.vbox.addWidget(scard)

        # =====================================================================
        # 队列卡片
        # =====================================================================
        qcard, qvb, self.tQueue = self._make_card("compress.queue.title")
        self.listWidget = CompressListWidget(self)
        self.listWidget.removeRequested.connect(self._on_remove)
        self.queueScroll = self._make_scroll(280)
        self.queueScroll.setWidget(self.listWidget)
        qvb.addWidget(self.queueScroll)
        ctrl = QHBoxLayout()
        self.startBtn = primary_btn(tr("compress.start"), icon=FIF.PLAY)
        self.startBtn.clicked.connect(self._on_start)
        self.pauseBtn = ghost_btn(tr("compress.pause"), icon=FIF.PAUSE)
        self.pauseBtn.clicked.connect(self._on_pause)
        self.clearBtn = ghost_btn(tr("compress.clear"), icon=FIF.DELETE)
        self.clearBtn.clicked.connect(self._on_clear)
        ctrl.addWidget(self.startBtn, 1)
        ctrl.addWidget(self.pauseBtn)
        ctrl.addWidget(self.clearBtn)
        qvb.addLayout(ctrl)
        self.vbox.addWidget(qcard)

        self._on_program(self._program)
        self._restyle_switches()
        self.vbox.addStretch(1)
        self._collapse_ready = True
        self.retheme()

    # =========================================================================
    # 后端专用参数面板
    # =========================================================================

    def _build_oxipng(self):
        grp = self._tool_opts["oxipng"]
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)
        lvl = QSlider(Qt.Orientation.Horizontal)
        lvl.setRange(0, 6)
        lvl.setValue(int(grp["level"]))
        lvl_label = QLabel(str(grp["level"]))
        lvl.valueChanged.connect(
            lambda v: (grp.__setitem__("level", v), lvl_label.setText(str(v))))
        row = QHBoxLayout()
        row.addWidget(lvl_label)
        row.addWidget(lvl, 1)
        fr = field_row(tr("advanced.level"), row)
        ly.addWidget(fr); attach_help(fr, "advanced.help.level")
        inter = SwitchButton(tr("advanced.interlace"))
        inter.setChecked(bool(grp["interlace"]))
        inter.checkedChanged.connect(lambda b: grp.__setitem__("interlace", b))
        self._switches.append(inter)
        fr = field_row(tr("advanced.interlace"), inter)
        ly.addWidget(fr); attach_help(fr, "advanced.help.interlace")
        strip = self._make_combo(
            [(tr("advanced.strip.safe"), "safe"), (tr("advanced.strip.all"), "all")],
            grp["strip"], lambda v: grp.__setitem__("strip", v))
        fr = field_row(tr("advanced.strip"), strip)
        ly.addWidget(fr); attach_help(fr, "advanced.help.strip")
        filt = self._make_combo(
            [(tr("advanced.filter.none"), 0), (tr("advanced.filter.sub"), 1),
             (tr("advanced.filter.up"), 2), (tr("advanced.filter.average"), 3),
             (tr("advanced.filter.paeth"), 4), (tr("advanced.filter.mixed"), 5)],
            grp["filter"], lambda v: grp.__setitem__("filter", int(v)))
        fr = field_row(tr("advanced.filter"), filt)
        ly.addWidget(fr); attach_help(fr, "advanced.help.filter")
        zc = QSlider(Qt.Orientation.Horizontal)
        zc.setRange(1, 9)
        zc.setValue(int(grp["zc"]))
        zc_label = QLabel(str(grp["zc"]))
        zc.valueChanged.connect(
            lambda v: (grp.__setitem__("zc", v), zc_label.setText(str(v))))
        zc_row = QHBoxLayout()
        zc_row.addWidget(zc_label)
        zc_row.addWidget(zc, 1)
        fr = field_row(tr("advanced.zc"), zc_row)
        ly.addWidget(fr); attach_help(fr, "advanced.help.zc")
        alpha = SwitchButton(tr("advanced.alpha"))
        alpha.setChecked(bool(grp["alpha"]))
        alpha.checkedChanged.connect(lambda b: grp.__setitem__("alpha", b))
        self._switches.append(alpha)
        fr = field_row(tr("advanced.alpha"), alpha)
        ly.addWidget(fr); attach_help(fr, "advanced.help.alpha")
        return w

    def _build_jpegoptim(self):
        grp = self._tool_opts["jpegoptim"]
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)
        jo_mode = self._make_combo(
            [(tr("advanced.jo.mode.lossless"), "lossless"),
             (tr("advanced.jo.mode.lossy"), "lossy")],
            grp["jo_mode"],
            lambda v: (grp.__setitem__("jo_mode", v), self._sync_jo_max(v)))
        fr = field_row(tr("advanced.jo.mode"), jo_mode)
        ly.addWidget(fr); attach_help(fr, "advanced.help.jo.mode")
        jo_max = QSlider(Qt.Orientation.Horizontal)
        jo_max.setRange(0, 100)
        jo_max.setValue(int(grp["jo_max"]))
        jo_max_spin = QSpinBox()
        jo_max_spin.setRange(0, 100)
        jo_max_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        jo_max_spin.setValue(int(grp["jo_max"]))
        jo_max.valueChanged.connect(
            lambda v: (grp.__setitem__("jo_max", v), jo_max_spin.setValue(v)))
        jo_max_spin.valueChanged.connect(
            lambda v: (grp.__setitem__("jo_max", v), jo_max.setValue(v)))
        jm_row = QHBoxLayout()
        jm_row.addWidget(jo_max, 1)
        jm_row.addWidget(jo_max_spin)
        jo_max_fr = field_row(tr("advanced.jo.max"), jm_row)
        ly.addWidget(jo_max_fr); attach_help(jo_max_fr, "advanced.help.jo.max")
        jo_strip = self._make_combo(
            [(tr("advanced.jo.strip.none"), "none"),
             (tr("advanced.jo.strip.meta"), "meta"),
             (tr("advanced.jo.strip.exif"), "exif"),
             (tr("advanced.jo.strip.icc"), "icc"),
             (tr("advanced.jo.strip.all"), "all")],
            grp["jo_strip"], lambda v: grp.__setitem__("jo_strip", v))
        fr = field_row(tr("advanced.jo.strip"), jo_strip)
        ly.addWidget(fr); attach_help(fr, "advanced.help.jo.strip")
        jo_prog = self._make_combo(
            [(tr("advanced.jo.prog.auto"), "auto"),
             (tr("advanced.jo.prog.keep"), "keep"),
             (tr("advanced.jo.prog.progressive"), "progressive"),
             (tr("advanced.jo.prog.normal"), "normal")],
            grp["jo_progressive"], lambda v: grp.__setitem__("jo_progressive", v))
        fr = field_row(tr("advanced.jo.prog"), jo_prog)
        ly.addWidget(fr); attach_help(fr, "advanced.help.jo.prog")
        jo_thr = QSpinBox()
        jo_thr.setRange(0, 99)
        jo_thr.setSuffix("%")
        jo_thr.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        jo_thr.setValue(int(grp["jo_threshold"]))
        jo_thr.valueChanged.connect(lambda v: grp.__setitem__("jo_threshold", v))
        fr = field_row(tr("advanced.jo.threshold"), jo_thr)
        ly.addWidget(fr); attach_help(fr, "advanced.help.jo.threshold")
        jo_pres = SwitchButton(tr("advanced.jo.preserve"))
        jo_pres.setChecked(bool(grp["jo_preserve"]))
        jo_pres.checkedChanged.connect(lambda b: grp.__setitem__("jo_preserve", b))
        self._switches.append(jo_pres)
        fr = field_row(tr("advanced.jo.preserve"), jo_pres)
        ly.addWidget(fr); attach_help(fr, "advanced.help.jo.preserve")
        jo_retry = SwitchButton(tr("advanced.jo.retry"))
        jo_retry.setChecked(bool(grp["jo_retry"]))
        jo_retry.checkedChanged.connect(lambda b: grp.__setitem__("jo_retry", b))
        self._switches.append(jo_retry)
        fr = field_row(tr("advanced.jo.retry"), jo_retry)
        ly.addWidget(fr); attach_help(fr, "advanced.help.jo.retry")
        self._jo_max_fr = jo_max_fr
        self._sync_jo_max(grp["jo_mode"])
        return w

    def _sync_jo_max(self, mode: str):
        if hasattr(self, "_jo_max_fr"):
            self._jo_max_fr.setEnabled(mode == "lossy")

    def _build_pillow(self):
        grp = self._tool_opts["pillow"]
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)
        pq = QSlider(Qt.Orientation.Horizontal)
        pq.setRange(0, 95)
        pq.setValue(int(grp["pil_quality"]))
        pq_spin = QSpinBox()
        pq_spin.setRange(0, 95)
        pq_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        pq_spin.setValue(int(grp["pil_quality"]))
        pq.valueChanged.connect(
            lambda v: (grp.__setitem__("pil_quality", v), pq_spin.setValue(v)))
        pq_spin.valueChanged.connect(
            lambda v: (grp.__setitem__("pil_quality", v), pq.setValue(v)))
        pq_row = QHBoxLayout()
        pq_row.addWidget(pq, 1)
        pq_row.addWidget(pq_spin)
        fr = field_row(tr("advanced.pil.quality"), pq_row)
        ly.addWidget(fr); attach_help(fr, "advanced.help.pil.quality")
        pil_opt = SwitchButton(tr("advanced.pil.optimize"))
        pil_opt.setChecked(bool(grp["pil_optimize"]))
        pil_opt.checkedChanged.connect(lambda b: grp.__setitem__("pil_optimize", b))
        self._switches.append(pil_opt)
        fr = field_row(tr("advanced.pil.optimize"), pil_opt)
        ly.addWidget(fr); attach_help(fr, "advanced.help.pil.optimize")
        pil_prog = SwitchButton(tr("advanced.pil.progressive"))
        pil_prog.setChecked(bool(grp["pil_progressive"]))
        pil_prog.checkedChanged.connect(
            lambda b: grp.__setitem__("pil_progressive", b))
        self._switches.append(pil_prog)
        fr = field_row(tr("advanced.pil.progressive"), pil_prog)
        ly.addWidget(fr); attach_help(fr, "advanced.help.pil.progressive")
        pil_sub = self._make_combo(
            [(tr("advanced.pil.sub.444"), "4:4:4"),
             (tr("advanced.pil.sub.422"), "4:2:2"),
             (tr("advanced.pil.sub.420"), "4:2:0")],
            grp["pil_subsampling"],
            lambda v: grp.__setitem__("pil_subsampling", v))
        fr = field_row(tr("advanced.pil.subsampling"), pil_sub)
        ly.addWidget(fr); attach_help(fr, "advanced.help.pil.subsampling")
        return w

    def _on_program(self, p):
        self._program = p
        # 容器始终可见；auto 显示全部三个组（纵向留白），指定程序只留对应组
        self._backend_container.setVisible(True)
        self.oxipngGroup.setVisible(p == "oxipng")
        self.joGroup.setVisible(p == "jpegoptim")
        self.pilGroup.setVisible(p == "pillow")
        if p == "auto":
            self.oxipngGroup.setVisible(True)
            self.joGroup.setVisible(True)
            self.pilGroup.setVisible(True)
        self._route_hint.setVisible(p == "auto")
        self._refresh_tool_status()

    def _refresh_tool_status(self):
        if self._program in ("pillow", "auto"):
            self.toolsBtn.setVisible(False)
            self.toolsStatus.setVisible(False)
            return
        installed = False
        if self._program == "oxipng":
            installed = compressor.find_tool("oxipng") is not None
        elif self._program == "jpegoptim":
            installed = compressor.find_tool("jpegoptim") is not None
        self.toolsBtn.setVisible(not installed)
        self.toolsStatus.setVisible(installed)
        if installed:
            self.toolsStatus.setText(tr("compress.tools.done"))

    def _current_mode(self):
        if self._program == "auto":
            return "lossless"
        if self._program == "oxipng":
            return "lossless"
        if self._program == "jpegoptim":
            return self._tool_opts["jpegoptim"].get("jo_mode", "lossless")
        return "lossy"

    def _current_quality(self) -> int:
        if self._program == "jpegoptim":
            o = self._tool_opts["jpegoptim"]
            return int(o["jo_max"]) if o.get("jo_mode") == "lossy" else 100
        if self._program == "pillow":
            return int(self._tool_opts["pillow"]["pil_quality"])
        return 100

    def _current_opts(self):
        if self._program == "auto":
            merged: dict = {}
            for g in self._tool_opts.values():
                merged.update(g)
            return merged
        return self._tool_opts.get(self._program, {})

    def _on_output_mode(self, checked):
        self._output_mode = "same" if checked else "fixed"
        cfg.compressMode.value = self._output_mode
        qconfig.save()
        self._apply_output_mode()

    def _apply_output_mode(self):
        same = self._output_mode == "same"
        self.outputSwitch.setChecked(same)
        self.outputSwitch.setText(
            tr("compress.output.same") if same else tr("compress.output.fixed"))
        self.suffixRow.setVisible(same)
        self.folderRow.setVisible(not same)

    def _restyle_switches(self):
        for sw in getattr(self, "_switches", []):
            sw.setOnText(tr("common.on"))
            sw.setOffText(tr("common.off"))

    def _pick_output(self):
        """浏览选择固定输出目录（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            d = QFileDialog.getExistingDirectory(
                None, tr("compress.output.browse"), self._folder or "",
                QFileDialog.DontUseNativeDialog,
                )
            if d:
                self._folder = d
                cfg.compressFolder.value = d
                qconfig.save()
                self.folderEdit.setText(d)
        finally:
            self._picking = False

    def _on_download_tools(self):
        self.toolsBtn.setEnabled(False)
        self.toolsStatus.setVisible(True)
        self.toolsStatus.setText(tr("compress.tools.downloading"))
        worker = ToolsDownloadWorker(self._program, str(compressor.tools_dir()))
        worker.signals.finished.connect(self._on_tools_downloaded)
        QThreadPool.globalInstance().start(worker)

    def _on_tools_downloaded(self, tool_id, ok, msg):
        self.toolsBtn.setEnabled(True)
        if ok:
            self.toolsStatus.setText(tr("compress.tools.done"))
        else:
            self.toolsStatus.setText(tr("compress.tools.failed", msg=msg))
        self._refresh_tool_status()

    # =========================================================================
    # 输入处理
    # =========================================================================

    def _on_files(self, paths):
        for p in self._expand_paths(paths, IMAGE_EXTS):
            self._add_item(p)
        self._update_controls()

    def _pick_files(self):
        """弹出图片文件选择器（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            flt = "Images (" + " ".join(f"*{e}" for e in sorted(IMAGE_EXTS)) + ")"
            files, _ = QFileDialog.getOpenFileNames(
                None, tr("compress.add.files"), "", flt, "",
                QFileDialog.DontUseNativeDialog,
            )
            if files:
                self._on_files(files)
        finally:
            self._picking = False

    def _pick_folder(self):
        """弹出文件夹选择器（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            d = QFileDialog.getExistingDirectory(
                None, tr("compress.add.folder"), "",
                QFileDialog.DontUseNativeDialog,
                )
            if d:
                self._on_files([d])
        finally:
            self._picking = False

    def _add_item(self, src):
        if src in self._items:
            return
        self._items[src] = {"src": src, "status": "pending", "saved": 0}
        self.listWidget.add_item(src, src, self._program)

    # =========================================================================
    # 输出路径计算
    # =========================================================================

    def _out_path(self, src: str) -> str:
        p = Path(src)
        ext = p.suffix if self._target == "same" else "." + self._target
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
        return max(1, min(int(cfg.maxThreads.value), 8))

    # =========================================================================
    # 任务运行管理（自管线程池循环）
    # =========================================================================

    def _on_start(self):
        if not self._items:
            return
        self._pending = [k for k, v in self._items.items()
                         if v["status"] in ("pending", "failed")]
        if not self._pending:
            return
        self._running = True
        self._paused = False
        self._launch_next()

    def _launch_next(self):
        while (self._running and not self._paused
               and len(self._active) < self._max_threads() and self._pending):
            src = self._pending.pop(0)
            self._active.add(src)
            out = self._out_path(src)
            self._items[src]["status"] = "running"
            self.listWidget.set_status(src, "running")
            worker = CompressWorker(
                src, src, out, self._target, self._current_mode(),
                self._current_quality(), self._program, opts=self._current_opts())
            worker.signals.progress.connect(self.listWidget.set_progress)
            worker.signals.finished.connect(self._on_finished)
            QThreadPool.globalInstance().start(worker)

    def _on_finished(self, item_id, ok, saved, detail, backend):
        self._active.discard(item_id)
        status = "done" if ok else "failed"
        self._items[item_id]["status"] = status
        self._items[item_id]["saved"] = saved
        self.listWidget.set_status(item_id, status, saved, detail, backend)
        if self._running and not self._paused:
            self._launch_next()
        if not self._pending and not self._active:
            self._running = False
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
        self.startBtn.setEnabled(
            bool(self._items) and not (self._running and not self._paused))
        self.pauseBtn.setEnabled(self._running)
        self.clearBtn.setEnabled(bool(self._items))
        self.pauseBtn.setText(tr("compress.resume")
            if (self._running and self._paused) else tr("compress.pause"))

    # =========================================================================
    # 主题 / i18n
    # =========================================================================

    def retheme(self):
        super().retheme()
        self.dropArea.retheme()

    def retranslateUi(self):
        self.titleLabel.setText(tr("nav.compress"))
        self.subLabel.setText(tr("compress.subtitle"))
        self.tInput.setText(tr("compress.input.title"))
        self.tSettings.setText(tr("compress.settings.title"))
        self.tQueue.setText(tr("compress.queue.title"))
        self.dropArea.retranslate(
            tr("compress.drop.title"), tr("compress.drop.hint"),
            tr("compress.drop.formats"))
        self.addFolderBtn.setText(tr("compress.add.folder"))
        self.toolsBtn.setText(tr("compress.tools.download"))
        self._repopulate_combo(self.programCombo, [
            (tr("advanced.compression.auto"), "auto"),
            (tr("advanced.compression.oxipng"), "oxipng"),
            (tr("advanced.compression.jpegoptim"), "jpegoptim"),
            (tr("advanced.compression.pillow"), "pillow"),
        ])
        self._repopulate_combo(self.targetCombo, [
            (tr("compress.target.same"), "same"),
            ("PNG", "png"), ("JPG", "jpg"),
            ("WebP", "webp"), ("BMP", "bmp"), ("TIFF", "tiff"),
        ])
        self._apply_output_mode()
        self._restyle_switches()
        self.listWidget.retranslate()
        self.startBtn.setText(tr("compress.start"))
        self.pauseBtn.setText(tr("compress.pause"))
        self.clearBtn.setText(tr("compress.clear"))
        self._update_controls()
