"""Compress screen — rebuilt UI. Self-managed batch image compression."""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QFileDialog, QScrollArea,
    QSlider, QLabel, QMessageBox,
)
from PyQt6.QtCore import Qt

from qfluentwidgets import (
    FluentIcon as FIF, PushButton, PrimaryPushButton, SwitchButton, ComboBox,
    CaptionLabel, StrongBodyLabel, isDarkTheme,
)

from ..core.config import cfg
from ..core import compressor
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
from .queue_widget import ProgressBar, StatusPill, human_size


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
class _WorkerSignals(QObject):
    progress = Signal(str, int)
    finished = Signal(str, bool, int, str)  # id, ok, saved_bytes, detail


class CompressWorker(QRunnable):
    def __init__(self, item_id, src, out, target_fmt, mode, quality, preferred, opts=None):
        super().__init__()
        self.setAutoDelete(True)
        self.item_id = item_id
        self.src = src
        self.out = out
        self.target_fmt = target_fmt
        self.mode = mode
        self.quality = quality
        # ``preferred`` is now a real backend id ("pillow"/"oxipng"/"optipng"/
        # "mozjpeg") or ``None`` to let ``best_backend`` decide. The old, invalid
        # "ffmpeg" sentinel is gone.
        self.preferred = preferred
        self.opts = opts or {}
        self.signals = _WorkerSignals()

    def run(self):
        self.signals.progress.emit(self.item_id, 0)
        src_ext = Path(self.src).suffix.lower().lstrip(".")
        # Resolve the "same" sentinel to the source extension so we never try to
        # save to a literal "SAME" format (the root cause of every task failing).
        effective = src_ext if self.target_fmt in ("same", "", None) else self.target_fmt
        log.info(
            "[compress] start id=%s src=%s ext=%s target=%s effective=%s mode=%s "
            "quality=%s backend=%s opts=%s",
            self.item_id, self.src, src_ext, self.target_fmt, effective,
            self.mode, self.quality, self.preferred, self.opts,
        )
        try:
            if compressor.needs_conversion(src_ext, effective):
                ok, detail, saved = compressor.transcode_and_compress(
                    self.src, self.out, effective, self.mode,
                    self.quality, self.opts, preferred=self.preferred)
            else:
                ok, detail, saved = compressor.compress_auto(
                    self.src, self.out, self.mode, self.quality, self.opts,
                    preferred=self.preferred)
        except Exception:  # defensive — log the full traceback for debugging
            log.exception("[compress] task %s raised an exception", self.item_id)
            ok, detail, saved = False, "exception (see log)", 0
        log.info(
            "[compress] finished id=%s ok=%s saved=%d detail=%s",
            self.item_id, ok, saved, detail,
        )
        self.signals.finished.emit(self.item_id, ok, saved, detail)


# --------------------------------------------------------------------------
# Item + list
# --------------------------------------------------------------------------
class CompressItemWidget(ThemedCard):
    removeRequested = Signal(str)

    def __init__(self, item_id: str, src: str, parent=None):
        super().__init__(parent)
        self._id = item_id
        self._src = src
        self._saved = 0
        self._status = "pending"

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

    def set_status(self, status: str, saved: int = 0, detail: str = ""):
        self._status = status
        self.pill.set_status(status)
        self.prog.set_error(status == "failed")
        if status == "done":
            self._saved = saved
            self.detailLbl.setText(
                f"{human_size(Path(self._src).stat().st_size if Path(self._src).exists() else 0)} "
                f"→ {human_size(Path(self._src).stat().st_size - saved if Path(self._src).exists() else 0)}"
                if saved else tr("compress.done"))
        elif status == "failed":
            self.detailLbl.setText((detail or tr("compress.failed"))[:60])
        else:
            self.detailLbl.setText("")

    def retranslate(self):
        self.pill.set_status(self._status)
        self.delBtn.setToolTip(tr("compress.action.remove"))


class CompressListWidget(QWidget):
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
            w.setStyleSheet(f"color: {muted_text()}; font-weight:600;")
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
        # The "queue empty" hint text was removed by design; keep the label
        # hidden so no empty gap is left behind.
        self.emptyHint.setVisible(False)

    def _update_stats(self):
        total = len(self.items)
        done = 0
        failed = 0
        for w in self.items.values():
            if w._status == "done":
                done += 1
            elif w._status == "failed":
                failed += 1
        self.statTotal.setText(tr("compress.queue.stats.total", n=total))
        self.statDone.setText(tr("compress.queue.stats.done", n=done))
        self.statErr.setText(tr("compress.queue.stats.error", n=failed))

    def add_item(self, item_id: str, src: str):
        if item_id in self.items:
            return
        w = CompressItemWidget(item_id, src)
        w.removeRequested.connect(self.removeRequested)
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
        self.emptyHint.setText(tr("compress.queue.empty"))
        self._update_stats()


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
class CompressInterface(InterfaceBase):
    def __init__(self, parent=None):
        super().__init__("Compress", tr("nav.compress"), tr("compress.subtitle"), parent)

        self._items: dict[str, dict] = {}
        self._pending: list[str] = []
        self._active: set[str] = set()
        self._running = False
        self._paused = False

        self._program = "pillow"
        self._tool_opts = {
            "pillow": {},
            "oxipng": {"level": 2, "interlace": False, "strip": "safe"},
            "optipng": {"level": 2, "strip": "all"},
            "mozjpeg": {"quality": 100, "progressive": True, "strip": True, "arithmetic": False},
        }
        self._quality = 100
        self._target = "same"
        self._output_mode = cfg.compressMode.value
        self._suffix = cfg.compressSuffix.value
        self._folder = cfg.compressFolder.value or ""

        # --- input --------------------------------------------------------
        card, vb, self.tInput = self._card("compress.input.title")
        self.dropArea = DropArea(self)
        self.dropArea.filesDropped.connect(self._on_files)
        self.dropArea.clicked.connect(self._pick_files)
        vb.addWidget(self.dropArea)
        tools = QHBoxLayout()
        self.addFolderBtn = ghost_btn(tr("compress.add.folder"), icon=FIF.FOLDER_ADD)
        self.addFolderBtn.clicked.connect(self._pick_folder)
        tools.addWidget(self.addFolderBtn)
        vb.addLayout(tools)
        self.vbox.addWidget(card)
        self._inputCard = card

        # --- settings -----------------------------------------------------
        scard, svb, self.tSettings = self._card("compress.settings.title")

        self.programCombo = self._opt_combo(
            [(tr("advanced.compression.pillow"), "pillow"),
             ("oxipng", "oxipng"),
             ("OptiPNG", "optipng"),
             ("Mozilla JPEG", "mozjpeg")],
            self._program, lambda v: self._on_program(v))
        svb.addWidget(field_row(tr("advanced.compression.backend"), self.programCombo))

        # General compression parameters (target format + quality) — always
        # visible regardless of the chosen backend.
        self.paramsGroup = QWidget()
        fq = QVBoxLayout(self.paramsGroup)
        fq.setContentsMargins(0, 0, 0, 0)
        fq.setSpacing(6)
        self.targetCombo = self._opt_combo(
            [(tr("compress.target.same"), "same"), ("PNG", "png"), ("JPG", "jpg"),
             ("WebP", "webp"), ("BMP", "bmp"), ("TIFF", "tiff")],
            self._target, lambda v: setattr(self, "_target", v))
        fq.addWidget(field_row(tr("compress.target"), self.targetCombo))
        self.quality = QSlider(Qt.Orientation.Horizontal)
        self.quality.setRange(1, 100)
        self.quality.setValue(self._quality)
        self.quality.valueChanged.connect(lambda v: setattr(self, "_quality", v))
        fq.addWidget(field_row(tr("compress.quality"), self.quality))
        svb.addWidget(self.paramsGroup)

        # Tool-specific parameter groups
        self.oxipngGroup = self._build_oxipng()
        self.optipngGroup = self._build_optipng()
        self.mozjpegGroup = self._build_mozjpeg()
        svb.addWidget(self.oxipngGroup)
        svb.addWidget(self.optipngGroup)
        svb.addWidget(self.mozjpegGroup)

        # Output location settings (same as before)
        self.outputSwitch = SwitchButton(tr("compress.output.same"))
        self.outputSwitch.checkedChanged.connect(self._on_output_mode)
        svb.addWidget(field_row(tr("compress.output.mode"), self.outputSwitch))
        self.suffixEdit = QLineEdit(self._suffix)
        self.suffixEdit.textChanged.connect(
            lambda t: (setattr(self, "_suffix", t), setattr(cfg.compressSuffix, "value", t)))
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

        # Tools download (conditional: visible only when a tool is selected + missing)
        self.toolsBtn = primary_btn(tr("compress.tools.download"), icon=FIF.DOWNLOAD)
        self.toolsBtn.clicked.connect(self._on_download_tools)
        self.toolsStatus = CaptionLabel()
        self.toolsStatus.setStyleSheet(f"color: {muted_text()};")
        svb.addWidget(self.toolsBtn)
        svb.addWidget(self.toolsStatus)
        self.vbox.addWidget(scard)

        # --- queue --------------------------------------------------------
        qcard, qvb, self.tQueue = self._card("compress.queue.title")
        self.listWidget = CompressListWidget(self)
        self.listWidget.removeRequested.connect(self._on_remove)
        self.queueScroll = QScrollArea()
        self.queueScroll.setWidgetResizable(True)
        self.queueScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.queueScroll.setStyleSheet(
            f"QScrollArea{{border:none; background:transparent;}} {scrollbar_qss()}"
        )
        self.queueScroll.viewport().setStyleSheet("background:transparent;")
        self.queueScroll.setWidget(self.listWidget)
        self.queueScroll.setMinimumHeight(280)
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

        # Cards that auto-collapse when a batch finishes (queue stays open).
        self._auto_fold = [self._inputCard, scard]

        self._on_program(self._program)
        self._restyle_switches()
        self.vbox.addStretch(1)
        self._collapse_ready = True
        self.retheme()

    # -- helpers ----------------------------------------------------------
    def _card(self, title_key, subtitle_key=None):
        title_text = tr(title_key)
        sub_text = tr(subtitle_key) if subtitle_key else ""
        card = CollapsibleCard(title_text, sub_text, self)
        self.register_collapsible(card)
        return card, card.body, card.titleLabel

    def _opt_combo(self, mapping, current, on_change) -> ComboBox:
        combo = ComboBox()
        for disp, val in mapping:
            combo.addItem(disp)
        for i, (disp, val) in enumerate(mapping):
            if val == current:
                combo.setCurrentIndex(i)
                break
        combo._mapping = dict(mapping)
        combo.currentTextChanged.connect(lambda t: on_change(combo._mapping.get(t, t)))
        return combo

    def _repopulate_combo(self, combo: ComboBox, mapping: list):
        """Repopulate a combo created by ``_opt_combo`` with translated strings.
        Preserves the current value (by key, not by text position)."""
        current_val = combo._mapping.get(combo.currentText(), combo.currentText())
        combo.blockSignals(True)
        combo.clear()
        combo._mapping = dict(mapping)
        for disp, val in mapping:
            combo.addItem(disp)
        for i, (disp, val) in enumerate(mapping):
            if val == current_val:
                combo.setCurrentIndex(i)
                break
        combo.blockSignals(False)

    def _expand(self, paths):
        out = []
        for p in paths:
            if os.path.isdir(p):
                for root, _, files in os.walk(p):
                    for f in files:
                        fp = os.path.join(root, f)
                        if Path(fp).suffix.lower() in IMAGE_EXTS:
                            out.append(fp)
            elif os.path.isfile(p) and Path(p).suffix.lower() in IMAGE_EXTS:
                out.append(p)
        seen, uniq = set(), []
        for p in out:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    # -- input -----------------------------------------------------------
    def _on_files(self, paths):
        for p in self._expand(paths):
            self._add_item(p)
        self._update_controls()

    def _pick_files(self):
        flt = "Images (" + " ".join(f"*{e}" for e in sorted(IMAGE_EXTS)) + ")"
        files, _ = QFileDialog.getOpenFileNames(self, tr("compress.add.files"), "", flt)
        if files:
            self._on_files(files)

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, tr("compress.add.folder"), "")
        if d:
            self._on_files([d])

    def _add_item(self, src):
        if src in self._items:
            return
        self._items[src] = {"src": src, "status": "pending", "saved": 0}
        self.listWidget.add_item(src, src)
        self._auto_expand_cards()

    # -- settings --------------------------------------------------------
    def _on_program(self, p):
        self._program = p
        # ``paramsGroup`` (target + quality) is always visible; only the
        # tool-specific parameter groups follow the selected backend.
        self.oxipngGroup.setVisible(p == "oxipng")
        self.optipngGroup.setVisible(p == "optipng")
        self.mozjpegGroup.setVisible(p == "mozjpeg")
        self._refresh_tool_status()

    def _refresh_tool_status(self):
        if self._program == "pillow":
            self.toolsBtn.setVisible(False)
            self.toolsStatus.setVisible(False)
            return
        installed = False
        if self._program == "oxipng":
            installed = compressor.find_tool("oxipng") is not None
        elif self._program == "optipng":
            installed = compressor.find_tool("optipng") is not None
        elif self._program == "mozjpeg":
            installed = compressor.find_tool("jpegtran") is not None or compressor.find_tool("cjpeg") is not None
        self.toolsBtn.setVisible(not installed)
        self.toolsStatus.setVisible(installed)
        if installed:
            self.toolsStatus.setText(tr("compress.tools.done"))

    def _current_mode(self):
        if self._program in ("oxipng", "optipng"):
            return "lossless"
        return "lossless" if self._quality == 100 else "lossy"

    def _current_opts(self):
        return self._tool_opts.get(self._program, {})

    def _build_oxipng(self):
        grp = self._tool_opts["oxipng"]
        w = QWidget()
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(6)
        lvl = QSlider(Qt.Orientation.Horizontal)
        lvl.setRange(0, 6)
        lvl.setValue(int(grp["level"]))
        lvl_label = QLabel(str(grp["level"]))
        lvl.valueChanged.connect(lambda v: (grp.__setitem__("level", v), lvl_label.setText(str(v))))
        row = QHBoxLayout()
        row.addWidget(lvl_label)
        row.addWidget(lvl, 1)
        ly.addWidget(field_row(tr("advanced.level"), row))
        inter = SwitchButton(tr("advanced.interlace"))
        inter.setChecked(bool(grp["interlace"]))
        inter.checkedChanged.connect(lambda b: grp.__setitem__("interlace", b))
        self._ox_inter = inter
        ly.addWidget(field_row(tr("advanced.interlace"), inter))
        strip = self._opt_combo(
            [(tr("advanced.strip.safe"), "safe"), (tr("advanced.strip.all"), "all")],
            grp["strip"], lambda v: grp.__setitem__("strip", v))
        ly.addWidget(field_row(tr("advanced.strip"), strip))
        return w

    def _build_optipng(self):
        grp = self._tool_opts["optipng"]
        w = QWidget()
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(6)
        lvl = QSlider(Qt.Orientation.Horizontal)
        lvl.setRange(0, 7)
        lvl.setValue(int(grp["level"]))
        lvl_label = QLabel(str(grp["level"]))
        lvl.valueChanged.connect(lambda v: (grp.__setitem__("level", v), lvl_label.setText(str(v))))
        row = QHBoxLayout()
        row.addWidget(lvl_label)
        row.addWidget(lvl, 1)
        ly.addWidget(field_row(tr("advanced.level"), row))
        strip = self._opt_combo(
            [(tr("advanced.strip.all"), "all"), (tr("advanced.strip.safe"), "safe")],
            grp["strip"], lambda v: grp.__setitem__("strip", v))
        ly.addWidget(field_row(tr("advanced.strip"), strip))
        return w

    def _build_mozjpeg(self):
        grp = self._tool_opts["mozjpeg"]
        w = QWidget()
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(6)
        q = QSlider(Qt.Orientation.Horizontal)
        q.setRange(1, 100)
        q.setValue(int(grp["quality"]))
        q_label = QLabel(str(grp["quality"]))
        q.valueChanged.connect(lambda v: (grp.__setitem__("quality", v), q_label.setText(str(v))))
        row = QHBoxLayout()
        row.addWidget(q_label)
        row.addWidget(q, 1)
        ly.addWidget(field_row(tr("advanced.quality"), row))
        prog = SwitchButton(tr("advanced.progressive"))
        prog.setChecked(bool(grp["progressive"]))
        prog.checkedChanged.connect(lambda b: grp.__setitem__("progressive", b))
        self._moz_prog = prog
        ly.addWidget(field_row(tr("advanced.progressive"), prog))
        stripx = SwitchButton(tr("advanced.strip"))
        stripx.setChecked(bool(grp["strip"]))
        stripx.checkedChanged.connect(lambda b: grp.__setitem__("strip", b))
        self._moz_strip = stripx
        ly.addWidget(field_row(tr("advanced.strip"), stripx))
        arith = SwitchButton(tr("advanced.arithmetic"))
        arith.setChecked(bool(grp["arithmetic"]))
        arith.checkedChanged.connect(lambda b: grp.__setitem__("arithmetic", b))
        self._moz_arith = arith
        ly.addWidget(field_row(tr("advanced.arithmetic"), arith))
        return w

    def _on_output_mode(self, checked):
        self._output_mode = "same" if checked else "fixed"
        cfg.compressMode.value = self._output_mode
        self._apply_output_mode()

    def _apply_output_mode(self):
        same = self._output_mode == "same"
        self.outputSwitch.setChecked(same)
        self.outputSwitch.setText(tr("compress.output.same") if same else tr("compress.output.fixed"))
        self.suffixRow.setVisible(same)
        self.folderRow.setVisible(not same)

    def _restyle_switches(self):
        """Apply the locale-aware on/off labels to the boolean toggle switches (#6)."""
        for sw in (self._ox_inter, self._moz_prog, self._moz_strip, self._moz_arith):
            sw.setOnText(tr("common.on"))
            sw.setOffText(tr("common.off"))

    def _pick_output(self):
        d = QFileDialog.getExistingDirectory(self, tr("compress.output.browse"), self._folder or "")
        if d:
            self._folder = d
            cfg.compressFolder.value = d
            self.folderEdit.setText(d)

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

    # -- run management --------------------------------------------------
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

    def _on_start(self):
        if not self._items:
            return
        self._pending = [k for k, v in self._items.items() if v["status"] in ("pending", "failed")]
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
            worker = CompressWorker(src, src, out, self._target, self._current_mode(),
                                    self._quality, self._program, opts=self._current_opts())
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
            self._auto_collapse_cards()
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
        self.startBtn.setEnabled(bool(self._items) and not (self._running and not self._paused))
        self.pauseBtn.setEnabled(self._running)
        self.clearBtn.setEnabled(bool(self._items))
        self.pauseBtn.setText(tr("compress.resume") if (self._running and self._paused)
                              else tr("compress.pause"))

    # -- auto-collapse / expand -------------------------------------------
    def _auto_collapse_cards(self):
        """Collapse input/settings cards when a batch finishes (if enabled)."""
        if not cfg.autoCollapse.value:
            return
        for c in self._auto_fold:
            c.setCollapsed(True)

    def _auto_expand_cards(self):
        """Expand all cards when user adds new files (if enabled)."""
        if not cfg.autoCollapse.value:
            return
        for c in self._auto_fold:
            c.setCollapsed(False)

    # -- theme / i18n ----------------------------------------------------
    def retheme(self):
        super().retheme()
        self.dropArea.retheme()

    def retranslateUi(self):
        self.titleLabel.setText(tr("nav.compress"))
        self.subLabel.setText(tr("compress.subtitle"))
        self.tInput.setText(tr("compress.input.title"))
        self.tSettings.setText(tr("compress.settings.title"))
        self.tQueue.setText(tr("compress.queue.title"))
        self.dropArea.retranslate(tr("compress.drop.title"), tr("compress.drop.hint"),
                                  tr("compress.drop.formats"))
        self.addFolderBtn.setText(tr("compress.add.folder"))
        self.toolsBtn.setText(tr("compress.tools.download"))
        self._repopulate_combo(self.programCombo, [
            (tr("advanced.compression.pillow"), "pillow"),
            ("oxipng", "oxipng"),
            ("OptiPNG", "optipng"),
            ("Mozilla JPEG", "mozjpeg"),
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
