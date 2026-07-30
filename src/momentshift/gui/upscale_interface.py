"""Upscale screen — rebuilt UI. Self-managed batch AI upscaling.

Mirrors :mod:`compress_interface` for the self-managed worker loop (capped at
``min(cfg.maxThreads, 4)``) and :mod:`convert_interface` for the staging → queue
flow. Wires to :mod:`core.upscaler` (engine detect + one-click download) and the
shared :class:`CompareWidget` for before/after preview.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QFileDialog, QScrollArea,
    QLabel, QMessageBox, QProgressBar,
)
from PyQt6.QtCore import Qt

from qfluentwidgets import (
    FluentIcon as FIF, PushButton, PrimaryPushButton, SwitchButton, ComboBox,
    CaptionLabel, StrongBodyLabel, BodyLabel, HyperlinkButton, isDarkTheme,
)

from ..core.config import cfg
from ..core import upscaler
from ..core.qt_compat import Signal, QObject, QRunnable, QThreadPool
from ..i18n.translator import tr
from .theme import (
    ThemedCard, CollapsibleCard, field_row, primary_btn, ghost_btn, icon_btn,
    muted_text, sub_text, CARD_MARGIN, scrollbar_qss,
)
from .base import InterfaceBase
from .drop_area import DropArea
from .queue_widget import ProgressBar, StatusPill, human_size
from .compare_widget import CompareWidget


VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".wmv"}


# --------------------------------------------------------------------------
# Engine card (mirror of FfmpegCard)
# --------------------------------------------------------------------------
class EngineCard(ThemedCard):
    engine_ready = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        vb = QVBoxLayout(self)
        vb.setContentsMargins(16, 14, 16, 14)
        vb.setSpacing(10)

        self.titleLbl = StrongBodyLabel(tr("upscale.engine.title"))
        vb.addWidget(self.titleLbl)

        top = QHBoxLayout()
        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        top.addWidget(self.dot)
        self.statusLbl = BodyLabel()
        top.addWidget(self.statusLbl, 1)
        vb.addLayout(top)

        self.linkBtn = HyperlinkButton(upscaler.ENGINE_PAGE, tr("upscale.engine.open_site"))
        self.dlBtn = PrimaryPushButton(tr("upscale.engine.oneclick"), icon=FIF.DOWNLOAD)
        self.dlBtn.clicked.connect(self._download)
        row = QHBoxLayout()
        row.addWidget(self.linkBtn)
        row.addStretch(1)
        row.addWidget(self.dlBtn)
        vb.addLayout(row)

        self.prog = QProgressBar()
        self.prog.setRange(0, 0)
        self.prog.setFixedHeight(4)
        self.prog.setStyleSheet(
            "QProgressBar{background:#dcdcdc; border:none; border-radius:2px;} "
            "QProgressBar::chunk{background:#0f6cbd; border-radius:2px;}")
        self.prog.hide()
        vb.addWidget(self.prog)

        self._refresh()

    def _refresh(self):
        path = upscaler.find_upscaler()
        if path:
            n = len(upscaler.available_models())
            self.statusLbl.setText(tr("upscale.engine.ok", n=n))
            self.statusLbl.setStyleSheet("color:#10893e;")
            self.dot.setStyleSheet("background:#10893e; border-radius:5px;")
            # Collapse download / link when engine is ready
            self.linkBtn.hide()
            self.dlBtn.hide()
            self.prog.hide()
        else:
            self.statusLbl.setText(tr("upscale.engine.missing"))
            self.statusLbl.setStyleSheet(f"color:{sub_text()};")
            self.dot.setStyleSheet("background:#e81123; border-radius:5px;")
            self.linkBtn.show()
            self.dlBtn.show()

    def _download(self):
        self.dlBtn.setEnabled(False)
        self.prog.show()
        worker = upscaler.UpscalerDownloadWorker(str(upscaler.realesrgan_dir()))
        worker.signals.finished.connect(self._on_finished)
        QThreadPool.globalInstance().start(worker)

    def _on_finished(self, ok: bool, msg: str):
        self.prog.hide()
        self.dlBtn.setEnabled(True)
        self._refresh()
        if ok:
            self.engine_ready.emit()

    def retranslateUi(self):
        self.titleLbl.setText(tr("upscale.engine.title"))
        self.linkBtn.setText(tr("upscale.engine.open_site"))
        self.dlBtn.setText(tr("upscale.engine.oneclick"))
        self._refresh()


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
class _WorkerSignals(QObject):
    progress = Signal(str, int)
    finished = Signal(str, bool, int, str)  # id, ok, saved_bytes, detail


class UpscaleWorker(QRunnable):
    def __init__(self, item_id, src, out, model, scale, tile, gpu):
        super().__init__()
        self.setAutoDelete(True)
        self.item_id = item_id
        self.src = src
        self.out = out
        self.model = model
        self.scale = scale
        self.tile = tile
        self.gpu = "cpu" if not gpu else "auto"
        self.signals = _WorkerSignals()

    def run(self):
        self.signals.progress.emit(self.item_id, 0)
        try:
            ok, detail = upscaler.upscale_media(
                self.src, self.out, self.model, self.scale, self.tile, self.gpu)
        except Exception as exc:  # defensive
            ok, detail = False, str(exc)
        saved = 0
        try:
            if ok and Path(self.out).exists():
                saved = Path(self.src).stat().st_size - Path(self.out).stat().st_size
        except OSError:
            saved = 0
        self.signals.finished.emit(self.item_id, ok, saved, detail)


# --------------------------------------------------------------------------
# Item + list
# --------------------------------------------------------------------------
class UpscaleItemWidget(ThemedCard):
    removeRequested = Signal(str)
    compareRequested = Signal(str)

    def __init__(self, item_id: str, src: str, parent=None):
        super().__init__(parent)
        self._id = item_id
        self._src = src
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
        self.cmpBtn = icon_btn(FIF.SEARCH, tr("upscale.action.compare"))
        self.cmpBtn.clicked.connect(lambda: self.compareRequested.emit(self._id))
        bottom.addWidget(self.cmpBtn)
        self.delBtn = icon_btn(FIF.DELETE, tr("convert.action.remove"))
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
            src_size = Path(self._src).stat().st_size if Path(self._src).exists() else 0
            dst_size = src_size - saved
            pct = f"{(dst_size - src_size) / src_size * 100:+.0f}%" if src_size else ""
            self.detailLbl.setText(tr("upscale.result.saved",
                                     before=human_size(src_size),
                                     after=human_size(dst_size),
                                     pct=pct))
        elif status == "failed":
            self.detailLbl.setText((detail or tr("convert.status.failed"))[:80])
        elif status == "running":
            self.detailLbl.setText(tr("upscale.status.upscaling"))
        else:
            self.detailLbl.setText("")

    def retranslate(self):
        self.pill.set_status(self._status)
        self.cmpBtn.setToolTip(tr("upscale.action.compare"))
        self.delBtn.setToolTip(tr("convert.action.remove"))
        self.set_status(self._status)


class UpscaleListWidget(QWidget):
    removeRequested = Signal(str)
    compareRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: dict[str, UpscaleItemWidget] = {}
        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(8)
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
        self.emptyHint.setVisible(not self.items)

    def add_item(self, item_id: str, src: str):
        if item_id in self.items:
            return
        w = UpscaleItemWidget(item_id, src)
        w.removeRequested.connect(self.removeRequested)
        w.compareRequested.connect(self.compareRequested)
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
        self.emptyHint.setText(tr("upscale.queue.empty"))


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
class UpscaleInterface(InterfaceBase):
    def __init__(self, parent=None):
        super().__init__("Upscale", tr("nav.upscale"), tr("upscale.tagline"), parent)

        self._staged: list[str] = []
        self._items: dict[str, dict] = {}
        self._active: set[str] = set()
        self._pending: list[str] = []
        self._running = False
        self._paused = False

        self._model = "realesrgan-x4plus"
        self._scale = 4
        self._fmt = "png"
        self._tile = 0
        self._gpu = True
        self._output_mode = cfg.outputMode.value
        self._suffix = cfg.outputSuffix.value
        self._folder = cfg.outputFolder.value or ""

        # --- engine -------------------------------------------------------
        self.engineCard = EngineCard(self)
        self.engineCard.engine_ready.connect(self._update_controls)
        self.vbox.addWidget(self.engineCard)

        # --- input --------------------------------------------------------
        card, vb, self.tInput = self._card("upscale.input.title", "upscale.input.subtitle")
        self.dropArea = DropArea(self)
        self.dropArea.filesDropped.connect(self._on_files)
        self.dropArea.clicked.connect(self._pick_files)
        vb.addWidget(self.dropArea)
        tools = QHBoxLayout()
        self.addFilesBtn = ghost_btn(tr("upscale.btn.add"), icon=FIF.ADD)
        self.addFilesBtn.clicked.connect(self._pick_files)
        self.addFolderBtn = ghost_btn(tr("upscale.add_folder"), icon=FIF.FOLDER_ADD)
        self.addFolderBtn.clicked.connect(self._pick_folder)
        tools.addWidget(self.addFilesBtn)
        tools.addWidget(self.addFolderBtn)
        vb.addLayout(tools)
        self.vbox.addWidget(card)
        self._inputCard = card

        # --- staging ------------------------------------------------------
        scap, svb, self.tStage = self._card("upscale.staging.title")
        self.stagingCount = CaptionLabel(tr("upscale.staging.empty"))
        self.stagingCount.setStyleSheet(f"color: {muted_text()};")
        svb.addWidget(self.stagingCount)
        self.stagingScroll = self._scroll()
        self.stagingList = QWidget()
        self.stagingLayout = QVBoxLayout(self.stagingList)
        self.stagingLayout.setContentsMargins(0, 0, 0, 0)
        self.stagingLayout.setSpacing(6)
        self.stagingLayout.addStretch(1)
        self.stagingScroll.setWidget(self.stagingList)
        self.stagingScroll.setMinimumHeight(140)
        svb.addWidget(self.stagingScroll)
        sctrl = QHBoxLayout()
        self.stageAddBtn = primary_btn(tr("upscale.staging.add", n=0), icon=FIF.UP)
        self.stageAddBtn.clicked.connect(self._on_stage_to_queue)
        self.stageClearBtn = ghost_btn(tr("upscale.staging.clear"), icon=FIF.DELETE)
        self.stageClearBtn.clicked.connect(self._clear_staging)
        sctrl.addWidget(self.stageAddBtn, 1)
        sctrl.addWidget(self.stageClearBtn)
        svb.addLayout(sctrl)
        self.vbox.addWidget(scap)

        # --- settings -----------------------------------------------------
        setc, setvb, self.tSettings = self._card("upscale.settings.title")
        self.modelCombo = ComboBox()
        for mid, meta in upscaler.MODELS.items():
            self.modelCombo.addItem(meta["label"])
        self._model_map = {meta["label"]: mid for mid, meta in upscaler.MODELS.items()}
        self.modelCombo.setCurrentText(upscaler.MODELS[self._model]["label"])
        self.modelCombo.currentTextChanged.connect(
            lambda t: setattr(self, "_model", self._model_map.get(t, self._model)))
        setvb.addWidget(field_row(tr("upscale.model"), self.modelCombo))

        self.scaleCombo = self._opt_combo(
            [("2x", 2), ("3x", 3), ("4x", 4)], self._scale,
            lambda v: setattr(self, "_scale", v))
        setvb.addWidget(field_row(tr("upscale.scale"), self.scaleCombo))

        self.fmtCombo = self._opt_combo(
            [(tr("upscale.fmt.png"), "png"), (tr("upscale.fmt.jpg"), "jpg"),
             (tr("upscale.fmt.webp"), "webp")], self._fmt,
            lambda v: setattr(self, "_fmt", v))
        setvb.addWidget(field_row(tr("upscale.output.fmt"), self.fmtCombo))

        self.tileCombo = self._opt_combo(
            [(tr("upscale.tile.auto"), 0), ("256", 256), ("512", 512)], self._tile,
            lambda v: setattr(self, "_tile", v))
        setvb.addWidget(field_row(tr("upscale.tile"), self.tileCombo))

        self.gpuSwitch = SwitchButton(tr("upscale.gpu.auto"))
        self.gpuSwitch.setChecked(self._gpu)
        self.gpuSwitch.checkedChanged.connect(self._on_gpu)
        setvb.addWidget(field_row(tr("upscale.gpu"), self.gpuSwitch))

        self.outputSwitch = SwitchButton(tr("convert.output.same"))
        self.outputSwitch.checkedChanged.connect(self._on_output_mode)
        setvb.addWidget(field_row(tr("upscale.output.mode"), self.outputSwitch))
        self.suffixEdit = QLineEdit(self._suffix)
        self.suffixEdit.setPlaceholderText(tr("upscale.output.suffix_hint"))
        self.suffixEdit.textChanged.connect(lambda t: setattr(self, "_suffix", t))
        self.suffixRow = field_row(tr("upscale.output.suffix"), self.suffixEdit)
        setvb.addWidget(self.suffixRow)
        self.folderEdit = QLineEdit(self._folder)
        self.folderEdit.setReadOnly(True)
        self.browseBtn = icon_btn(FIF.FOLDER, tr("convert.output.browse"))
        self.browseBtn.clicked.connect(self._pick_output)
        frow = QHBoxLayout()
        frow.addWidget(self.folderEdit, 1)
        frow.addWidget(self.browseBtn)
        self.folderRow = field_row(tr("upscale.output.folder"), frow)
        setvb.addWidget(self.folderRow)
        self._apply_output_mode()
        self.vbox.addWidget(setc)

        # --- queue --------------------------------------------------------
        qcard, qvb, self.tQueue = self._card("upscale.queue.title", "upscale.queue.hint")
        self.listWidget = UpscaleListWidget(self)
        self.listWidget.removeRequested.connect(self._on_remove)
        self.listWidget.compareRequested.connect(self._on_compare)
        self.queueScroll = self._scroll()
        self.queueScroll.setWidget(self.listWidget)
        self.queueScroll.setMinimumHeight(280)
        qvb.addWidget(self.queueScroll)
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
        self.vbox.addWidget(qcard)

        # Cards that auto-collapse when a batch finishes (queue stays open).
        self._auto_fold = [self._inputCard, scap, setc]

        # --- compare ------------------------------------------------------
        self.compareWidget = CompareWidget(self)
        self.vbox.addWidget(self.compareWidget)

        self._render_staging()
        self._update_controls()
        self.retheme()

    # -- helpers ----------------------------------------------------------
    def _card(self, title_key, subtitle_key=None):
        title_text = tr(title_key)
        sub_text = tr(subtitle_key) if subtitle_key else ""
        card = CollapsibleCard(title_text, sub_text, self)
        return card, card.body, card.titleLabel

    def _scroll(self) -> QScrollArea:
        s = QScrollArea()
        s.setWidgetResizable(True)
        s.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        s.setStyleSheet(
            f"QScrollArea{{border:none; background:transparent;}} {scrollbar_qss()}"
        )
        s.viewport().setStyleSheet("background:transparent;")
        return s

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

    def _expand(self, paths) -> list[str]:
        exts = upscaler.IMAGE_EXTS | upscaler.ANIM_EXTS | VIDEO_EXTS
        out: list[str] = []
        for p in paths:
            if os.path.isdir(p):
                for root, _, files in os.walk(p):
                    for f in files:
                        fp = os.path.join(root, f)
                        if Path(fp).suffix.lower() in exts:
                            out.append(fp)
            elif os.path.isfile(p) and Path(p).suffix.lower() in exts:
                out.append(p)
        seen, uniq = set(), []
        for p in out:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    # -- input / staging --------------------------------------------------
    def _on_files(self, paths):
        self._add_staged(self._expand(paths))

    def _pick_files(self):
        exts = upscaler.IMAGE_EXTS | upscaler.ANIM_EXTS | VIDEO_EXTS
        flt = "Media (" + " ".join(f"*{e}" for e in sorted(exts)) + ")"
        files, _ = QFileDialog.getOpenFileNames(self, tr("upscale.btn.add"), "", flt)
        if files:
            self._add_staged(self._expand(files))

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, tr("upscale.add_folder"), "")
        if d:
            self._add_staged(self._expand([d]))

    def _add_staged(self, paths):
        if not paths:
            return
        for p in paths:
            if p not in self._staged:
                self._staged.append(p)
        self._render_staging()
        self._auto_expand_cards()

    def _render_staging(self):
        while self.stagingLayout.count():
            item = self.stagingLayout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not self._staged:
            self.stagingCount.setText(tr("upscale.staging.empty"))
            self.stagingLayout.addStretch(1)
        else:
            self.stagingCount.setText(tr("upscale.staging.count", n=len(self._staged)))
            for p in self._staged:
                row = QWidget()
                hb = QHBoxLayout(row)
                hb.setContentsMargins(4, 4, 4, 4)
                name = QLabel(Path(p).name)
                name.setObjectName("stagedName")
                name.setToolTip(p)
                name.setStyleSheet(f"color: {sub_text()};")
                hb.addWidget(name, 1)
                rm = icon_btn(FIF.DELETE, tr("convert.action.remove"))
                rm.clicked.connect(lambda _, path=p: self._remove_staged(path))
                hb.addWidget(rm)
                self.stagingLayout.insertWidget(self.stagingLayout.count() - 1, row)
            self.stagingLayout.addStretch(1)
        self.stageAddBtn.setText(tr("upscale.staging.add", n=len(self._staged)))
        self.stageAddBtn.setEnabled(bool(self._staged))

    def _remove_staged(self, path):
        if path in self._staged:
            self._staged.remove(path)
        self._render_staging()

    def _clear_staging(self):
        self._staged = []
        self._render_staging()

    def _on_stage_to_queue(self):
        if not self._staged:
            return
        for p in self._staged:
            if p not in self._items:
                self._items[p] = {"src": p, "out": self._out_path(p),
                                  "status": "pending", "saved": 0}
                self.listWidget.add_item(p, p)
        self._staged = []
        self._render_staging()
        self._update_controls()

    # -- settings --------------------------------------------------------
    def _on_gpu(self, checked):
        self._gpu = checked
        self.gpuSwitch.setText(tr("upscale.gpu.auto") if checked else tr("upscale.gpu.cpu"))

    def _on_output_mode(self, checked):
        self._output_mode = "same" if checked else "fixed"
        self._apply_output_mode()

    def _apply_output_mode(self):
        same = self._output_mode == "same"
        self.outputSwitch.setChecked(same)
        self.outputSwitch.setText(tr("convert.output.same") if same else tr("convert.output.fixed"))
        self.suffixRow.setVisible(same)
        self.folderRow.setVisible(not same)

    def _pick_output(self):
        d = QFileDialog.getExistingDirectory(self, tr("convert.output.browse"), self._folder or "")
        if d:
            self._folder = d
            self.folderEdit.setText(d)

    def _out_path(self, src: str) -> str:
        p = Path(src)
        ext = "." + self._fmt
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

    # -- compare ---------------------------------------------------------
    def _on_compare(self, item_id):
        item = self._items.get(item_id)
        if item:
            self.compareWidget.set_paths(item["src"], item.get("out", ""))

    # -- run management --------------------------------------------------
    def _on_start(self):
        if not upscaler.find_upscaler():
            QMessageBox.warning(self, tr("common.warning"), tr("upscale.toast.no_engine"))
            return
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
            self._items[src]["out"] = out
            self._items[src]["status"] = "running"
            self.listWidget.set_status(src, "running")
            worker = UpscaleWorker(src, src, out, self._model, self._scale,
                                   self._tile, self._gpu)
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
        ready = bool(upscaler.find_upscaler())
        self.startBtn.setEnabled(ready and bool(self._items)
                                 and not (self._running and not self._paused))
        self.pauseBtn.setEnabled(self._running)
        self.clearBtn.setEnabled(bool(self._items))
        self.pauseBtn.setText(tr("convert.resume") if (self._running and self._paused)
                              else tr("convert.pause"))

    # -- auto-collapse / expand -------------------------------------------
    def _auto_collapse_cards(self):
        """Collapse input/settings cards when a batch finishes (if enabled)."""
        if not cfg.autoCollapse.value:
            return
        for c in self._auto_fold:
            c.setCollapsed(True)

    def _auto_expand_cards(self):
        """Expand all cards when user stages new files (if enabled)."""
        if not cfg.autoCollapse.value:
            return
        for c in self._auto_fold:
            c.setCollapsed(False)

    # -- theme / i18n ----------------------------------------------------
    def retheme(self):
        super().retheme()
        self.dropArea.retheme()
        self.compareWidget._restyle()

    def retranslateUi(self):
        self.titleLabel.setText(tr("nav.upscale"))
        self.subLabel.setText(tr("upscale.tagline"))
        self.tInput.setText(tr("upscale.input.title"))
        self.tStage.setText(tr("upscale.staging.title"))
        self.tSettings.setText(tr("upscale.settings.title"))
        self.tQueue.setText(tr("upscale.queue.title"))
        self.engineCard.retranslateUi()
        self.dropArea.retranslate(tr("upscale.drop.title"), tr("upscale.drop.hint"),
                                  tr("upscale.drop.formats"))
        self.addFilesBtn.setText(tr("upscale.btn.add"))
        self.addFolderBtn.setText(tr("upscale.add_folder"))
        self.stageAddBtn.setText(tr("upscale.staging.add", n=len(self._staged)))
        self.stageClearBtn.setText(tr("upscale.staging.clear"))
        self.gpuSwitch.setText(tr("upscale.gpu.auto") if self._gpu else tr("upscale.gpu.cpu"))
        self._apply_output_mode()
        self.startBtn.setText(tr("convert.start"))
        self.pauseBtn.setText(tr("convert.pause"))
        self.clearBtn.setText(tr("convert.clear"))
        self.listWidget.retranslate()
        self._render_staging()
        self._update_controls()
