"""The primary "Convert" interface.

New flow (per product spec):
  1. Add files (drop / pick files / pick folder) -> they land in a *staging*
     list (the "file conversion queue").
  2. Pick a target format from the card-style matrix (one format per media
     category present in the staging list).
  3. "Add to task queue" promotes the staged files into runnable tasks.
  4. Start the conversion, or keep adding more files (the loop repeats).

The output location is chosen up-front via a card placed directly beneath the
FFmpeg status card: either a fixed output folder, or "next to the source file"
with a custom suffix to keep originals intact.
"""

from pathlib import Path

from ..core.qt_compat import QFileDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, Signal, Qt
from qfluentwidgets import (
    CardWidget,
    FluentIcon as FIF,
    PrimaryPushButton,
    PushButton,
    TransparentToolButton,
    LineEdit,
    StrongBodyLabel,
    BodyLabel,
    CaptionLabel,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    RadioButton,
)
from ..core.config import cfg
from ..core.presets import TARGET_GROUPS, PROFILES, guess_category, IMAGE_EXTS, AUDIO_EXTS, VIDEO_EXTS
from ..core.models import Task
from ..i18n.translator import tr
from .base import InterfaceBase
from .drop_area import DropArea
from .queue_widget import QueueListWidget
from .ffmpeg_card import FfmpegCard
from .format_grid import FormatGrid
from .advanced_panel import AdvancedPanel

ALL_EXTS = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS
CATEGORY_ICON = {"image": FIF.PHOTO, "audio": FIF.MUSIC, "video": FIF.VIDEO}


class ConvertInterface(InterfaceBase):
    def __init__(self, manager, parent=None):
        super().__init__("Convert", tr("nav.convert"), tr("app.tagline"), parent)
        self.manager = manager
        self._staged: list[str] = []            # raw files awaiting a format
        self._format_by_cat: dict[str, str] = {}  # category -> chosen format
        self._run_active = False

        self.setStyleSheet(
            """
            #dropTitle { font-size: 18px; font-weight: 600; }
            #dropHint  { color: rgba(128,128,128,1); }
            #dropFormats { color: rgba(150,150,150,1); font-size: 12px; }
            #queueSub { color: rgba(128,128,128,1); }
            #queueEmpty { color: rgba(140,140,140,1); padding: 30px; }
            #queueStatus { color: rgba(128,128,128,1); }
            #stagedSub { color: rgba(128,128,128,1); }
            """
        )

        # ---- ffmpeg status / acquisition card (start screen) ----
        self.ffmpegCard = FfmpegCard()
        self.vbox.addWidget(self.ffmpegCard)

        # ---- output location card (directly under the ffmpeg card) ----
        self._build_output_card()
        self.vbox.addWidget(self.outputCard)

        # ---- drop area ----
        self.drop = DropArea()
        self.vbox.addWidget(self.drop)

        # ---- add toolbar (files / folder) ----
        toolbar = QHBoxLayout()
        self.addBtn = PushButton(FIF.ADD, tr("convert.btn.add"))
        self.addFolderBtn = PushButton(FIF.FOLDER, tr("convert.add_folder"))
        toolbar.addWidget(self.addBtn)
        toolbar.addWidget(self.addFolderBtn)
        toolbar.addStretch(1)
        self.vbox.addLayout(toolbar)

        # ---- staging card (files waiting for a format) ----
        self._build_staging_card()
        self.vbox.addWidget(self.stagingCard)

        # ---- format matrix card ----
        self._build_format_card()
        self.vbox.addWidget(self.formatCard)

        # ---- task queue card ----
        self._build_queue_card()
        self.vbox.addWidget(self.queueCard, 1)

        # ---- connections ----
        self.drop.filesDropped.connect(self._on_paths)
        self.drop.clicked.connect(self._pick_files)
        self.addBtn.clicked.connect(self._pick_files)
        self.addFolderBtn.clicked.connect(self._pick_folder)
        self.startBtn.clicked.connect(self._on_start)
        self.pauseBtn.clicked.connect(self._on_pause)
        self.clearBtn.clicked.connect(self._on_clear)
        self.stagingClear.clicked.connect(self._clear_staging)
        self.queueList.removeRequested.connect(self.manager.remove)
        self.queueList.retryRequested.connect(self.manager.retry)
        self.queueList.formatChanged.connect(self._on_row_format)
        self.manager.queue_changed.connect(self._sync_queue)
        self.manager.progress_updated.connect(self.queueList.update_progress)
        self.manager.task_finished.connect(self._on_finished)
        self.manager.state_changed.connect(self._on_state_changed)
        self.ffmpegCard.ffmpeg_ready.connect(self._on_ffmpeg_ready)

        self._apply_output_mode()
        self._refresh_staging()

    # ================================================================== #
    # Output location card
    # ================================================================== #
    def _build_output_card(self):
        self.outputCard = CardWidget()
        ocv = QVBoxLayout(self.outputCard)
        ocv.setContentsMargins(16, 14, 16, 14)
        ocv.setSpacing(10)

        head = QHBoxLayout()
        self.outputTitle = StrongBodyLabel(tr("convert.output.label"))
        head.addWidget(self.outputTitle)
        head.addStretch(1)
        ocv.addLayout(head)

        mode_row = QHBoxLayout()
        self.fixedRadio = RadioButton(tr("convert.output.mode.fixed"))
        self.sameRadio = RadioButton(tr("convert.output.mode.same"))
        self.fixedRadio.setChecked(cfg.outputMode.value == "fixed")
        self.sameRadio.setChecked(cfg.outputMode.value == "same")
        self.fixedRadio.toggled.connect(self._on_mode_fixed)
        self.sameRadio.toggled.connect(self._on_mode_same)
        mode_row.addWidget(self.fixedRadio)
        mode_row.addWidget(self.sameRadio)
        mode_row.addStretch(1)
        ocv.addLayout(mode_row)

        # fixed folder controls (left label keeps the two rows aligned)
        self.fixedRow = QHBoxLayout()
        fixedLabel = BodyLabel(tr("convert.output.fixed_label"))
        fixedLabel.setFixedWidth(72)
        self.outputLine = LineEdit()
        self.outputLine.setReadOnly(True)
        self.outputLine.setPlaceholderText(tr("convert.output.same_dir"))
        self.outputLine.setText(cfg.outputFolder.value)
        self.outputChoose = PushButton(FIF.FOLDER, tr("convert.output.choose"))
        self.outputChoose.clicked.connect(self._choose_output)
        self.fixedRow.addWidget(fixedLabel)
        self.fixedRow.addWidget(self.outputLine, 1)
        self.fixedRow.addWidget(self.outputChoose)
        ocv.addLayout(self.fixedRow)

        # same-dir + suffix controls (directly under the "same dir" option)
        self.sameRow = QHBoxLayout()
        sameLabel = BodyLabel(tr("convert.output.suffix"))
        sameLabel.setFixedWidth(72)
        self.suffixEdit = LineEdit()
        self.suffixEdit.setPlaceholderText(tr("convert.output.suffix_hint"))
        self.suffixEdit.setText(cfg.outputSuffix.value)
        self.suffixEdit.setFixedWidth(190)
        self.sameRow.addWidget(sameLabel)
        self.sameRow.addWidget(self.suffixEdit)
        self.sameRow.addStretch(1)
        ocv.addLayout(self.sameRow)

        self.sameHint = CaptionLabel(tr("convert.output.same_hint"))
        self.sameHint.setObjectName("stagedSub")
        ocv.addWidget(self.sameHint)

    def _on_mode_fixed(self, checked: bool):
        if checked:
            cfg.outputMode.value = "fixed"
            self._apply_output_mode()

    def _on_mode_same(self, checked: bool):
        if checked:
            cfg.outputMode.value = "same"
            self._apply_output_mode()

    def _apply_output_mode(self):
        fixed = cfg.outputMode.value == "fixed"
        self.fixedRow.setEnabled(fixed)
        self.sameRow.setEnabled(not fixed)

    def _choose_output(self):
        d = QFileDialog.getExistingDirectory(
            self, tr("convert.output.choose"), cfg.outputFolder.value or ""
        )
        if d:
            cfg.outputFolder.value = d
            self.outputLine.setText(d)

    # ================================================================== #
    # Staging card
    # ================================================================== #
    def _build_staging_card(self):
        self.stagingCard = CardWidget()
        scv = QVBoxLayout(self.stagingCard)
        scv.setContentsMargins(16, 14, 16, 14)
        scv.setSpacing(10)

        head = QHBoxLayout()
        self.stagingTitle = StrongBodyLabel(tr("convert.staging.title"))
        self.stagingCount = CaptionLabel("")
        self.stagingClear = PushButton(tr("convert.staging.clear"))
        head.addWidget(self.stagingTitle)
        head.addWidget(self.stagingCount)
        head.addStretch(1)
        head.addWidget(self.stagingClear)
        scv.addLayout(head)

        self.stagingList = QVBoxLayout()
        self.stagingList.setContentsMargins(0, 0, 0, 0)
        self.stagingList.setSpacing(6)
        scv.addLayout(self.stagingList)

        self.advancedPanel = AdvancedPanel()
        scv.addWidget(self.advancedPanel)

        self.stagingCard.setVisible(False)

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
        cats = sorted({guess_category(p) for p in self._staged if guess_category(p)})
        self._refresh_format_cards(cats)
        self.advancedPanel.refresh(cats)

    def _make_staged_row(self, path: str) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        cat = guess_category(path) or "image"
        icon = QLabel()
        icon.setPixmap(CATEGORY_ICON.get(cat, FIF.DOCUMENT).icon().pixmap(22, 22))
        icon.setFixedSize(26, 26)

        name = StrongBodyLabel(Path(path).name)
        sub = CaptionLabel(str(Path(path).parent))
        sub.setObjectName("stagedSub")
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        text_col.addWidget(name)
        text_col.addWidget(sub)

        remove = TransparentToolButton(FIF.DELETE, self)
        remove.setFixedSize(30, 30)
        remove.setToolTip(tr("convert.action.remove"))
        remove.clicked.connect(lambda _=None, p=path: self._remove_staged(p))

        h.addWidget(icon)
        h.addLayout(text_col, 1)
        h.addWidget(remove)
        return row

    def _remove_staged(self, path: str):
        if path in self._staged:
            self._staged.remove(path)
        self._refresh_staging()

    def _clear_staging(self):
        self._staged.clear()
        self._refresh_staging()

    # ================================================================== #
    # Format matrix card
    # ================================================================== #
    def _build_format_card(self):
        self.formatCard = CardWidget()
        fcv = QVBoxLayout(self.formatCard)
        fcv.setContentsMargins(16, 14, 16, 14)
        fcv.setSpacing(10)

        head = QHBoxLayout()
        self.formatTitle = StrongBodyLabel(tr("convert.format.title"))
        self.formatHint = CaptionLabel(tr("convert.format.hint"))
        head.addWidget(self.formatTitle)
        head.addStretch(1)
        head.addWidget(self.formatHint)
        fcv.addLayout(head)

        self.formatGrid = FormatGrid()
        self.formatGrid.selectionChanged.connect(self._on_grid_selection)
        fcv.addWidget(self.formatGrid)

        self.addQueueBtn = PrimaryPushButton(tr("convert.format.add", n=0))
        self.addQueueBtn.clicked.connect(self._on_add_to_queue)
        fcv.addWidget(self.addQueueBtn)
        self.formatCard.setVisible(False)

    def _refresh_format_cards(self, cats=None):
        if cats is None:
            cats = sorted({guess_category(p) for p in self._staged if guess_category(p)})
        for cat in cats:
            if cat not in self._format_by_cat:
                self._format_by_cat[cat] = TARGET_GROUPS[cat][0]
        # Drop stale category selections no longer present.
        self._format_by_cat = {c: f for c, f in self._format_by_cat.items() if c in cats}
        if cats:
            self.formatGrid.setup(cats, self._format_by_cat)
            self.addQueueBtn.setText(tr("convert.format.add", n=len(self._staged)))
            self.formatCard.setVisible(True)
        else:
            self.formatCard.setVisible(False)

    def _on_grid_selection(self, sel: dict[str, str]):
        self._format_by_cat.update(sel)
        self.addQueueBtn.setText(tr("convert.format.add", n=len(self._staged)))

    # ================================================================== #
    # Task queue card
    # ================================================================== #
    def _build_queue_card(self):
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
        self.startBtn = PrimaryPushButton(FIF.PLAY, tr("convert.btn.start"))
        self.pauseBtn = PushButton(FIF.PAUSE, tr("convert.btn.pause"))
        self.pauseBtn.setEnabled(False)
        self.clearBtn = PushButton(tr("convert.btn.clear"))
        self.controls.addWidget(self.startBtn)
        self.controls.addWidget(self.pauseBtn)
        self.controls.addWidget(self.clearBtn)

        qcv.addLayout(q_head)
        qcv.addWidget(self.queueList, 1)
        qcv.addLayout(self.controls)

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
        self._add_to_staging(expanded)

    def _add_to_staging(self, paths):
        added = 0
        for p in paths:
            if p not in self._staged:
                self._staged.append(p)
                added += 1
        if added:
            InfoBar.success(
                tr("convert.toast.staged", n=added), "", parent=self.window(),
                duration=2000, position=InfoBarPosition.TOP_RIGHT,
            )
        self._refresh_staging()

    def _pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, tr("convert.btn.add"), "", self._file_filter()
        )
        if files:
            self._on_paths(files)

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(
            self, tr("convert.add_folder"), cfg.outputFolder.value or ""
        )
        if d:
            self._on_paths([d])

    def _on_add_to_queue(self):
        if not self._staged:
            return
        mode = cfg.outputMode.value
        folder = cfg.outputFolder.value
        suffix = self.suffixEdit.text().strip()
        cfg.outputSuffix.value = suffix
        if mode == "fixed" and not folder:
            InfoBar.warning(
                tr("convert.output.fixed_empty"), "", parent=self.window(),
                duration=3000, position=InfoBarPosition.TOP_RIGHT,
            )
            return

        # Group staged files by category and enqueue per chosen format.
        groups: dict[str, list[str]] = {}
        for p in self._staged:
            c = guess_category(p)
            if c:
                groups.setdefault(c, []).append(p)

        added_total = 0
        skipped_total: list[str] = []
        for cat, ps in groups.items():
            fmt = self._format_by_cat.get(cat) or TARGET_GROUPS[cat][0]
            added, skipped = self.manager.add_files(
                ps, fmt, folder, self._gpu_enabled(), mode, suffix
            )
            added_total += len(added)
            skipped_total += skipped

        self._staged.clear()
        self._refresh_staging()

        if added_total:
            InfoBar.success(
                tr("convert.toast.added", n=added_total), "", parent=self.window(),
                duration=2000, position=InfoBarPosition.TOP_RIGHT,
            )
        if skipped_total:
            names = ", ".join(skipped_total[:3])
            InfoBar.warning(
                tr("convert.warn.unsupported", name=names), "", parent=self.window(),
                duration=3000, position=InfoBarPosition.TOP_RIGHT,
            )

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
        # Same-format conversions are allowed but deserve a heads-up.
        same = self.manager.pending_same_format()
        if same:
            names = ", ".join(Path(t.input_path).name for t in same[:5])
            box = MessageBox(
                tr("convert.warn.same_format_start_title"),
                tr("convert.warn.same_format_start", n=len(same)) + "\n" + names,
                self.window(),
            )
            if not box.exec():
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
        self.manager.set_task_target(task_id, fmt)

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
        self.outputTitle.setText(tr("convert.output.label"))
        self.fixedRadio.setText(tr("convert.output.mode.fixed"))
        self.sameRadio.setText(tr("convert.output.mode.same"))
        self.outputLine.setPlaceholderText(tr("convert.output.same_dir"))
        self.outputChoose.setText(tr("convert.output.choose"))
        self.suffixEdit.setPlaceholderText(tr("convert.output.suffix_hint"))
        self.sameHint.setText(tr("convert.output.same_hint"))
        self.stagingTitle.setText(tr("convert.staging.title"))
        self.stagingClear.setText(tr("convert.staging.clear"))
        self.formatTitle.setText(tr("convert.format.title"))
        self.formatHint.setText(tr("convert.format.hint"))
        self.addQueueBtn.setText(tr("convert.format.add", n=len(self._staged)))
        self.addBtn.setText(tr("convert.btn.add"))
        self.addFolderBtn.setText(tr("convert.add_folder"))
        self.queueTitle.setText(tr("convert.queue.title"))
        self.startBtn.setText(tr("convert.btn.start"))
        self.pauseBtn.setText(tr("convert.btn.pause"))
        self.clearBtn.setText(tr("convert.btn.clear"))
        self.queueList.retranslate()
        self.formatGrid.retranslate()
        self.advancedPanel.retranslate()
        self._update_count()
