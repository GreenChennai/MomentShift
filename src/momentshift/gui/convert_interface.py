"""Convert screen — rebuilt UI (v0.2.7, #4).

Flow: the input card (DropArea + Add folder) stays on the main screen. After
files are picked, a dedicated 800x500 setup dialog (ConvertSetupDialog) opens
with the pending files, target-format picker and advanced options. Confirming
pushes the configured tasks straight into the conversion queue (QueueListWidget),
which also lives on the main screen. The staging / format / advanced UI was
moved out of the main body into that popup per the redesign spec.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QFileDialog, QLabel, QScrollArea,
    QMessageBox,
)
from PyQt6.QtCore import Qt

from qfluentwidgets import (
    FluentIcon as FIF, PrimaryPushButton, SwitchButton,
    TransparentToolButton, CaptionLabel,
)

from ..core.config import cfg
from ..core.models import Task
from ..core.presets import IMAGE_EXTS, AUDIO_EXTS, VIDEO_EXTS
from ..i18n.translator import tr
from .theme import (
    CollapsibleCard, field_row, primary_btn, ghost_btn, scrollbar_qss,
)
from .base import InterfaceBase
from .drop_area import DropArea
from .queue_widget import QueueListWidget
from .convert_setup_dialog import ConvertSetupDialog
from .ffmpeg_card import FfmpegCard


class ConvertInterface(InterfaceBase):
    def __init__(self, manager, parent=None):
        super().__init__("Convert", tr("nav.convert"), tr("convert.subtitle"), parent)
        self.manager = manager
        # Default target format per category, used to seed the setup dialog.
        self._selection = {"image": "jpg", "audio": "mp3", "video": "mp4"}

        # --- ffmpeg status -------------------------------------------------
        self.ffmpegCard = FfmpegCard(self)
        self.ffmpegCard.ffmpeg_ready.connect(self._on_ffmpeg_ready)
        self.vbox.addWidget(self.ffmpegCard)

        # --- input (add media) --------------------------------------------
        card, vb, self.tInput = self._card("convert.input.title")
        self.dropArea = DropArea(self)
        self.dropArea.filesDropped.connect(self._open_setup)
        self.dropArea.clicked.connect(self._pick_files)
        vb.addWidget(self.dropArea)

        tools = QHBoxLayout()
        self.addFolderBtn = primary_btn(tr("convert.add.folder"), icon=FIF.FOLDER_ADD)
        self.addFolderBtn.clicked.connect(self._pick_folder)
        tools.addWidget(self.addFolderBtn)
        vb.addLayout(tools)
        self.vbox.addWidget(card)
        self._inputCard = card

        # --- output location ---------------------------------------------
        ocard, ovb, self.tOutput = self._card("convert.output.title")
        self.outputSwitch = SwitchButton(tr("convert.output.same"))
        self.outputSwitch.checkedChanged.connect(self._on_output_mode)
        ovb.addWidget(field_row(tr("convert.output.mode"), self.outputSwitch))
        self.suffixEdit = QLineEdit(cfg.outputSuffix.value)
        self.suffixEdit.setPlaceholderText(tr("convert.output.suffix.ph"))
        self.suffixEdit.textChanged.connect(lambda t: setattr(cfg.outputSuffix, "value", t))
        self.suffixRow = field_row(tr("convert.output.suffix"), self.suffixEdit)
        ovb.addWidget(self.suffixRow)
        self.folderEdit = QLineEdit(cfg.outputFolder.value)
        self.folderEdit.setReadOnly(True)
        self.browseBtn = TransparentToolButton(FIF.FOLDER, self)
        self.browseBtn.setToolTip(tr("convert.output.browse"))
        self.browseBtn.setFixedSize(36, 36)
        self.browseBtn.clicked.connect(self._pick_output)
        frow = QHBoxLayout()
        frow.addWidget(self.folderEdit, 1)
        frow.addWidget(self.browseBtn)
        self.folderRow = field_row(tr("convert.output.folder"), frow)
        ovb.addWidget(self.folderRow)
        self._apply_output_mode()
        self.vbox.addWidget(ocard)

        # --- queue --------------------------------------------------------
        qcard, qvb, self.tQueue = self._card("convert.queue.title")
        self.queueList = QueueListWidget(self)
        self.queueList.removeRequested.connect(self.manager.remove)
        self.queueList.retryRequested.connect(self.manager.retry)
        self.queueList.formatChanged.connect(self._on_row_format)
        self.queueScroll = self._scroll()
        self.queueScroll.setWidget(self.queueList)
        self.queueScroll.setMinimumHeight(280)  # ~3 items visible
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
        self._auto_fold = [self._inputCard, ocard]

        # --- manager wiring ----------------------------------------------
        self.manager.queue_changed.connect(self._sync_queue)
        self.manager.progress_updated.connect(self.queueList.update_progress)
        self.manager.task_finished.connect(self._on_finished)
        self.manager.state_changed.connect(self._on_state_changed)

        self._update_controls()
        # Cards stay top-aligned; extra vertical space is absorbed by this spacer
        # so a collapsed/short layout never stretches a card to fill the window.
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

    def _scroll(self) -> QScrollArea:
        s = QScrollArea()
        s.setWidgetResizable(True)
        s.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        s.setStyleSheet(
            f"QScrollArea{{border:none; background:transparent;}} {scrollbar_qss()}"
        )
        s.viewport().setStyleSheet("background:transparent;")
        return s

    def _expand(self, paths: list[str]) -> list[str]:
        exts = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS
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
        # de-dupe, preserve order
        seen, uniq = set(), []
        for p in out:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    # -- setup dialog flow ----------------------------------------------
    def _open_setup(self, paths: list[str]):
        """Expand the picked paths and open the 800x500 setup popup."""
        expanded = self._expand(paths)
        if not expanded:
            return
        dlg = ConvertSetupDialog(self, self.manager, expanded, self._selection, self._gpu_enabled)
        if dlg.exec():
            # Remember the chosen formats as the seed for next time.
            self._selection.update(dlg.get_selection())
        self._update_controls()

    def _pick_files(self):
        exts = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS
        flt = "Media (" + " ".join(f"*{e}" for e in sorted(exts)) + ")"
        # Non-native dialog: the native Windows picker replays a mouse-up after
        # closing and re-opens itself (v0.2.7, #3).
        files, _ = QFileDialog.getOpenFileNames(
            self, tr("convert.add.files"), "", flt, "",
            QFileDialog.Option.DontUseNativeDialog,
        )
        if files:
            self._open_setup(files)

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(
            self, tr("convert.add.folder"), "", QFileDialog.Option.DontUseNativeDialog)
        if d:
            self._open_setup([d])

    # -- output ----------------------------------------------------------
    def _on_output_mode(self, checked: bool):
        cfg.outputMode.value = "same" if checked else "fixed"
        self._apply_output_mode()

    def _apply_output_mode(self):
        same = cfg.outputMode.value == "same"
        self.outputSwitch.setChecked(same)
        self.outputSwitch.setText(tr("convert.output.same") if same else tr("convert.output.fixed"))
        self.suffixRow.setVisible(same)
        self.folderRow.setVisible(not same)

    def _pick_output(self):
        d = QFileDialog.getExistingDirectory(
            self, tr("convert.output.browse"), cfg.outputFolder.value or "",
            QFileDialog.Option.DontUseNativeDialog)
        if d:
            cfg.outputFolder.value = d
            self.folderEdit.setText(d)

    # -- queue actions ---------------------------------------------------
    def _gpu_enabled(self) -> bool:
        if cfg.hardware.value == "cpu":
            return False
        if cfg.hardware.value == "gpu":
            return True
        return bool(self.manager.hw)

    def _on_row_format(self, task_id: str, fmt: str):
        self.manager.set_task_target(task_id, fmt)

    def _on_ffmpeg_ready(self):
        self.manager.refresh_ffmpeg()
        self._update_controls()

    def _sync_queue(self):
        self.queueList.sync(self.manager.tasks)
        self._update_count()

    def _on_finished(self, task_id: str, ok: bool, log: str):
        self.queueList.update_status(task_id, Task.DONE if ok else Task.FAILED, log)
        self._update_count()

    def _on_state_changed(self):
        self._update_controls()
        self._update_count()
        if not self.manager.is_running and self.manager.tasks:
            self._auto_collapse_cards()

    def _update_count(self):
        pass  # queueList owns its own stats; nothing extra needed here

    def _update_controls(self):
        running = self.manager.is_running
        has = bool(self.manager.tasks)
        self.startBtn.setEnabled(not running and has and self.manager.has_ffmpeg)
        self.pauseBtn.setEnabled(running)
        self.clearBtn.setEnabled(has)
        if running and self.manager.is_paused:
            self.pauseBtn.setText(tr("convert.resume"))
        else:
            self.pauseBtn.setText(tr("convert.pause"))

    # -- auto-collapse / expand -------------------------------------------
    def _auto_collapse_cards(self):
        """Collapse input/settings cards when a batch finishes (if enabled)."""
        if not cfg.autoCollapse.value:
            return
        for c in self._auto_fold:
            c.setCollapsed(True)

    def _on_start(self):
        if not self.manager.has_ffmpeg:
            QMessageBox.warning(self, tr("common.warning"), tr("convert.start.no_ffmpeg"))
            return
        if not self.manager.tasks:
            return
        same = self.manager.pending_same_format()
        if same:
            ans = QMessageBox.question(
                self, tr("common.warning"), tr("convert.start.same_format"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        self.manager.start()
        self._update_controls()

    def _on_pause(self):
        if self.manager.is_running and not self.manager.is_paused:
            self.manager.pause()
        else:
            self.manager.resume()
        self._update_controls()

    def _on_clear(self):
        self.manager.clear()
        self._update_controls()

    # -- theme / i18n ----------------------------------------------------
    def retheme(self):
        super().retheme()
        self.dropArea.retheme()

    def retranslateUi(self):
        self.titleLabel.setText(tr("nav.convert"))
        self.subLabel.setText(tr("convert.subtitle"))
        self.tInput.setText(tr("convert.input.title"))
        self.tOutput.setText(tr("convert.output.title"))
        self.tQueue.setText(tr("convert.queue.title"))

        self.ffmpegCard.retranslateUi()
        self.dropArea.retranslate(
            tr("convert.drop.title"), tr("convert.drop.hint"), tr("convert.drop.formats"))
        self.addFolderBtn.setText(tr("convert.add.folder"))
        self.suffixEdit.setPlaceholderText(tr("convert.output.suffix.ph"))
        self._apply_output_mode()
        self.queueList.retranslate()
        self.startBtn.setText(tr("convert.start"))
        self.pauseBtn.setText(tr("convert.pause"))
        self.clearBtn.setText(tr("convert.clear"))
        self._update_controls()
