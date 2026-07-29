"""The new "图片 & 视频放大" feature block.

Layout mirrors the Convert interface (drop area -> staging list -> settings ->
queue) and adds the requested **before/after comparison** component and a
**queue** (single or many files are queued, then processed; selecting a queued
file shows its before/after quality in the compare widget).

The heavy lifting is delegated to :mod:`momentshift.core.upscaler`, which wraps
the open-source realesrgan-ncnn-vulkan engine (downloadable in-app, models
bundled with the engine zip).
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from ..core.qt_compat import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, Signal, Qt, QObject, QRunnable,
    QThreadPool, QDesktopServices, QUrl,
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
    HyperlinkButton,
    ProgressBar,
    isDarkTheme,
    Theme,
)
from ..core.config import cfg
from ..core import upscaler
from ..i18n.translator import tr
from .base import InterfaceBase
from .drop_area import DropArea
from .theme import ThemedCard, sub_text, hint_text, muted_text
from .queue_widget import StatusPill, human_size
from .compare_widget import CompareWidget

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
UPSCALE_EXTS = IMAGE_EXTS | VIDEO_EXTS
CATEGORY_ICON = {"image": FIF.PHOTO, "video": FIF.VIDEO}
ENGINE_PAGE = upscaler.ENGINE_PAGE


# --------------------------------------------------------------------------
# Engine status / acquisition card
# --------------------------------------------------------------------------
class EngineCard(ThemedCard):
    engine_ready = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._building = True
        self._init_ui()
        self._refresh()
        self._building = False

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.iconLabel = StrongBodyLabel("!")
        self.iconLabel.setFixedWidth(22)
        self.titleLabel = StrongBodyLabel(tr("upscale.engine.missing"))
        top.addWidget(self.iconLabel)
        top.addWidget(self.titleLabel, 1)
        root.addLayout(top)

        self.hintLabel = CaptionLabel(tr("upscale.engine.hint"))
        self.hintLabel.setWordWrap(True)
        root.addWidget(self.hintLabel)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.linkBtn = HyperlinkButton(ENGINE_PAGE, tr("upscale.engine.open_site"), self)
        self.downloadBtn = PushButton(tr("upscale.engine.oneclick"), icon=FIF.DOWNLOAD)
        self.downloadBtn.clicked.connect(self._on_download)
        btn_row.addWidget(self.linkBtn)
        btn_row.addWidget(self.downloadBtn)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        self.progress = ProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        root.addWidget(self.progress)

    def _refresh(self):
        exe = upscaler.find_upscaler()
        models = len(upscaler.available_models())
        if exe:
            self.iconLabel.setText("✓")
            self.titleLabel.setText(tr("upscale.engine.ok", n=models))
            self.hintLabel.setText(str(Path(exe).parent))
            self.downloadBtn.hide()
            self.linkBtn.hide()
            self.progress.hide()
        else:
            self.iconLabel.setText("!")
            self.titleLabel.setText(tr("upscale.engine.missing"))
            self.hintLabel.setText(tr("upscale.engine.hint"))
            self.downloadBtn.show()
            self.linkBtn.show()
            self.progress.hide()

    def _on_download(self):
        self.downloadBtn.setEnabled(False)
        self.linkBtn.setEnabled(False)
        self.progress.show()
        self.titleLabel.setText(tr("upscale.engine.downloading"))
        worker = upscaler.UpscalerDownloadWorker(str(upscaler.realesrgan_dir()))
        worker.signals.started.connect(lambda: self.titleLabel.setText(tr("upscale.engine.downloading")))
        worker.signals.finished.connect(self._on_done)
        QThreadPool.globalInstance().start(worker)

    def _on_done(self, ok: bool, msg: str):
        self.progress.hide()
        self.downloadBtn.setEnabled(True)
        self.linkBtn.setEnabled(True)
        if ok:
            InfoBar.success(tr("upscale.engine.done"), "", parent=self.window(),
                            duration=2500, position=InfoBarPosition.TOP_RIGHT)
            self._refresh()
            self.engine_ready.emit()
        else:
            InfoBar.error(tr("upscale.engine.failed"), msg or "", parent=self.window(),
                          duration=4000, position=InfoBarPosition.TOP_RIGHT)
            self._refresh()

    def retranslateUi(self):
        self.linkBtn.setText(tr("upscale.engine.open_site"))
        self.downloadBtn.setText(tr("upscale.engine.oneclick"))
        if not self._building:
            self._refresh()


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
class _WorkerSignals(QObject):
    progress = Signal(str, int)
    finished = Signal(str, bool, int, str)


class UpscalerWorker(QRunnable):
    def __init__(self, item: dict, out_path: str, model: str, scale: int, tile: int, gpu: str):
        super().__init__()
        self.setAutoDelete(True)
        self.item = item
        self.out_path = out_path
        self.model = model
        self.scale = scale
        self.tile = tile
        self.gpu = gpu
        self.signals = _WorkerSignals()

    def run(self):
        self.signals.progress.emit(self.item["id"], 0)
        try:
            src = self.item["input_path"]
            src_size = Path(src).stat().st_size
            self.item["src_size"] = src_size
            ok, msg = upscaler.upscale_media(
                src, self.out_path, self.model, self.scale, self.tile, self.gpu
            )
            saved = 0
            if ok:
                try:
                    saved = src_size - Path(self.out_path).stat().st_size
                except OSError:
                    saved = 0
            self.signals.progress.emit(self.item["id"], 100)
            self.signals.finished.emit(self.item["id"], ok, saved, msg)
        except Exception as exc:  # pragma: no cover - defensive
            self.signals.finished.emit(self.item["id"], False, 0, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# Queue item
# --------------------------------------------------------------------------
class UpscaleItemWidget(ThemedCard):
    removeRequested = Signal(str)
    compareRequested = Signal(str)

    def __init__(self, item: dict, parent=None):
        super().__init__(parent)
        self.item = item
        self.setMinimumHeight(86)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(6)

        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(8)

        self.iconLabel = QLabel()
        cat = "video" if Path(item["input_path"]).suffix.lower() in VIDEO_EXTS else "image"
        self.iconLabel.setPixmap(
            CATEGORY_ICON.get(cat, FIF.DOCUMENT)
            .icon(Theme.DARK if isDarkTheme() else Theme.AUTO)
            .pixmap(22, 22)
        )
        self.iconLabel.setFixedSize(24, 24)
        self.iconLabel.setStyleSheet("background-color: transparent;")

        self.nameLabel = StrongBodyLabel(Path(item["input_path"]).name)
        self.nameLabel.setToolTip(str(item["input_path"]))

        m = upscaler.MODELS.get(item["model"], {})
        self.badge = QLabel(f"→ {m.get('label', item['model'])} · {item['scale']}x")
        self.badge.setObjectName("queueSub")

        self.statusLabel = StatusPill("pending")

        self.compareBtn = _tool_button(FIF.VIEW, tr("upscale.action.compare"), self)
        self.compareBtn.setFixedSize(28, 28)
        self.compareBtn.clicked.connect(lambda: self.compareRequested.emit(item["id"]))

        self.removeBtn = _tool_button(FIF.DELETE, tr("convert.action.remove"), self)
        self.removeBtn.setFixedSize(28, 28)
        self.removeBtn.clicked.connect(lambda: self.removeRequested.emit(item["id"]))

        row1.addWidget(self.iconLabel)
        row1.addWidget(self.nameLabel, 1)
        row1.addWidget(self.badge)
        row1.addWidget(self.statusLabel)
        row1.addWidget(self.compareBtn)
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
                tr("upscale.result.saved", before=human_size(src),
                   after=human_size(src - saved), pct=f"-{pct:.0f}%")
                + (f"  ·  {detail}" if detail else "")
            )
        elif status == "failed":
            self.detailLabel.setText(detail)
        elif status == "running":
            self.detailLabel.setText(tr("upscale.status.upscaling"))

    def mousePressEvent(self, event):
        self.compareRequested.emit(self.item["id"])
        super().mousePressEvent(event)


def _tool_button(icon, tooltip, parent):
    from qfluentwidgets import TransparentToolButton

    btn = TransparentToolButton(icon, parent)
    btn.setToolTip(tooltip)
    return btn


class _ProgressBar(QWidget):
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

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        bg = QColor(220, 220, 220)
        fill = QColor(32, 128, 240)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, 4, 4)
        if self._value > 0:
            w = r.width() * self._value / 100.0
            p.setBrush(fill)
            p.drawRoundedRect(QRectF(r.x(), r.y(), w, r.height()), 4, 4)


# --------------------------------------------------------------------------
# List container
# --------------------------------------------------------------------------
class UpscaleListWidget(QWidget):
    removeRequested = Signal(str)
    compareRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: transparent; border: none;")
        self.items: dict[str, UpscaleItemWidget] = {}
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

        self.emptyLabel = QLabel(tr("upscale.queue.empty"))
        self.emptyLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyLabel.setObjectName("queueEmpty")
        self.listLayout.addWidget(self.emptyLabel)
        self._update_empty()
        self._update_stats({"total": 0, "done": 0, "failed": 0})

    def _update_empty(self):
        self.emptyLabel.setVisible(len(self.items) == 0)

    def _update_stats(self, c: dict):
        self.statTotal.setText(tr("upscale.queue.stats.total", n=c.get("total", 0)))
        self.statDone.setText(tr("upscale.queue.stats.done", n=c.get("done", 0)))
        self.statError.setText(tr("upscale.queue.stats.error", n=c.get("failed", 0)))

    def add_item(self, item: dict):
        if item["id"] in self.items:
            return
        w = UpscaleItemWidget(item)
        w.removeRequested.connect(self.removeRequested.emit)
        w.compareRequested.connect(self.compareRequested.emit)
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
        self.emptyLabel.setText(tr("upscale.queue.empty"))


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
class UpscaleInterface(InterfaceBase):
    def __init__(self, parent=None):
        super().__init__("Upscale", tr("nav.upscale"), tr("upscale.tagline"), parent)
        self.retheme()

        self._staged: list[str] = []
        self._items: dict[str, dict] = {}
        self._running = False
        self._paused = False
        self._pool = QThreadPool.globalInstance()
        self._selected: str | None = None

        # engine card
        self.engineCard = EngineCard()
        self.vbox.addWidget(self.engineCard)

        # drop area
        self.drop = DropArea()
        self.vbox.addWidget(self.drop)

        # add toolbar
        toolbar = QVBoxLayout()
        toolbar.setSpacing(8)
        self.addBtn = PushButton(FIF.ADD, tr("upscale.btn.add"))
        self.addFolderBtn = PushButton(FIF.FOLDER, tr("upscale.add_folder"))
        toolbar.addWidget(self.addBtn)
        toolbar.addWidget(self.addFolderBtn)
        self.vbox.addLayout(toolbar)

        # staging card
        self._build_staging_card()
        self.vbox.addWidget(self.stagingCard)

        # settings card
        self._build_settings_card()
        self.vbox.addWidget(self.settingsCard)

        # queue card
        self._build_queue_card()
        self.vbox.addWidget(self.queueCard, 1)

        # compare card
        self.compareCard = CompareWidget()
        self.vbox.addWidget(self.compareCard)

        # connections
        self.drop.filesDropped.connect(self._on_paths)
        self.drop.clicked.connect(self._pick_files)
        self.addBtn.clicked.connect(self._pick_files)
        self.addFolderBtn.clicked.connect(self._pick_folder)
        self.startBtn.clicked.connect(self._on_start)
        self.pauseBtn.clicked.connect(self._on_pause)
        self.clearBtn.clicked.connect(self._on_clear)
        self.stagingClear.clicked.connect(self._clear_staging)
        self.list.removeRequested.connect(self._on_remove)
        self.list.compareRequested.connect(self._on_compare)
        self.engineCard.engine_ready.connect(self._refresh_models)
        self.modelCombo.currentIndexChanged.connect(self._on_model)
        self.formatCombo.currentIndexChanged.connect(self._on_format_choice)
        self.gpuSwitch.checkedChanged.connect(self._on_gpu)
        self.modeFixed.checkedChanged.connect(self._on_out_mode)

        self._refresh_models()
        self._refresh_staging()

    # -- settings card ----------------------------------------------------
    def _build_settings_card(self):
        self.settingsCard = ThemedCard()
        cv = QVBoxLayout(self.settingsCard)
        cv.setContentsMargins(16, 14, 16, 14)
        cv.setSpacing(10)

        head = QHBoxLayout()
        self.settingsTitle = StrongBodyLabel(tr("upscale.settings.title"))
        head.addWidget(self.settingsTitle)
        head.addStretch(1)
        cv.addLayout(head)

        # model
        self.modelCombo = ComboBox()
        cv.addWidget(self._row(tr("upscale.model"), self.modelCombo))

        # scale
        self.scaleCombo = ComboBox()
        for s in (2, 3, 4):
            self.scaleCombo.addItem(f"{s}x", userData=s)
        self.scaleCombo.setCurrentIndex(2)  # default 4x
        cv.addWidget(self._row(tr("upscale.scale"), self.scaleCombo))

        # output format (images)
        self.formatCombo = ComboBox()
        for key, val in (("upscale.fmt.png", "png"), ("upscale.fmt.jpg", "jpg"),
                         ("upscale.fmt.webp", "webp")):
            self.formatCombo.addItem(tr(key), userData=val)
        self.formatCombo.setCurrentIndex(0)
        cv.addWidget(self._row(tr("upscale.output.fmt"), self.formatCombo))

        # tile
        self.tileCombo = ComboBox()
        for label, val in ((tr("upscale.tile.auto"), 0), ("256", 256), ("512", 512)):
            self.tileCombo.addItem(label, userData=val)
        self.tileCombo.setCurrentIndex(0)
        cv.addWidget(self._row(tr("upscale.tile"), self.tileCombo))

        # gpu
        self.gpuSwitch = SwitchButton()
        self.gpuSwitch.setText(tr("upscale.gpu.auto"))
        self.gpuSwitch.setChecked(True)
        cv.addWidget(self._row(tr("upscale.gpu"), self.gpuSwitch))

        # output mode
        self.modeFixed = SwitchButton()
        self.modeFixed.setText(tr("compress.output.mode.fixed"))
        self.modeFixed.setChecked(False)
        self.modeFixed.checkedChanged.connect(self._on_out_mode)
        cv.addWidget(self._row(tr("upscale.output.mode"), self.modeFixed))

        self.suffixEdit = _line_edit(tr("upscale.output.suffix_hint"), "_upscaled")
        cv.addWidget(self._row(tr("upscale.output.suffix"), self.suffixEdit))

        self.fixedEdit = _line_edit(tr("compress.output.fixed_hint"), "")
        self.fixedEdit.setReadOnly(True)
        self.fixedChoose = PushButton(FIF.FOLDER, tr("convert.output.choose"))
        self.fixedChoose.clicked.connect(self._choose_out)
        frow = QHBoxLayout()
        frow.setContentsMargins(0, 0, 0, 0)
        frow.setSpacing(8)
        frow.addWidget(self.fixedEdit, 1)
        frow.addWidget(self.fixedChoose)
        cv.addWidget(self._row(tr("upscale.output.folder"), frow))
        self._on_out_mode(False)

    def _refresh_models(self):
        self.modelCombo.clear()
        for mid, meta in upscaler.MODELS.items():
            self.modelCombo.addItem(f"{meta['label']} · {meta['scale']}x", userData=mid)
        # default to general photo model
        self._select_model("realesrgan-x4plus")

    def _select_model(self, mid: str):
        for i in range(self.modelCombo.count()):
            self.modelCombo.setCurrentIndex(i)
            if self.modelCombo.currentData() == mid:
                break
        self._sync_scale_to_model()

    def _sync_scale_to_model(self):
        mid = self.modelCombo.currentData()
        native = upscaler.MODELS.get(mid, {}).get("scale", 4)
        for i in range(self.scaleCombo.count()):
            self.scaleCombo.setCurrentIndex(i)
            if self.scaleCombo.currentData() == native:
                break

    def _on_model(self, _index):
        self._sync_scale_to_model()

    def _on_format_choice(self, _index):
        pass

    def _on_gpu(self, checked: bool):
        # checked => auto (use GPU if available); unchecked => force CPU
        pass

    def _on_out_mode(self, fixed: bool):
        self.fixedEdit.setEnabled(fixed)
        self.fixedChoose.setEnabled(fixed)
        self.suffixEdit.setEnabled(not fixed)

    def _choose_out(self):
        from ..core.qt_compat import QFileDialog

        d = QFileDialog.getExistingDirectory(self, tr("upscale.output.folder"), "")
        if d:
            self.fixedEdit.setText(d)

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

    # -- staging card -----------------------------------------------------
    def _build_staging_card(self):
        self.stagingCard = ThemedCard()
        scv = QVBoxLayout(self.stagingCard)
        scv.setContentsMargins(16, 14, 16, 14)
        scv.setSpacing(10)

        head = QHBoxLayout()
        self.stagingTitle = StrongBodyLabel(tr("upscale.staging.title"))
        self.stagingCount = CaptionLabel("")
        self.stagingClear = PushButton(tr("upscale.staging.clear"))
        head.addWidget(self.stagingTitle)
        head.addWidget(self.stagingCount)
        head.addStretch(1)
        head.addWidget(self.stagingClear)
        scv.addLayout(head)

        self.stagingList = QVBoxLayout()
        self.stagingList.setContentsMargins(0, 0, 0, 0)
        self.stagingList.setSpacing(6)
        self.stagingWidget = QWidget()
        self.stagingWidget.setLayout(self.stagingList)
        self.stagingScroll = ScrollArea()
        self.stagingScroll.setWidgetResizable(True)
        self.stagingScroll.setWidget(self.stagingWidget)
        self.stagingScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stagingScroll.setStyleSheet("background-color: transparent; border: none;")
        self.stagingScroll.setMaximumHeight(420)
        scv.addWidget(self.stagingScroll, 1)

        self.addQueueBtn = PrimaryPushButton(tr("upscale.staging.add", n=0))
        self.addQueueBtn.clicked.connect(self._on_add_to_queue)
        scv.addWidget(self.addQueueBtn)
        self.stagingCard.setVisible(False)

    # -- queue card -------------------------------------------------------
    def _build_queue_card(self):
        self.queueCard = ThemedCard()
        qcv = QVBoxLayout(self.queueCard)
        qcv.setContentsMargins(16, 14, 16, 14)
        qcv.setSpacing(10)

        q_head = QHBoxLayout()
        self.queueTitle = StrongBodyLabel(tr("upscale.queue.title"))
        q_head.addWidget(self.queueTitle)
        q_head.addStretch(1)
        self.queueCard.addTip = CaptionLabel(tr("upscale.queue.hint"))
        self.queueCard.addTip.setObjectName("queueSub")
        q_head.addWidget(self.queueCard.addTip)
        qcv.addLayout(q_head)

        self.list = UpscaleListWidget()
        self.list.removeRequested.connect(self._on_remove)
        self.list.compareRequested.connect(self._on_compare)

        self.queueScroll = ScrollArea()
        self.queueScroll.setWidgetResizable(True)
        self.queueScroll.setWidget(self.list)
        self.queueScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.queueScroll.setStyleSheet("background-color: transparent; border: none;")
        self.queueScroll.setMaximumHeight(420)
        qcv.addWidget(self.queueScroll, 1)

        self.controls = QVBoxLayout()
        self.controls.setSpacing(8)
        self.startBtn = PrimaryPushButton(FIF.PLAY, tr("convert.btn.start"))
        self.pauseBtn = PushButton(FIF.PAUSE, tr("convert.btn.pause"))
        self.pauseBtn.setEnabled(False)
        self.clearBtn = PushButton(tr("convert.btn.clear"))
        self.controls.addWidget(self.startBtn)
        self.controls.addWidget(self.pauseBtn)
        self.controls.addWidget(self.clearBtn)
        qcv.addLayout(self.controls)

    # -- files -------------------------------------------------------------
    def _expand(self, paths):
        out = []
        for p in paths:
            pp = Path(p)
            if pp.is_dir():
                for f in pp.iterdir():
                    if f.is_file() and f.suffix.lower() in UPSCALE_EXTS:
                        out.append(str(f))
            elif pp.is_file() and pp.suffix.lower() in UPSCALE_EXTS:
                out.append(str(pp))
        return out

    def _on_paths(self, paths):
        expanded = self._expand(paths)
        if not expanded:
            InfoBar.warning(tr("convert.toast.empty"), "", parent=self.window(),
                            duration=2000, position=InfoBarPosition.TOP_RIGHT)
            return
        self._add_to_staging(expanded)

    def _pick_files(self):
        from ..core.qt_compat import QFileDialog

        exts = " ".join(f"*{e}" for e in sorted(UPSCALE_EXTS))
        files, _ = QFileDialog.getOpenFileNames(
            self, tr("upscale.btn.add"), "", f"Media ({exts});;All Files (*.*)"
        )
        if files:
            self._on_paths(files)

    def _pick_folder(self):
        from ..core.qt_compat import QFileDialog

        d = QFileDialog.getExistingDirectory(self, tr("upscale.add_folder"), "")
        if d:
            self._on_paths([d])

    def _add_to_staging(self, paths):
        added = 0
        for p in paths:
            if p not in self._staged:
                self._staged.append(p)
                added += 1
        if added:
            InfoBar.success(tr("upscale.toast.staged", n=added), "", parent=self.window(),
                            duration=2000, position=InfoBarPosition.TOP_RIGHT)
        self._refresh_staging()

    def _refresh_staging(self):
        while self.stagingList.count():
            item = self.stagingList.takeAt(0)
            w = item.widget() if item else None
            if w:
                w.deleteLater()
        for p in self._staged:
            self.stagingList.addWidget(self._make_staged_row(p))
        n = len(self._staged)
        self.stagingCount.setText(f"（{n}）" if n else "")
        self.stagingCard.setVisible(n > 0)
        self.addQueueBtn.setText(tr("upscale.staging.add", n=n))

    def _make_staged_row(self, path: str) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        ext = Path(path).suffix.lower()
        cat = "video" if ext in VIDEO_EXTS else "image"
        icon = QLabel()
        icon.setPixmap(
            CATEGORY_ICON.get(cat, FIF.DOCUMENT)
            .icon(Theme.DARK if isDarkTheme() else Theme.AUTO)
            .pixmap(22, 22)
        )
        icon.setFixedSize(26, 26)
        icon.setStyleSheet("background-color: transparent;")
        name = StrongBodyLabel(Path(path).name)
        sub = CaptionLabel(str(Path(path).parent))
        sub.setObjectName("queueSub")
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        text_col.addWidget(name)
        text_col.addWidget(sub)
        remove = _tool_button(FIF.DELETE, tr("convert.action.remove"), self)
        remove.setFixedSize(30, 30)
        remove.clicked.connect(lambda _=None, p=path: self._staged.remove(p) or self._refresh_staging())
        h.addWidget(icon)
        h.addLayout(text_col, 1)
        h.addWidget(remove)
        return row

    def _clear_staging(self):
        self._staged.clear()
        self._refresh_staging()

    # -- enqueue -----------------------------------------------------------
    def _out_ext_for(self, path: str) -> str:
        ext = Path(path).suffix.lower()
        if ext in IMAGE_EXTS:
            return "." + (self.formatCombo.currentData() or "png")
        return ext  # keep original container for video / gif

    def _unique_path(self, path: Path) -> Path:
        if not path.exists():
            return path
        i = 1
        while True:
            c = path.parent / f"{path.stem}_{i}{path.suffix}"
            if not c.exists():
                return c
            i += 1

    def _on_add_to_queue(self):
        if not self._staged:
            return
        model = self.modelCombo.currentData() or "realesrgan-x4plus"
        scale = self.scaleCombo.currentData() or 4
        tile = self.tileCombo.currentData() or 0
        gpu = "cpu" if not self.gpuSwitch.isChecked() else "auto"
        suffix = self.suffixEdit.text().strip() or "_upscaled"
        fixed = self.modeFixed.isChecked() and self.fixedEdit.text().strip()
        out_dir = Path(self.fixedEdit.text().strip()) if fixed else None

        added = 0
        for p in self._staged:
            item = {
                "id": uuid.uuid4().hex[:12],
                "input_path": p,
                "model": model,
                "scale": scale,
                "tile": tile,
                "gpu": gpu,
                "out_ext": self._out_ext_for(p),
                "suffix": suffix,
                "out_dir": str(out_dir) if out_dir else "",
                "_status": "pending",
            }
            src = Path(p)
            base = out_dir if out_dir else src.parent
            base.mkdir(parents=True, exist_ok=True)
            out_path = self._unique_path(base / (src.stem + suffix + item["out_ext"]))
            item["_out"] = str(out_path)
            self._items[item["id"]] = item
            self.list.add_item(item)
            added += 1

        self._staged.clear()
        self._refresh_staging()
        self._update_stats()
        if added:
            InfoBar.success(tr("upscale.toast.added", n=added), "", parent=self.window(),
                            duration=2000, position=InfoBarPosition.TOP_RIGHT)

    def _on_remove(self, tid: str):
        self._items.pop(tid, None)
        self.list.remove_item(tid)
        if self._selected == tid:
            self._selected = None
            self.compareCard.set_paths(None, None)
        self._update_stats()

    def _on_clear(self):
        self._items.clear()
        self.list.clear()
        self._selected = None
        self.compareCard.set_paths(None, None)
        self._update_stats()

    # -- compare -----------------------------------------------------------
    def _on_compare(self, tid: str):
        item = self._items.get(tid)
        if not item:
            return
        self._selected = tid
        before = item["input_path"]
        after = item.get("_out") if Path(item.get("_out", "")).is_file() else None
        self.compareCard.set_paths(before, after)

    # -- run ---------------------------------------------------------------
    def _on_start(self):
        if not upscaler.find_upscaler():
            InfoBar.error(tr("upscale.toast.no_engine"), "", parent=self.window(),
                          duration=3000, position=InfoBarPosition.TOP_RIGHT)
            return
        if not self._items:
            InfoBar.warning(tr("convert.toast.empty"), "", parent=self.window(),
                            duration=2000, position=InfoBarPosition.TOP_RIGHT)
            return
        for item in self._items.values():
            if item.get("_status") in ("running", "done"):
                continue
            item["model"] = self.modelCombo.currentData() or item["model"]
            item["scale"] = self.scaleCombo.currentData() or item["scale"]
            item["tile"] = self.tileCombo.currentData() or 0
            item["gpu"] = "cpu" if not self.gpuSwitch.isChecked() else "auto"
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
        max_threads = max(1, min(int(cfg.maxThreads.value), 4))
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
        worker = UpscalerWorker(item, item["_out"], item["model"],
                                item["scale"], item["tile"], item["gpu"])
        worker.signals.progress.connect(lambda tid, pct: self.list.set_progress(tid, pct))
        worker.signals.finished.connect(self._on_finished)
        self._pool.start(worker)

    def _on_finished(self, tid: str, ok: bool, saved: int, detail: str):
        item = self._items.get(tid)
        if item:
            item["_status"] = "done" if ok else "failed"
        self.list.set_status(tid, "done" if ok else "failed", saved, detail)
        self._update_stats()
        if ok and self._selected == tid:
            self._on_compare(tid)
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
        if hasattr(self, "compareCard"):
            self.compareCard._restyle()

    def retranslateUi(self):
        self.retranslate(tr("nav.upscale"), tr("upscale.tagline"))
        self.drop.retranslate()
        self.engineCard.retranslateUi()
        self.settingsTitle.setText(tr("upscale.settings.title"))
        self.stagingTitle.setText(tr("upscale.staging.title"))
        self.stagingClear.setText(tr("upscale.staging.clear"))
        self.addQueueBtn.setText(tr("upscale.staging.add", n=len(self._staged)))
        self.addBtn.setText(tr("upscale.btn.add"))
        self.addFolderBtn.setText(tr("upscale.add_folder"))
        self.queueTitle.setText(tr("upscale.queue.title"))
        if hasattr(self, "queueCard"):
            self.queueCard.addTip.setText(tr("upscale.queue.hint"))
        self.startBtn.setText(tr("convert.btn.start"))
        self.pauseBtn.setText(tr("convert.btn.pause"))
        self.clearBtn.setText(tr("convert.btn.clear"))
        self._refresh_models()
        self._update_stats()


def _line_edit(placeholder: str, text: str):
    from qfluentwidgets import LineEdit

    le = LineEdit()
    le.setPlaceholderText(placeholder)
    le.setText(text)
    return le
