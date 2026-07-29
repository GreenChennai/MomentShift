"""Compress screen — rebuilt UI. Self-managed batch image compression."""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QFileDialog, QScrollArea,
    QSlider, QLabel, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer

from qfluentwidgets import (
    FluentIcon as FIF, PushButton, PrimaryPushButton, SwitchButton, ComboBox,
    CaptionLabel, StrongBodyLabel, isDarkTheme,
)

from ..core.config import cfg
from ..core import compressor
from ..core.presets import IMAGE_EXTS
from ..core.qt_compat import Signal, QObject, QRunnable, QThreadPool
from ..core.tools_download import ToolsDownloadAllWorker
from ..i18n.translator import tr
from .theme import (
    ThemedCard, panel, field_row, primary_btn, ghost_btn, icon_btn,
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
    def __init__(self, item_id, src, out, target_fmt, mode, quality, preferred):
        super().__init__()
        self.setAutoDelete(True)
        self.item_id = item_id
        self.src = src
        self.out = out
        self.target_fmt = target_fmt
        self.mode = mode
        self.quality = quality
        self.preferred = None if preferred == "auto" else preferred
        self.signals = _WorkerSignals()

    def run(self):
        self.signals.progress.emit(self.item_id, 0)
        src_ext = Path(self.src).suffix.lower().lstrip(".")
        try:
            if compressor.needs_conversion(src_ext, self.target_fmt):
                ok, detail, saved = compressor.transcode_and_compress(
                    self.src, self.out, self.target_fmt, self.mode,
                    self.quality, {}, preferred=self.preferred)
            else:
                ok, detail, saved = compressor.compress_auto(
                    self.src, self.out, self.mode, self.quality, {},
                    preferred=self.preferred)
        except Exception as exc:  # defensive
            ok, detail, saved = False, str(exc), 0
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
        self.emptyHint.setVisible(not self.items)

    def add_item(self, item_id: str, src: str):
        if item_id in self.items:
            return
        w = CompressItemWidget(item_id, src)
        w.removeRequested.connect(self.removeRequested)
        self.items[item_id] = w
        self.listLayout.insertWidget(self.listLayout.count() - 1, w)
        self._refresh_empty()

    def set_progress(self, item_id: str, pct: int):
        w = self.items.get(item_id)
        if w:
            w.set_progress(pct)

    def set_status(self, item_id: str, status: str, saved: int = 0, detail: str = ""):
        w = self.items.get(item_id)
        if w:
            w.set_status(status, saved, detail)

    def remove_item(self, item_id: str):
        w = self.items.pop(item_id, None)
        if w:
            w.deleteLater()
        self._refresh_empty()

    def clear(self):
        for w in self.items.values():
            w.deleteLater()
        self.items.clear()
        self._refresh_empty()

    def retranslate(self):
        for w in self.items.values():
            w.retranslate()
        self.emptyHint.setText(tr("compress.queue.empty"))


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
class CompressInterface(InterfaceBase):
    def __init__(self, parent=None):
        super().__init__("Compress", tr("nav.compress"), tr("compress.subtitle"), parent)

        self._items: dict[str, dict] = {}
        self._backend_map: dict[str, str] = {}
        self._pending: list[str] = []
        self._active: set[str] = set()
        self._running = False
        self._paused = False

        self._backend = "auto"
        self._mode = "lossless"
        self._quality = 100
        self._target = "same"
        self._output_mode = cfg.outputMode.value
        self._suffix = cfg.outputSuffix.value
        self._folder = cfg.outputFolder.value or ""

        # --- input --------------------------------------------------------
        card, vb, self.tInput = self._card("compress.input.title", "compress.input.subtitle")
        self.dropArea = DropArea(self)
        self.dropArea.filesDropped.connect(self._on_files)
        self.dropArea.clicked.connect(self._pick_files)
        vb.addWidget(self.dropArea)
        tools = QHBoxLayout()
        self.addFilesBtn = ghost_btn(tr("compress.add.files"), icon=FIF.ADD)
        self.addFilesBtn.clicked.connect(self._pick_files)
        self.addFolderBtn = ghost_btn(tr("compress.add.folder"), icon=FIF.FOLDER_ADD)
        self.addFolderBtn.clicked.connect(self._pick_folder)
        tools.addWidget(self.addFilesBtn)
        tools.addWidget(self.addFolderBtn)
        vb.addLayout(tools)
        self.vbox.addWidget(card)

        # --- settings -----------------------------------------------------
        scard, svb, self.tSettings = self._card("compress.settings.title")
        self.backendCombo = ComboBox(self)
        self.backendCombo.currentTextChanged.connect(self._on_backend)
        svb.addWidget(field_row(tr("compress.backend"), self.backendCombo))
        self.modeCombo = self._opt_combo(
            [(tr("compress.mode.lossless"), "lossless"), (tr("compress.mode.lossy"), "lossy")],
            self._mode, lambda v: setattr(self, "_mode", v))
        svb.addWidget(field_row(tr("compress.mode"), self.modeCombo))
        self.quality = QSlider(Qt.Orientation.Horizontal)
        self.quality.setRange(1, 100)
        self.quality.setValue(self._quality)
        self.quality.valueChanged.connect(lambda v: setattr(self, "_quality", v))
        svb.addWidget(field_row(tr("compress.quality"), self.quality))
        self.targetCombo = self._opt_combo(
            [(tr("compress.target.same"), "same"), ("PNG", "png"), ("JPG", "jpg"),
             ("WebP", "webp"), ("BMP", "bmp"), ("TIFF", "tiff")],
            self._target, lambda v: setattr(self, "_target", v))
        svb.addWidget(field_row(tr("compress.target"), self.targetCombo))

        self.outputSwitch = SwitchButton(tr("compress.output.same"))
        self.outputSwitch.checkedChanged.connect(self._on_output_mode)
        svb.addWidget(field_row(tr("compress.output.mode"), self.outputSwitch))
        self.suffixEdit = QLineEdit(self._suffix)
        self.suffixEdit.textChanged.connect(lambda t: setattr(self, "_suffix", t))
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

        self.toolsBtn = ghost_btn(tr("compress.tools.download"), icon=FIF.DOWNLOAD)
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
        self.queueScroll.setMaximumHeight(600)
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

        # Populate backends lazily on first show: constructing the combo items
        # here triggers an offscreen paint in headless CI/sandbox; deferring also
        # means newly installed compression tools are detected when the tab opens.
        QTimer.singleShot(0, self._fill_backends)
        self.retheme()

    # -- helpers ----------------------------------------------------------
    def _card(self, title_key, subtitle_key=None):
        card = ThemedCard(self)
        vb = QVBoxLayout(card)
        vb.setContentsMargins(CARD_MARGIN, 14, CARD_MARGIN, 14)
        vb.setSpacing(10)
        titleLbl = StrongBodyLabel(tr(title_key))
        vb.addWidget(titleLbl)
        if subtitle_key:
            vb.addWidget(CaptionLabel(tr(subtitle_key)))
        return card, vb, titleLbl

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

    # -- settings --------------------------------------------------------
    def _fill_backends(self):
        backs = compressor.available_backends()
        mapping = [(tr("compress.backend.auto"), "auto")]
        for bid, meta in backs.items():
            mapping.append((meta["name"], bid))
        self.backendCombo.clear()
        for disp, val in mapping:
            self.backendCombo.addItem(disp)
        self._backend_map = dict(mapping)
        self.backendCombo.setCurrentText(
            self._backend_map.get(self._backend, tr("compress.backend.auto")))

    def _on_backend(self, text):
        self._backend = self._backend_map.get(text, "auto")

    def _on_output_mode(self, checked):
        self._output_mode = "same" if checked else "fixed"
        self._apply_output_mode()

    def _apply_output_mode(self):
        same = self._output_mode == "same"
        self.outputSwitch.setChecked(same)
        self.outputSwitch.setText(tr("compress.output.same") if same else tr("compress.output.fixed"))
        self.suffixRow.setVisible(same)
        self.folderRow.setVisible(not same)

    def _pick_output(self):
        d = QFileDialog.getExistingDirectory(self, tr("compress.output.browse"), self._folder or "")
        if d:
            self._folder = d
            self.folderEdit.setText(d)

    def _on_download_tools(self):
        self.toolsBtn.setEnabled(False)
        self.toolsStatus.setText(tr("compress.tools.downloading"))
        worker = ToolsDownloadAllWorker(str(compressor.tools_dir()))
        worker.signals.finished.connect(self._on_tools_downloaded)
        QThreadPool.globalInstance().start(worker)

    def _on_tools_downloaded(self, result: dict):
        self.toolsBtn.setEnabled(True)
        ok = all(v[0] for v in result.values())
        self.toolsStatus.setText(tr("compress.tools.done") if ok else tr("compress.tools.failed"))
        self._fill_backends()

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
            worker = CompressWorker(src, src, out, self._target, self._mode,
                                    self._quality, self._backend)
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
        self.addFilesBtn.setText(tr("compress.add.files"))
        self.addFolderBtn.setText(tr("compress.add.folder"))
        self.toolsBtn.setText(tr("compress.tools.download"))
        self._fill_backends()
        self._apply_output_mode()
        self.listWidget.retranslate()
        self.startBtn.setText(tr("compress.start"))
        self.pauseBtn.setText(tr("compress.pause"))
        self.clearBtn.setText(tr("compress.clear"))
        self._update_controls()
