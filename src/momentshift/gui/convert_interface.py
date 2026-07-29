"""The primary "Convert" interface: drop files, pick a format, batch convert."""

from pathlib import Path

from ..core.qt_compat import QFileDialog, QWidget, QVBoxLayout, QHBoxLayout, Signal, Qt
from qfluentwidgets import (
    CardWidget,
    FluentIcon as FIF,
    PrimaryPushButton,
    PushButton,
    ComboBox,
    LineEdit,
    StrongBodyLabel,
    CaptionLabel,
    FlowLayout,
    InfoBar,
    InfoBarPosition,
    MessageBox,
)
from ..core.config import cfg
from ..core.presets import TARGET_GROUPS, PROFILES, guess_category, IMAGE_EXTS, AUDIO_EXTS, VIDEO_EXTS
from ..core.models import Task
from ..i18n.translator import tr
from .base import InterfaceBase
from .drop_area import DropArea
from .queue_widget import QueueListWidget
from .ffmpeg_card import FfmpegCard

ALL_EXTS = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS


class ConvertInterface(InterfaceBase):
    def __init__(self, manager, parent=None):
        super().__init__("Convert", tr("nav.convert"), tr("app.tagline"), parent)
        self.manager = manager
        self._current_category = "image"
        self._target_format = "jpg"
        self._chip_buttons: dict[str, PushButton] = {}
        self._run_active = False

        self.setStyleSheet(
            """
            #dropTitle { font-size: 18px; font-weight: 600; }
            #dropHint  { color: rgba(128,128,128,1); }
            #dropFormats { color: rgba(150,150,150,1); font-size: 12px; }
            #queueSub { color: rgba(128,128,128,1); }
            #queueEmpty { color: rgba(140,140,140,1); padding: 30px; }
            #queueStatus { color: rgba(128,128,128,1); }
            """
        )

        # ---- ffmpeg status / acquisition card (start screen) ----
        self.ffmpegCard = FfmpegCard()
        self.vbox.addWidget(self.ffmpegCard)

        # ---- drop area ----
        self.drop = DropArea()
        self.vbox.addWidget(self.drop)

        # ---- target format card ----
        self.targetCard = CardWidget()
        tcv = QVBoxLayout(self.targetCard)
        tcv.setContentsMargins(16, 14, 16, 14)
        tcv.setSpacing(10)

        t_head = QHBoxLayout()
        self.targetLabel = StrongBodyLabel(tr("convert.target.label"))
        self.formatCombo = ComboBox()
        self.formatCombo.setFixedWidth(150)
        self._build_combo()
        t_head.addWidget(self.targetLabel)
        t_head.addStretch(1)
        t_head.addWidget(self.formatCombo)

        self.chipLayout = FlowLayout()
        self.chipLayout.setContentsMargins(0, 0, 0, 0)
        self.chipLayout.setSpacing(8)

        tcv.addLayout(t_head)
        tcv.addLayout(self.chipLayout)
        self.vbox.addWidget(self.targetCard)

        # ---- output folder card ----
        self.outputCard = CardWidget()
        ocv = QHBoxLayout(self.outputCard)
        ocv.setContentsMargins(16, 12, 16, 12)
        self.outputLabel = StrongBodyLabel(tr("convert.output.label"))
        self.outputLine = LineEdit()
        self.outputLine.setReadOnly(True)
        self.outputLine.setPlaceholderText(tr("convert.output.same_dir"))
        self.outputLine.setText(cfg.outputFolder.value)
        self.outputChoose = PushButton(tr("convert.output.choose"), icon=FIF.FOLDER)
        self.outputChoose.clicked.connect(self._choose_output)
        ocv.addWidget(self.outputLabel)
        ocv.addStretch(1)
        ocv.addWidget(self.outputLine, 1)
        ocv.addWidget(self.outputChoose)
        self.vbox.addWidget(self.outputCard)

        # ---- queue card ----
        self.queueCard = CardWidget()
        qcv = QVBoxLayout(self.queueCard)
        qcv.setContentsMargins(16, 14, 16, 14)
        qcv.setSpacing(10)

        q_head = QHBoxLayout()
        self.queueTitle = StrongBodyLabel(tr("convert.queue.title"))
        self.queueCount = CaptionLabel("")
        q_head.addWidget(self.queueTitle)
        q_head.addStretch(1)
        q_head.addWidget(self.queueCount)

        self.queueList = QueueListWidget()
        self.controls = QHBoxLayout()
        self.addBtn = PushButton(tr("convert.btn.add"), icon=FIF.ADD)
        self.startBtn = PrimaryPushButton(icon=FIF.PLAY, text=tr("convert.btn.start"))
        self.pauseBtn = PushButton(icon=FIF.PAUSE, text=tr("convert.btn.pause"))
        self.pauseBtn.setEnabled(False)
        self.clearBtn = PushButton(tr("convert.btn.clear"))
        self.controls.addWidget(self.addBtn)
        self.controls.addWidget(self.startBtn)
        self.controls.addWidget(self.pauseBtn)
        self.controls.addWidget(self.clearBtn)

        qcv.addLayout(q_head)
        qcv.addWidget(self.queueList, 1)
        qcv.addLayout(self.controls)
        self.vbox.addWidget(self.queueCard, 1)

        # ---- init selections ----
        self._build_chips()
        self._set_combo(self._target_format)

        # ---- connections ----
        self.drop.filesDropped.connect(self._on_paths)
        self.drop.clicked.connect(self._pick_files)
        self.addBtn.clicked.connect(self._pick_files)
        self.startBtn.clicked.connect(self._on_start)
        self.pauseBtn.clicked.connect(self._on_pause)
        self.clearBtn.clicked.connect(self._on_clear)
        self.formatCombo.currentIndexChanged.connect(self._on_combo)
        self.queueList.removeRequested.connect(self.manager.remove)
        self.queueList.retryRequested.connect(self.manager.retry)
        self.queueList.formatChanged.connect(self._on_row_format)
        self.manager.queue_changed.connect(self._sync_queue)
        self.manager.progress_updated.connect(self.queueList.update_progress)
        self.manager.task_finished.connect(self._on_finished)
        self.manager.state_changed.connect(self._on_state_changed)
        self.ffmpegCard.ffmpeg_ready.connect(self._on_ffmpeg_ready)

    # ================================================================== #
    # Format selection
    # ================================================================== #
    def _build_combo(self):
        self.formatCombo.blockSignals(True)
        if hasattr(self.formatCombo, "clear"):
            self.formatCombo.clear()
        first = True
        for cat in ("image", "audio", "video"):
            if not first and hasattr(self.formatCombo, "addSeparator"):
                self.formatCombo.addSeparator()
            first = False
            for fmt in TARGET_GROUPS[cat]:
                self.formatCombo.addItem(fmt.upper(), userData=fmt)
        self.formatCombo.blockSignals(False)

    def _set_combo(self, fmt: str):
        self.formatCombo.blockSignals(True)
        for i in range(self.formatCombo.count()):
            self.formatCombo.setCurrentIndex(i)
            if self.formatCombo.currentData() == fmt:
                break
        self.formatCombo.blockSignals(False)

    def _build_chips(self):
        while self.chipLayout.count():
            w = self.chipLayout.takeAt(0)
            if w:
                w.deleteLater()
        self._chip_buttons.clear()
        for fmt in TARGET_GROUPS[self._current_category]:
            btn = PushButton(fmt.upper())
            btn.setCheckable(True)
            btn.setFixedHeight(30)
            btn.setChecked(fmt == self._target_format)
            btn.clicked.connect(lambda _checked, f=fmt: self._on_chip(f))
            self.chipLayout.addWidget(btn)
            self._chip_buttons[fmt] = btn

    def _on_chip(self, fmt: str):
        self._target_format = fmt
        for f, b in self._chip_buttons.items():
            b.setChecked(f == fmt)
        self._set_combo(fmt)

    def _on_combo(self, _index: int):
        fmt = self.formatCombo.currentData()
        if not fmt:
            return
        self._target_format = fmt
        cat = PROFILES[fmt]["category"]
        if cat != self._current_category:
            self._set_category(cat, keep_target=fmt)
        else:
            for f, b in self._chip_buttons.items():
                b.setChecked(f == fmt)

    def _set_category(self, category: str, keep_target: str = None):
        self._current_category = category
        if keep_target and keep_target in TARGET_GROUPS[category]:
            self._target_format = keep_target
        elif self._target_format not in TARGET_GROUPS[category]:
            self._target_format = TARGET_GROUPS[category][0]
        self._build_chips()
        self._set_combo(self._target_format)

    # ================================================================== #
    # Adding files
    # ================================================================== #
    def _expand(self, paths):
        out = []
        for p in paths:
            pp = Path(p)
            if pp.is_dir():
                for f in pp.iterdir():
                    if f.is_file() and f.suffix.lower() in ALL_EXTS:
                        out.append(str(f))
            elif pp.is_file() and pp.suffix.lower() in ALL_EXTS:
                out.append(str(pp))
        return out

    def _gpu_enabled(self) -> bool:
        hw = self.manager.hw
        has_gpu = any(hw.get(k) for k in ("h264", "hevc"))
        mode = cfg.hardware.value
        if mode == "gpu":
            return True
        if mode == "cpu":
            return False
        return bool(has_gpu)

    def _file_filter(self) -> str:
        exts = " ".join(f"*{e}" for e in sorted(ALL_EXTS))
        return f"Media Files ({exts});;All Files (*.*)"

    def _on_paths(self, paths):
        expanded = self._expand(paths)
        if not expanded:
            InfoBar.warning(
                tr("convert.toast.empty"), "", parent=self.window(),
                duration=2000, position=InfoBarPosition.TOP_RIGHT,
            )
            return
        cats = {guess_category(p) for p in expanded}
        cats.discard(None)
        if not cats:
            return
        chosen = self._current_category if self._current_category in cats else sorted(cats)[0]
        self._set_category(chosen)

        matched = [p for p in expanded if guess_category(p) == chosen]
        out_dir = cfg.outputFolder.value or None
        added, skipped = self.manager.add_files(
            matched, self._target_format, out_dir, self._gpu_enabled()
        )
        if added:
            InfoBar.success(
                tr("convert.toast.added", n=len(added)), "", parent=self.window(),
                duration=2000, position=InfoBarPosition.TOP_RIGHT,
            )
        if skipped:
            names = ", ".join(skipped[:3])
            InfoBar.warning(
                tr("convert.warn.same_format", name=names), "", parent=self.window(),
                duration=3000, position=InfoBarPosition.TOP_RIGHT,
            )

    def _pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, tr("convert.btn.add"), "", self._file_filter()
        )
        if files:
            self._on_paths(files)

    def _choose_output(self):
        d = QFileDialog.getExistingDirectory(
            self, tr("convert.output.choose"), cfg.outputFolder.value or ""
        )
        if d:
            cfg.outputFolder.value = d
            self.outputLine.setText(d)

    # ================================================================== #
    # Run controls
    # ================================================================== #
    def _on_start(self):
        if not self.manager.has_ffmpeg:
            InfoBar.error(
                tr("convert.toast.no_ffmpeg"), "", parent=self.window(),
                duration=3000, position=InfoBarPosition.TOP_RIGHT,
            )
            return
        if not self.manager.tasks:
            InfoBar.warning(
                tr("convert.toast.empty"), "", parent=self.window(),
                duration=2000, position=InfoBarPosition.TOP_RIGHT,
            )
            return
        self._run_active = True
        self.manager.start()

    def _on_pause(self):
        if self.manager.is_running and not self.manager.is_paused:
            self.manager.pause()
        else:
            self.manager.resume()

    def _on_clear(self):
        box = MessageBox(tr("common.confirm_clear_title"), tr("common.confirm_clear"), self.window())
        if box.exec():
            self.manager.clear()

    def _on_row_format(self, task_id: str, fmt: str):
        task = self.manager.get_task(task_id)
        if not task:
            return
        task.target_format = fmt
        if task.status in (Task.DONE, Task.FAILED, Task.CANCELED):
            task.status = Task.PENDING
            task.progress = 0
            self.queueList.update_status(task_id, Task.PENDING)
            self._update_count()

    # ================================================================== #
    # Manager signal handlers
    # ================================================================== #
    def _on_ffmpeg_ready(self):
        self.manager.refresh_ffmpeg()

    def _sync_queue(self):
        self.queueList.sync(self.manager.tasks)
        self._update_count()

    def _on_finished(self, task_id: str, ok: bool, log: str):
        self.queueList.update_status(task_id, Task.DONE if ok else Task.FAILED, log)
        self._update_count()
        if not ok:
            task = self.manager.get_task(task_id)
            name = Path(task.input_path).name if task else task_id
            InfoBar.error(
                tr("convert.toast.fail_one", name=name), "", parent=self.window(),
                duration=3000, position=InfoBarPosition.TOP_RIGHT,
            )

    def _on_state_changed(self):
        self._update_controls()
        self._update_count()
        if self._run_active and not self.manager.is_running:
            self._run_active = False
            if self.manager.counts()["done"]:
                InfoBar.success(
                    tr("convert.toast.done"), "", parent=self.window(),
                    duration=2500, position=InfoBarPosition.TOP_RIGHT,
                )

    def _update_controls(self):
        running = self.manager.is_running
        paused = self.manager.is_paused
        self.startBtn.setEnabled(not running)
        self.pauseBtn.setEnabled(running)
        if running and paused:
            self.pauseBtn.setText(tr("convert.btn.resume"))
            self.pauseBtn.setIcon(FIF.PLAY)
        else:
            self.pauseBtn.setText(tr("convert.btn.pause"))
            self.pauseBtn.setIcon(FIF.PAUSE)

    def _update_count(self):
        c = self.manager.counts()
        self.queueCount.setText(f"{c['done']} / {c['total']}")

    # ================================================================== #
    # i18n
    # ================================================================== #
    def retranslateUi(self):
        self.retranslate(tr("nav.convert"), tr("app.tagline"))
        self.drop.retranslate()
        self.ffmpegCard.retranslateUi()
        self.targetLabel.setText(tr("convert.target.label"))
        self.outputLabel.setText(tr("convert.output.label"))
        self.outputLine.setPlaceholderText(tr("convert.output.same_dir"))
        self.queueTitle.setText(tr("convert.queue.title"))
        self.startBtn.setText(tr("convert.btn.start"))
        self.pauseBtn.setText(tr("convert.btn.pause"))
        self.clearBtn.setText(tr("convert.btn.clear"))
        self.addBtn.setText(tr("convert.btn.add"))
        self.queueList.retranslate()
        self._update_count()
