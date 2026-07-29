"""The dedicated "Compress" feature block.

A second major function of the app (alongside format conversion): batch,
multi-threaded image compression. Users drop images, pick a backend / lossless
vs lossy / quality (and optionally a target format), then run the whole batch
with live per-file progress and before→after size comparison.

The compression itself is delegated to :mod:`momentshift.core.compressor`, which
wraps Pillow (always available) and the external oxipng / OptiPNG / mozjpeg
binaries (when the user supplies them).
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from ..core.qt_compat import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, Signal, Qt, QObject, QRunnable,
    QThreadPool,
)
from PyQt6.QtWidgets import QLayout
from qfluentwidgets import (
    FluentIcon as FIF,
    ComboBox,
    Slider,
    SwitchButton,
    PushButton,
    PrimaryPushButton,
    StrongBodyLabel,
    BodyLabel,
    CaptionLabel,
    InfoBar,
    InfoBarPosition,
    ScrollArea,
    isDarkTheme,
    Theme,
)
from ..core.config import cfg
from ..core import compressor
from ..core.presets import IMAGE_EXTS
from ..i18n.translator import tr
from .base import InterfaceBase
from .drop_area import DropArea
from .theme import ThemedCard, sub_text, hint_text, muted_text
from .queue_widget import StatusPill, human_size

CATEGORY_ICON = {"image": FIF.PHOTO, "audio": FIF.MUSIC, "video": FIF.VIDEO}
IMAGE_TARGETS = ["same", "png", "jpg", "webp", "bmp", "tiff"]


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
class _WorkerSignals(QObject):
    progress = Signal(str, int)
    finished = Signal(str, bool, int, str)  # id, ok, saved_bytes, detail


class CompressWorker(QRunnable):
    """Compresses a single image inside the thread pool."""

    def __init__(self, item: dict, out_path: str):
        super().__init__()
        self.setAutoDelete(True)
        self.item = item
        self.out_path = out_path
        self.signals = _WorkerSignals()

    def run(self) -> None:
        self.signals.progress.emit(self.item["id"], 0)
        try:
            src = self.item["input_path"]
            target = self.item["target_fmt"]
            mode = self.item["mode"]
            quality = int(self.item.get("quality", 100))
            opts = self.item.get("opts", {}) or {}
            preferred = self.item.get("backend")
            preferred = None if preferred in (None, "auto") else preferred

            src_size = Path(src).stat().st_size
            self.item["src_size"] = src_size

            if compressor.needs_conversion(Path(src).suffix, target if target != "same" else Path(src).suffix):
                ok, detail, saved = compressor.transcode_and_compress(
                    src, self.out_path, target, mode, quality, opts, preferred
                )
            else:
                ok, detail, saved = compressor.compress_auto(
                    src, self.out_path, mode, quality, opts, preferred=preferred
                )
            self.signals.progress.emit(self.item["id"], 100)
            self.signals.finished.emit(self.item["id"], ok, saved, detail)
        except Exception as exc:  # pragma: no cover - defensive
            self.signals.finished.emit(self.item["id"], False, 0, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# List item
# --------------------------------------------------------------------------
class CompressItemWidget(ThemedCard):
    removeRequested = Signal(str)

    def __init__(self, item: dict, parent=None):
        super().__init__(parent)
        self.item = item
        self.setMinimumHeight(86)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(6)

        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(8)

        self.iconLabel = QLabel()
        cat = "image"
        self.iconLabel.setPixmap(
            CATEGORY_ICON.get(cat, FIF.DOCUMENT)
            .icon(Theme.DARK if isDarkTheme() else Theme.AUTO)
            .pixmap(22, 22)
        )
        self.iconLabel.setFixedSize(24, 24)
        self.iconLabel.setStyleSheet("background-color: transparent;")

        self.nameLabel = StrongBodyLabel(Path(item["input_path"]).name)
        self.nameLabel.setToolTip(str(item["input_path"]))

        # target format badge
        tgt = item["target_fmt"]
        badge_text = (Path(item["input_path"]).suffix.lstrip(".") or "?" ).upper() if tgt == "same" else tgt.upper()
        self.badge = QLabel(f"→ {badge_text}")
        self.badge.setObjectName("queueSub")

        self.statusLabel = StatusPill("pending")

        self.removeBtn = _tool_button(FIF.DELETE, tr("convert.action.remove"), self)
        self.removeBtn.setFixedSize(28, 28)
        self.removeBtn.clicked.connect(lambda: self.removeRequested.emit(item["id"]))

        row1.addWidget(self.iconLabel)
        row1.addWidget(self.nameLabel, 1)
        row1.addWidget(self.badge)
        row1.addWidget(self.statusLabel)
        row1.addWidget(self.removeBtn)

        self.progress = _ProgressBar()
        self.detailLabel = CaptionLabel("")
        self.detailLabel.setObjectName("queueSub")

        outer.addLayout(row1)
        outer.addWidget(self.progress)
        outer.addWidget(self.detailLabel)

    def set_progress(self, pct: int):
        self.progress.set_value(pct)

    def set_status(self, status: str, saved: int = 0, detail: str = ""):
        self.statusLabel.set_status(status)
        if status == "done":
            src = self.item.get("src_size", 0)
            saved = max(0, saved)
            pct = (saved / src * 100) if src else 0
            self.detailLabel.setText(
                tr("compress.result.saved", before=human_size(src),
                   after=human_size(src - saved), pct=f"-{pct:.0f}%")
                + f"  ·  {detail}"
            )
        elif status == "failed":
            self.detailLabel.setText(detail)
        elif status == "running":
            self.detailLabel.setText(tr("compress.status.compressing"))


def _tool_button(icon, tooltip, parent):
    from qfluentwidgets import TransparentToolButton

    btn = TransparentToolButton(icon, parent)
    btn.setToolTip(tooltip)
    return btn


class _ProgressBar(QWidget):
    """Reuse the same full-width background bar as the conversion queue."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0
        self.setMinimumHeight(8)
        self.setMaximumHeight(8)

    def set_value(self, v: int):
        self._value = max(0, min(100, v))
        self.update()

    def paintEvent(self, event):
        from PyQt6.QtGui import QPainter, QColor, QRectF
        from PyQt6.QtCore import Qt as _Qt

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        bg = QColor(220, 220, 220)
        fill = QColor(32, 128, 240)
        p.setPen(_Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, 4, 4)
        if self._value > 0:
            w = r.width() * self._value / 100.0
            p.setBrush(fill)
            p.drawRoundedRect(QRectF(r.x(), r.y(), w, r.height()), 4, 4)


# --------------------------------------------------------------------------
# List container
# --------------------------------------------------------------------------
class CompressListWidget(QWidget):
    removeRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: transparent; border: none;")
        self.items: dict[str, CompressItemWidget] = {}
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(10)

        stats = QHBoxLayout()
        stats.setContentsMargins(4, 0, 4, 0)
        stats.setSpacing(8)
        self.statTotal = QLabel("")
        self.statDone = QLabel("")
        self.statError = QLabel("")
        for l in (self.statTotal, self.statDone, self.statError):
            l.setObjectName("queueSub")
            stats.addWidget(l)
        stats.addStretch(1)
        self.layout.addLayout(stats)

        self.listLayout = QVBoxLayout()
        self.listLayout.setContentsMargins(0, 0, 0, 0)
        self.listLayout.setSpacing(8)
        self.layout.addLayout(self.listLayout)

        self.emptyLabel = QLabel(tr("compress.queue.empty"))
        self.emptyLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyLabel.setObjectName("queueEmpty")
        self.listLayout.addWidget(self.emptyLabel)
        self._update_empty()
        self._update_stats({"total": 0, "done": 0, "failed": 0})

    def _update_empty(self):
        self.emptyLabel.setVisible(len(self.items) == 0)

    def _update_stats(self, c: dict):
        self.statTotal.setText(tr("compress.queue.stats.total", n=c.get("total", 0)))
        self.statDone.setText(tr("compress.queue.stats.done", n=c.get("done", 0)))
        self.statError.setText(tr("compress.queue.stats.error", n=c.get("failed", 0)))

    def add_item(self, item: dict):
        if item["id"] in self.items:
            return
        w = CompressItemWidget(item)
        w.removeRequested.connect(self.removeRequested.emit)
        self.items[item["id"]] = w
        self.listLayout.addWidget(w)
        self._update_empty()

    def set_progress(self, tid: str, pct: int):
        w = self.items.get(tid)
        if w:
            w.set_progress(pct)

    def set_status(self, tid: str, status: str, saved: int = 0, detail: str = ""):
        w = self.items.get(tid)
        if w:
            w.set_status(status, saved, detail)

    def remove_item(self, tid: str):
        w = self.items.pop(tid, None)
        if w:
            self.listLayout.removeWidget(w)
            w.deleteLater()
        self._update_empty()

    def clear(self):
        for w in self.items.values():
            self.listLayout.removeWidget(w)
            w.deleteLater()
        self.items.clear()
        self._update_empty()

    def retranslate(self):
        self.emptyLabel.setText(tr("compress.queue.empty"))


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
class CompressInterface(InterfaceBase):
    def __init__(self, parent=None):
        super().__init__("Compress", tr("nav.compress"), tr("compress.tagline"), parent)
        self.retheme()

        self._items: dict[str, dict] = {}
        self._running = False
        self._paused = False
        self._pool = QThreadPool.globalInstance()
        self._events: dict[str, threading.Event] = {}

        # ---- drop area ----
        self.drop = DropArea()
        self.vbox.addWidget(self.drop)

        # ---- settings card ----
        self._build_settings_card()
        self.vbox.addWidget(self.settingsCard)

        # ---- list ----
        self._build_list_card()
        self.vbox.addWidget(self.listCard, 1)

        # ---- connections ----
        self.drop.filesDropped.connect(self._on_paths)
        self.drop.clicked.connect(self._pick_files)
        self.list.removeRequested.connect(self._on_remove)
        self.startBtn.clicked.connect(self._on_start)
        self.pauseBtn.clicked.connect(self._on_pause)
        self.clearBtn.clicked.connect(self._on_clear)
        self.modeCombo.currentIndexChanged.connect(self._on_mode)
        self.backendCombo.currentIndexChanged.connect(self._refresh_backend_note)

        self._refresh_backend_note()
        self._on_mode(0)

    # -- settings card ----------------------------------------------------
    def _build_settings_card(self):
        self.settingsCard = ThemedCard()
        cv = QVBoxLayout(self.settingsCard)
        cv.setContentsMargins(16, 14, 16, 14)
        cv.setSpacing(10)

        head = QHBoxLayout()
        self.settingsTitle = StrongBodyLabel(tr("compress.settings.title"))
        head.addWidget(self.settingsTitle)
        head.addStretch(1)
        cv.addLayout(head)

        # backend
        self.backendCombo = ComboBox()
        self._fill_backends()
        cv.addWidget(self._row(tr("compress.backend"), self.backendCombo))

        # mode (lossless / lossy)
        self.modeCombo = ComboBox()
        for text, val in ((tr("compress.mode.lossless"), "lossless"),
                          (tr("compress.mode.lossy"), "lossy")):
            self.modeCombo.addItem(text, userData=val)
        self.modeCombo.setCurrentIndex(0)
        cv.addWidget(self._row(tr("compress.mode"), self.modeCombo))

        # quality slider (only meaningful for lossy)
        self.qualitySlider = Slider(Qt.Orientation.Horizontal)
        self.qualitySlider.setRange(1, 100)
        self.qualitySlider.setValue(100)
        self.qualityVal = CaptionLabel("100")
        self.qualityVal.setFixedWidth(34)
        qrow = QHBoxLayout()
        qrow.setContentsMargins(0, 0, 0, 0)
        qrow.setSpacing(10)
        qrow.addWidget(self.qualitySlider, 1)
        qrow.addWidget(self.qualityVal)
        self.qualitySlider.valueChanged.connect(
            lambda v: self.qualityVal.setText(str(v))
        )
        cv.addWidget(self._row(tr("compress.quality"), qrow))

        # target format
        self.targetCombo = ComboBox()
        for f in IMAGE_TARGETS:
            text = tr("compress.target.same") if f == "same" else f.upper()
            self.targetCombo.addItem(text, userData=f)
        self.targetCombo.setCurrentIndex(0)
        cv.addWidget(self._row(tr("compress.target"), self.targetCombo))

        # output mode (same dir + suffix, or fixed folder)
        self.modeFixed = SwitchButton()
        self.modeFixed.setText(tr("compress.output.mode.fixed"))
        self.modeFixed.setChecked(False)
        self.modeFixed.checkedChanged.connect(self._on_out_mode)
        cv.addWidget(self._row(tr("compress.output.mode"), self.modeFixed))

        self.suffixEdit = _line_edit(tr("compress.output.suffix_hint"), "_compressed")
        cv.addWidget(self._row(tr("compress.output.suffix"), self.suffixEdit))

        self.fixedEdit = _line_edit(tr("compress.output.fixed_hint"), "")
        self.fixedEdit.setReadOnly(True)
        self.fixedChoose = PushButton(FIF.FOLDER, tr("convert.output.choose"))
        self.fixedChoose.clicked.connect(self._choose_out)
        frow = QHBoxLayout()
        frow.setContentsMargins(0, 0, 0, 0)
        frow.setSpacing(8)
        frow.addWidget(self.fixedEdit, 1)
        frow.addWidget(self.fixedChoose)
        cv.addWidget(self._row(tr("compress.output.folder"), frow))
        self._on_out_mode(False)

        self.backendNote = CaptionLabel("")
        self.backendNote.setObjectName("queueSub")
        cv.addWidget(self.backendNote)

        # in-app download for the optional external compressors
        self.toolsBtn = PushButton(FIF.DOWNLOAD, tr("compress.tools.download"))
        self.toolsBtn.clicked.connect(self._on_download_tools)
        cv.addWidget(self._row(tr("compress.tools"), self.toolsBtn))

    def _fill_backends(self):
        # Clear first so a language-switch retranslate doesn't append duplicates
        # (the old items were still present, yielding two "自动选择"/"Pillow").
        self.backendCombo.clear()
        self.backendCombo.addItem(tr("compress.backend.auto"), userData="auto")
        backs = compressor.available_backends()
        order = ["pillow", "oxipng", "optipng", "mozjpeg"]
        for bid in order:
            if bid in backs:
                self.backendCombo.addItem(backs[bid]["name"], userData=bid)

    def _refresh_backend_note(self):
        backs = compressor.available_backends()
        names = [b["name"] for b in backs.values()]
        self.backendNote.setText(tr("compress.backend.note", list="、".join(names) if names else tr("compress.backend.none")))

    def _on_download_tools(self):
        """One-click download of the optional external compressors into tools/."""
        from ..core.tools_download import ToolsDownloadAllWorker

        self.toolsBtn.setEnabled(False)
        self.toolsBtn.setText(tr("compress.tools.downloading"))
        tools_dir = compressor.tools_dir()
        worker = ToolsDownloadAllWorker(str(tools_dir))
        worker.signals.started.connect(lambda: self.toolsBtn.setText(tr("compress.tools.downloading")))
        worker.signals.finished.connect(self._on_tools_downloaded)
        QThreadPool.globalInstance().start(worker)

    def _on_tools_downloaded(self, results: dict):
        self.toolsBtn.setEnabled(True)
        self.toolsBtn.setText(tr("compress.tools.download"))
        self._fill_backends()
        self._refresh_backend_note()
        failed = {tid: r for tid, (ok, r) in results.items() if not ok}
        if not failed:
            InfoBar.success(tr("compress.tools.done"), "", parent=self.window(),
                            duration=2500, position=InfoBarPosition.TOP_RIGHT)
        else:
            msgs = "; ".join(f"{tid}: {r}" for tid, r in failed.items())
            InfoBar.warning(tr("compress.tools.failed", msg=msgs), "", parent=self.window(),
                            duration=5000, position=InfoBarPosition.TOP_RIGHT)

    @staticmethod
    def _row(label: str, control) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        lab = BodyLabel(label)
        lab.setFixedWidth(96)
        h.addWidget(lab)
        if isinstance(control, QLayout):
            h.addLayout(control, 1)
        else:
            h.addWidget(control, 1)
        return row

    def _on_mode(self, _index):
        lossy = self.modeCombo.currentData() == "lossy"
        self.qualitySlider.setEnabled(lossy)
        self.qualityVal.setEnabled(lossy)

    def _on_out_mode(self, fixed: bool):
        self.fixedEdit.setEnabled(fixed)
        self.fixedChoose.setEnabled(fixed)
        self.suffixEdit.setEnabled(not fixed)

    def _choose_out(self):
        from ..core.qt_compat import QFileDialog

        d = QFileDialog.getExistingDirectory(self, tr("compress.output.folder"), "")
        if d:
            self.fixedEdit.setText(d)

    # -- list card --------------------------------------------------------
    def _build_list_card(self):
        self.listCard = ThemedCard()
        lv = QVBoxLayout(self.listCard)
        lv.setContentsMargins(16, 14, 16, 14)
        lv.setSpacing(10)

        q_head = QHBoxLayout()
        self.queueTitle = StrongBodyLabel(tr("compress.queue.title"))
        q_head.addWidget(self.queueTitle)
        q_head.addStretch(1)
        lv.addLayout(q_head)

        self.list = CompressListWidget()
        self.list.removeRequested.connect(self._on_remove)

        # Internal scrollbar so the compress list does not push the whole page.
        self.listScroll = ScrollArea()
        self.listScroll.setWidgetResizable(True)
        self.listScroll.setWidget(self.list)
        self.listScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.listScroll.setStyleSheet("background-color: transparent; border: none;")
        self.listScroll.setMaximumHeight(420)
        lv.addWidget(self.listScroll, 1)

        self.controls = QVBoxLayout()
        self.controls.setSpacing(8)
        self.startBtn = PrimaryPushButton(FIF.PLAY, tr("convert.btn.start"))
        self.pauseBtn = PushButton(FIF.PAUSE, tr("convert.btn.pause"))
        self.pauseBtn.setEnabled(False)
        self.clearBtn = PushButton(tr("convert.btn.clear"))
        self.controls.addWidget(self.startBtn)
        self.controls.addWidget(self.pauseBtn)
        self.controls.addWidget(self.clearBtn)
        lv.addLayout(self.controls)

    # -- adding files -----------------------------------------------------
    def _expand(self, paths):
        out = []
        for p in paths:
            pp = Path(p)
            if pp.is_dir():
                for f in pp.iterdir():
                    if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                        out.append(str(f))
            elif pp.is_file() and pp.suffix.lower() in IMAGE_EXTS:
                out.append(str(pp))
        return out

    def _on_paths(self, paths):
        expanded = self._expand(paths)
        if not expanded:
            InfoBar.warning(tr("convert.toast.empty"), "", parent=self.window(),
                            duration=2000, position=InfoBarPosition.TOP_RIGHT)
            return
        self._add_items(expanded)

    def _pick_files(self):
        from ..core.qt_compat import QFileDialog

        exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXTS))
        files, _ = QFileDialog.getOpenFileNames(
            self, tr("compress.btn.add"), "", f"Images ({exts});;All Files (*.*)"
        )
        if files:
            self._on_paths(files)

    def _add_items(self, paths):
        for p in paths:
            if any(it["input_path"] == p for it in self._items.values()):
                continue
            item = {
                "id": uuid.uuid4().hex[:12],
                "input_path": p,
                "target_fmt": "same",
                "mode": "lossless",
                "quality": 100,
                "opts": {},
                "backend": "auto",
            }
            self._items[item["id"]] = item
            self.list.add_item(item)
        self._update_stats()

    def _on_remove(self, tid: str):
        self._items.pop(tid, None)
        self.list.remove_item(tid)
        self._update_stats()

    def _on_clear(self):
        self._items.clear()
        self.list.clear()
        self._update_stats()

    # -- run --------------------------------------------------------------
    def _current_target(self) -> str:
        return self.targetCombo.currentData() or "same"

    def _current_opts(self) -> dict:
        mode = self.modeCombo.currentData()
        backend = self.backendCombo.currentData()
        # mirror the advanced image-compress option groups so the dedicated
        # interface shares the same tuning as conversion-time compression.
        from ..core import advanced

        opts: dict = {}
        if mode == "lossless":
            opts = dict(advanced.get("image").get("png_oxipng", {}))
            opts.update(advanced.get("image").get("jpg_mozjpeg", {}))
        else:
            opts = {"quality": self.qualitySlider.value(),
                    "progressive": True, "strip": True,
                    "arithmetic": False, "level": 2, "interlace": False}
        return opts

    def _unique_path(self, path: Path) -> Path:
        if not path.exists():
            return path
        i = 1
        while True:
            c = path.parent / f"{path.stem}_{i}{path.suffix}"
            if not c.exists():
                return c
            i += 1

    def _out_dir(self) -> str:
        if self.modeFixed.isChecked() and self.fixedEdit.text().strip():
            return self.fixedEdit.text().strip()
        return ""

    def _on_start(self):
        if not self._items:
            InfoBar.warning(tr("convert.toast.empty"), "", parent=self.window(),
                            duration=2000, position=InfoBarPosition.TOP_RIGHT)
            return
        target = self._current_target()
        mode = self.modeCombo.currentData()
        quality = self.qualitySlider.value()
        opts = self._current_opts()
        backend = self.backendCombo.currentData() or "auto"
        suffix = self.suffixEdit.text().strip() or "_compressed"
        out_dir = self._out_dir()

        for item in self._items.values():
            if item.get("_status") in ("running", "done"):
                continue
            item["target_fmt"] = target
            item["mode"] = mode
            item["quality"] = quality
            item["opts"] = opts
            item["backend"] = backend
            src = Path(item["input_path"])
            base = Path(out_dir) if out_dir else src.parent
            base.mkdir(parents=True, exist_ok=True)
            ext = src.suffix if target == "same" else "." + target
            out_path = self._unique_path(base / (src.stem + suffix + ext))
            item["_out"] = str(out_path)
            item["_status"] = "pending"

        self._running = True
        self._paused = False
        self._fill_slots()

    def _on_pause(self):
        if self._running and not self._paused:
            self._paused = True
            self.pauseBtn.setText(tr("convert.btn.resume"))
            self.pauseBtn.setIcon(FIF.PLAY)
        elif self._paused:
            self._paused = False
            self.pauseBtn.setText(tr("convert.btn.pause"))
            self.pauseBtn.setIcon(FIF.PAUSE)
            self._fill_slots()

    def _fill_slots(self):
        if self._paused:
            return
        max_threads = max(1, min(int(cfg.maxThreads.value), 8))
        self._pool.setMaxThreadCount(max_threads)
        running = sum(1 for it in self._items.values() if it.get("_status") == "running")
        for item in list(self._items.values()):
            if running >= max_threads:
                break
            if item.get("_status") != "pending":
                continue
            self._launch(item)
            running += 1

    def _launch(self, item: dict):
        item["_status"] = "running"
        self.list.set_status(item["id"], "running")
        worker = CompressWorker(item, item["_out"])
        worker.signals.progress.connect(
            lambda tid, pct: self.list.set_progress(tid, pct)
        )
        worker.signals.finished.connect(self._on_finished)
        self._pool.start(worker)

    def _on_finished(self, tid: str, ok: bool, saved: int, detail: str):
        item = self._items.get(tid)
        if item:
            item["_status"] = "done" if ok else "failed"
        self.list.set_status(tid, "done" if ok else "failed", saved, detail)
        self._update_stats()
        if not self._paused:
            self._fill_slots()

    def _update_stats(self):
        c = {"total": len(self._items),
             "done": sum(1 for it in self._items.values() if it.get("_status") == "done"),
             "failed": sum(1 for it in self._items.values() if it.get("_status") == "failed")}
        self.list._update_stats(c)

    # -- theme / i18n -----------------------------------------------------
    def retheme(self):
        super().retheme()
        self.setStyleSheet(f"""
        FluentLabelBase {{ background-color: transparent; }}
        #queueSub {{ color: {sub_text()}; background-color: transparent; }}
        #queueEmpty {{ color: {muted_text()}; padding: 30px; background-color: transparent; }}
        """)

    def retranslateUi(self):
        self.retranslate(tr("nav.compress"), tr("compress.tagline"))
        self.settingsTitle.setText(tr("compress.settings.title"))
        self.toolsBtn.setText(tr("compress.tools.download"))
        self.queueTitle.setText(tr("compress.queue.title"))
        self.drop.retranslate()
        self._fill_backends()
        self._refresh_backend_note()
        self._update_stats()
        self.startBtn.setText(tr("convert.btn.start"))
        self.pauseBtn.setText(tr("convert.btn.pause"))
        self.clearBtn.setText(tr("convert.btn.clear"))


def _line_edit(placeholder: str, text: str) -> "QLineEdit":
    from qfluentwidgets import LineEdit

    le = LineEdit()
    le.setPlaceholderText(placeholder)
    le.setText(text)
    return le
