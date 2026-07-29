"""Convert screen — rebuilt UI. Wires to ``ConversionManager`` (no UI logic in core)."""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QFileDialog, QScrollArea,
    QMessageBox, QLabel,
)
from PyQt6.QtCore import Qt

from qfluentwidgets import (
    FluentIcon as FIF, PushButton, PrimaryPushButton, SwitchButton,
    CaptionLabel, StrongBodyLabel, isDarkTheme,
)

from ..core.config import cfg
from ..core.presets import TARGET_GROUPS, guess_category, IMAGE_EXTS, AUDIO_EXTS, VIDEO_EXTS
from ..core.models import Task
from ..i18n.translator import tr
from .theme import (
    ThemedCard, panel, field_row, primary_btn, ghost_btn, icon_btn,
    muted_text, sub_text, CARD_MARGIN,
)
from .base import InterfaceBase
from .drop_area import DropArea
from .queue_widget import QueueListWidget
from .format_grid import FormatGrid
from .advanced_panel import AdvancedPanel
from .ffmpeg_card import FfmpegCard


class ConvertInterface(InterfaceBase):
    def __init__(self, manager, parent=None):
        super().__init__("Convert", tr("nav.convert"), tr("convert.subtitle"), parent)
        self.manager = manager
        self._staged: list[str] = []
        self._selection = {"image": "jpg", "audio": "mp3", "video": "mp4"}

        # --- ffmpeg status -------------------------------------------------
        self.ffmpegCard = FfmpegCard(self)
        self.ffmpegCard.ffmpeg_ready.connect(self._on_ffmpeg_ready)
        self.vbox.addWidget(self.ffmpegCard)

        # --- input --------------------------------------------------------
        card, vb, self.tInput = self._card("convert.input.title", "convert.input.subtitle")
        self.dropArea = DropArea(self)
        self.dropArea.filesDropped.connect(self._on_files)
        self.dropArea.clicked.connect(self._pick_files)
        vb.addWidget(self.dropArea)

        tools = QHBoxLayout()
        self.addFilesBtn = ghost_btn(tr("convert.add.files"), icon=FIF.ADD)
        self.addFilesBtn.clicked.connect(self._pick_files)
        self.addFolderBtn = ghost_btn(tr("convert.add.folder"), icon=FIF.FOLDER_ADD)
        self.addFolderBtn.clicked.connect(self._pick_folder)
        tools.addWidget(self.addFilesBtn)
        tools.addWidget(self.addFolderBtn)
        vb.addLayout(tools)

        self.stagingCount = CaptionLabel(tr("convert.staging.empty"))
        self.stagingCount.setStyleSheet(f"color: {muted_text()};")
        vb.addWidget(self.stagingCount)

        self.stagingScroll = self._scroll()
        self.stagingList = QWidget()
        self.stagingLayout = QVBoxLayout(self.stagingList)
        self.stagingLayout.setContentsMargins(0, 0, 0, 0)
        self.stagingLayout.setSpacing(6)
        self.stagingLayout.addStretch(1)
        self.stagingScroll.setWidget(self.stagingList)
        self.stagingScroll.setMaximumHeight(170)
        vb.addWidget(self.stagingScroll)

        self.addQueueBtn = primary_btn(tr("convert.queue.add"), icon=FIF.UP)
        self.addQueueBtn.clicked.connect(self._on_add_to_queue)
        vb.addWidget(self.addQueueBtn)
        self.vbox.addWidget(card)

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
        self.browseBtn = icon_btn(FIF.FOLDER, tr("convert.output.browse"))
        self.browseBtn.clicked.connect(self._pick_output)
        frow = QHBoxLayout()
        frow.addWidget(self.folderEdit, 1)
        frow.addWidget(self.browseBtn)
        self.folderRow = field_row(tr("convert.output.folder"), frow)
        ovb.addWidget(self.folderRow)
        self._apply_output_mode()
        self.vbox.addWidget(ocard)

        # --- format selection --------------------------------------------
        fcard, fvb, self.tFormat = self._card("convert.format.title", "convert.format.subtitle")
        self.formatGrid = FormatGrid(self)
        self.formatGrid.selectionChanged.connect(self._on_selection)
        fvb.addWidget(self.formatGrid)
        self.vbox.addWidget(fcard)

        # --- advanced -----------------------------------------------------
        acard, avb, self.tAdvanced = self._card("convert.advanced.title", "convert.advanced.subtitle")
        self.advancedPanel = AdvancedPanel(self)
        avb.addWidget(self.advancedPanel)
        self.vbox.addWidget(acard)

        # --- queue --------------------------------------------------------
        qcard, qvb, self.tQueue = self._card("convert.queue.title")
        self.queueList = QueueListWidget(self)
        self.queueList.removeRequested.connect(self.manager.remove)
        self.queueList.retryRequested.connect(self.manager.retry)
        self.queueList.formatChanged.connect(self._on_row_format)
        self.queueScroll = self._scroll()
        self.queueScroll.setWidget(self.queueList)
        self.queueScroll.setMaximumHeight(320)
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

        # --- manager wiring ----------------------------------------------
        self.manager.queue_changed.connect(self._sync_queue)
        self.manager.progress_updated.connect(self.queueList.update_progress)
        self.manager.task_finished.connect(self._on_finished)
        self.manager.state_changed.connect(self._on_state_changed)

        self._render_staging()
        self._refresh_format_grid()
        self._update_controls()
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

    def _scroll(self) -> QScrollArea:
        s = QScrollArea()
        s.setWidgetResizable(True)
        s.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        s.setStyleSheet("QScrollArea{border:none; background:transparent;}")
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

    # -- staging ---------------------------------------------------------
    def _on_files(self, paths: list[str]):
        self._add_staged(self._expand(paths))

    def _pick_files(self):
        exts = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS
        flt = "Media (" + " ".join(f"*{e}" for e in sorted(exts)) + ")"
        files, _ = QFileDialog.getOpenFileNames(self, tr("convert.add.files"), "", flt)
        if files:
            self._add_staged(self._expand(files))

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, tr("convert.add.folder"), "")
        if d:
            self._add_staged(self._expand([d]))

    def _add_staged(self, paths: list[str]):
        if not paths:
            return
        self._staged.extend(paths)
        self._render_staging()
        self._refresh_format_grid()

    def _render_staging(self):
        while self.stagingLayout.count():
            item = self.stagingLayout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not self._staged:
            self.stagingCount.setText(tr("convert.staging.empty"))
            self.stagingLayout.addStretch(1)
            return
        self.stagingCount.setText(tr("convert.staging.count", count=len(self._staged)))
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

    def _remove_staged(self, path: str):
        if path in self._staged:
            self._staged.remove(path)
        self._render_staging()
        self._refresh_format_grid()

    # -- format grid -----------------------------------------------------
    def _refresh_format_grid(self):
        cats = sorted({guess_category(p) for p in self._staged if guess_category(p)})
        self.formatGrid.setup(cats, self._selection)

    def _on_selection(self, selection: dict):
        self._selection.update(selection)

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
        d = QFileDialog.getExistingDirectory(self, tr("convert.output.browse"), cfg.outputFolder.value or "")
        if d:
            cfg.outputFolder.value = d
            self.folderEdit.setText(d)

    # -- queue actions ---------------------------------------------------
    def _on_add_to_queue(self):
        if not self._staged:
            return
        mode = cfg.outputMode.value
        suffix = cfg.outputSuffix.value
        folder = cfg.outputFolder.value or ""
        by_cat: dict[str, list[str]] = {}
        for p in self._staged:
            c = guess_category(p)
            if c:
                by_cat.setdefault(c, []).append(p)
        for cat, paths in by_cat.items():
            fmt = self._selection.get(cat)
            if not fmt:
                continue
            self.manager.add_files(
                paths, fmt, folder if mode == "fixed" else None,
                self._gpu_enabled(), mode, suffix,
            )
        self._staged = []
        self._render_staging()
        self._refresh_format_grid()

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
        self.dropArea.retheme()
        self.formatGrid.retheme()
        self.advancedPanel.retheme()

    def retranslateUi(self):
        self.titleLabel.setText(tr("nav.convert"))
        self.subLabel.setText(tr("convert.subtitle"))
        self.tInput.setText(tr("convert.input.title"))
        self.tOutput.setText(tr("convert.output.title"))
        self.tFormat.setText(tr("convert.format.title"))
        self.tAdvanced.setText(tr("convert.advanced.title"))
        self.tQueue.setText(tr("convert.queue.title"))

        self.ffmpegCard.retranslateUi()
        self.dropArea.retranslate(
            tr("convert.drop.title"), tr("convert.drop.hint"), tr("convert.drop.formats"))
        self.addFilesBtn.setText(tr("convert.add.files"))
        self.addFolderBtn.setText(tr("convert.add.folder"))
        self.addQueueBtn.setText(tr("convert.queue.add"))
        self.suffixEdit.setPlaceholderText(tr("convert.output.suffix.ph"))
        self._apply_output_mode()
        self.formatGrid.retranslate()
        self.advancedPanel.retranslate()
        self.queueList.retranslate()
        self.startBtn.setText(tr("convert.start"))
        self.pauseBtn.setText(tr("convert.pause"))
        self.clearBtn.setText(tr("convert.clear"))
        self._render_staging()
        self._update_controls()
