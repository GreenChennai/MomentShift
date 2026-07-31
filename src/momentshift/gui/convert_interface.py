"""转换界面 —— 多媒体格式转换（v0.2.9 重写）。

流程：输入卡（DropArea + 添加文件夹）→ 选取文件 → 800×500 设置弹窗
（ConvertSetupDialog）→ 确认后入队 → ConversionManager 驱动转换队列。

v0.2.9 改动：使用 InterfaceBase 共享组件构建器（_make_card / _make_scroll /
_expand_paths / _auto_collapse），消除与 Compress/Upscale 的重复代码。
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QFileDialog, QScrollArea,
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
    CollapsibleCard, field_row, primary_btn, ghost_btn, muted_text,
)
from .base import InterfaceBase
from .drop_area import DropArea
from .queue_widget import QueueListWidget
from .convert_setup_dialog import ConvertSetupDialog
from .theme import success_color, danger_color

# 转换模块支持的所有媒体类型
_CONVERT_EXTS = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS


class ConvertInterface(InterfaceBase):
    """格式转换标签页。

    使用 ConversionManager（集中式队列管理器）驱动转换任务。
    选文件后弹窗选格式 + 高级参数，确认即入队。
    """

    def __init__(self, manager, parent=None):
        super().__init__("Convert", tr("nav.convert"), tr("convert.subtitle"), parent)
        self.manager = manager
        # 默认目标格式（按媒体大类）
        self._selection = {"image": "jpg", "audio": "mp3", "video": "mp4"}
        # 重入防护（v0.3.0）：模态对话框内部事件循环会递送 synthetic
        # mouseReleaseEvent，此标志确保只有第一次点击真正打开文件选择器
        self._picking = False

        # FFmpeg 状态指示器（v0.3.6：加标签文字，右对齐胶囊）
        ff_wrap = QWidget()
        ff_v = QVBoxLayout(ff_wrap)
        ff_v.setContentsMargins(0, 0, 0, 0)
        ff_v.setSpacing(3)
        self._ff_label = CaptionLabel(tr("convert.ffmpeg_status"))
        self._ff_label.setStyleSheet(f"color: #000000; font-size: 11px;")
        self._ff_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self._ff_status = CaptionLabel("")
        self._ff_status.setFixedHeight(22)
        ff_v.addWidget(self._ff_label)
        ff_v.addWidget(self._ff_status)
        self._header_row.addWidget(ff_wrap)
        self._refresh_ff_status()

        # =====================================================================
        # 输入卡片（拖拽区 + 添加文件夹按钮）
        # =====================================================================
        card, vb, self.tInput = self._make_card("convert.input.title")
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

        # =====================================================================
        # 输出位置卡片（默认折叠）
        # =====================================================================
        ocard, ovb, self.tOutput = self._make_card("convert.output.title", collapsed=True)
        # 输出模式切换开关
        self.outputSwitch = SwitchButton(tr("convert.output.same"))
        self.outputSwitch.checkedChanged.connect(self._on_output_mode)
        ovb.addWidget(field_row(tr("convert.output.mode"), self.outputSwitch))
        # 文件名后缀
        self.suffixEdit = QLineEdit(cfg.outputSuffix.value)
        self.suffixEdit.setPlaceholderText(tr("convert.output.suffix.ph"))
        self.suffixEdit.textChanged.connect(
            lambda t: setattr(cfg.outputSuffix, "value", t))
        self.suffixRow = field_row(tr("convert.output.suffix"), self.suffixEdit)
        ovb.addWidget(self.suffixRow)
        # 固定输出目录
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

        # =====================================================================
        # 转��队列卡片
        # =====================================================================
        qcard, qvb, self.tQueue = self._make_card("convert.queue.title")
        self.queueList = QueueListWidget(self)
        self.queueList.removeRequested.connect(self.manager.remove)
        self.queueList.retryRequested.connect(self.manager.retry)
        self.queueList.formatChanged.connect(self._on_row_format)
        self.queueScroll = self._make_scroll(280)
        self.queueScroll.setWidget(self.queueList)
        qvb.addWidget(self.queueScroll)

        # 队列控制按钮
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

        # 批次完成后自动折叠的卡片（队列始终展开）
        self._auto_fold = [self._inputCard, ocard]

        # =====================================================================
        # 连接 ConversionManager 信号
        # =====================================================================
        self.manager.queue_changed.connect(self._sync_queue)
        self.manager.progress_updated.connect(self.queueList.update_progress)
        self.manager.task_finished.connect(self._on_finished)
        self.manager.compress_started.connect(self.queueList.update_compress_start)
        self.manager.compress_finished.connect(self.queueList.update_compress_done)
        self.manager.state_changed.connect(self._on_state_changed)

        self._update_controls()
        self.vbox.addStretch(1)
        self._collapse_ready = True
        self.retheme()

    # =========================================================================
    # 文件选取 → 设置弹窗流程
    # =========================================================================

    def _gpu_enabled(self) -> bool:
        """判断 GPU 加速是否可用。"""
        if cfg.hardware.value == "cpu":
            return False
        if cfg.hardware.value == "gpu":
            return True
        return bool(self.manager.hw)

    def _open_setup(self, paths: list[str]):
        """展开路径 → 按类别分组 → 每类独立弹出设置弹窗（v0.3.3）。"""
        expanded = self._expand_paths(paths, _CONVERT_EXTS)
        if not expanded:
            return
        from ..core.presets import guess_category
        by_cat: dict[str, list[str]] = {}
        for p in expanded:
            c = guess_category(p)
            if c:
                by_cat.setdefault(c, []).append(p)
        for cat, cat_paths in by_cat.items():
            dlg = ConvertSetupDialog(
                self, self.manager, cat_paths, self._selection,
                self._gpu_enabled, cat)
            if dlg.exec():
                self._selection.update(dlg.get_selection())
        self._update_controls()

    def _pick_files(self):
        """点击 DropArea 弹出文件选择器（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            flt = "Media (" + " ".join(f"*{e}" for e in sorted(_CONVERT_EXTS)) + ")"
            files, _ = QFileDialog.getOpenFileNames(
                None, tr("convert.add.files"), "", flt, "",
            )
            if files:
                self._open_setup(files)
        finally:
            self._picking = False

    def _pick_folder(self):
        """弹出文件夹选择器（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            d = QFileDialog.getExistingDirectory(
                None, tr("convert.add.folder"), "",
                )
            if d:
                self._open_setup([d])
        finally:
            self._picking = False

    # =========================================================================
    # 输出位置设置
    # =========================================================================

    def _on_output_mode(self, checked: bool):
        """切换输出模式：同目录 + 后缀 vs 固定目录。"""
        cfg.outputMode.value = "same" if checked else "fixed"
        self._apply_output_mode()

    def _apply_output_mode(self):
        """根据当前输出模式显示/隐藏对应 UI 行。"""
        same = cfg.outputMode.value == "same"
        self.outputSwitch.setChecked(same)
        self.outputSwitch.setText(
            tr("convert.output.same") if same else tr("convert.output.fixed"))
        self.suffixRow.setVisible(same)
        self.folderRow.setVisible(not same)

    def _pick_output(self):
        """浏览选择固定输出目录（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            d = QFileDialog.getExistingDirectory(
                None, tr("convert.output.browse"), cfg.outputFolder.value or "",
                )
            if d:
                cfg.outputFolder.value = d
                self.folderEdit.setText(d)
        finally:
            self._picking = False

    # =========================================================================
    # 队列操作
    # =========================================================================

    def _on_row_format(self, task_id: str, fmt: str):
        """队列行内格式变更 → 同步到 manager。"""
        self.manager.set_task_target(task_id, fmt)

    def _refresh_ff_status(self):
        """v0.3.5 美化：胶囊样式状态指示器。"""
        ready = self.manager.has_ffmpeg
        if ready:
            self._ff_status.setText("  ✓ 已就绪")
            self._ff_status.setStyleSheet(
                "QLabel{ color:#2ea043; font-weight:600; font-size:12px;"
                " background:rgba(35,134,54,0.08); border:1px solid rgba(35,134,54,0.15);"
                " border-radius: 6px; padding: 2px 10px; }")
        if not ready:
            self._ff_status.setText("  ✗ 不存在")
            self._ff_status.setStyleSheet(
                "QLabel{ color:#d32f2f; font-weight:600; font-size:12px;"
                " background:rgba(211,47,47,0.06); border:1px solid rgba(211,47,47,0.12);"
                " border-radius: 6px; padding: 2px 10px; }")

    def _on_ffmpeg_ready(self):
        """ffmpeg 就绪后刷新引擎并更新控件。"""
        self.manager.refresh_ffmpeg()
        self._refresh_ff_status()
        self._update_controls()

    def _sync_queue(self):
        """manager 队列变更 → 同步到 UI 列表。"""
        self.queueList.sync(self.manager.tasks)

    def _on_finished(self, task_id: str, ok: bool, log: str):
        """单个任务完成 → 更新 UI 状态。"""
        self.queueList.update_status(task_id,
            Task.DONE if ok else Task.FAILED, log)

    def _on_state_changed(self):
        """manager 状态变更 → 更新按钮 + 自动折叠。"""
        self._update_controls()
        if not self.manager.is_running and self.manager.tasks:
            self._auto_collapse(*self._auto_fold)

    def _update_controls(self):
        """根据 manager 状态刷新各按钮的启用/文案。"""
        running = self.manager.is_running
        has = bool(self.manager.tasks)
        self.startBtn.setEnabled(
            not running and has and self.manager.has_ffmpeg)
        self.pauseBtn.setEnabled(running)
        self.clearBtn.setEnabled(has)
        if running and self.manager.is_paused:
            self.pauseBtn.setText(tr("convert.resume"))
        else:
            self.pauseBtn.setText(tr("convert.pause"))

    def _on_start(self):
        """开始转换：检查 ffmpeg 就绪 + 同格式警告后启动。"""
        if not self.manager.has_ffmpeg:
            QMessageBox.warning(
                self, tr("common.warning"), tr("convert.start.no_ffmpeg"))
            return
        if not self.manager.tasks:
            return
        same = self.manager.pending_same_format()
        if same:
            ans = QMessageBox.question(
                self, tr("common.warning"),
                tr("convert.start.same_format"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        self.manager.start()
        self._update_controls()

    def _on_pause(self):
        """暂停 / 继续转换。"""
        if self.manager.is_running and not self.manager.is_paused:
            self.manager.pause()
        else:
            self.manager.resume()
        self._update_controls()

    def _on_clear(self):
        """清空转换队列。"""
        self.manager.clear()
        self._update_controls()

    # =========================================================================
    # 主题 / i18n
    # =========================================================================

    def retheme(self):
        super().retheme()
        self.dropArea.retheme()

    def retranslateUi(self):
        """更新所有 UI 文字（语言切换时触发）。"""
        self.titleLabel.setText(tr("nav.convert"))
        self.subLabel.setText(tr("convert.subtitle"))
        self.tInput.setText(tr("convert.input.title"))
        self.tOutput.setText(tr("convert.output.title"))
        self.tQueue.setText(tr("convert.queue.title"))
        self._refresh_ff_status()
        self.dropArea.retranslate(
            tr("convert.drop.title"), tr("convert.drop.hint"),
            tr("convert.drop.formats"))
        self.addFolderBtn.setText(tr("convert.add.folder"))
        self.suffixEdit.setPlaceholderText(tr("convert.output.suffix.ph"))
        self._apply_output_mode()
        self.queueList.retranslate()
        self.startBtn.setText(tr("convert.start"))
        self.pauseBtn.setText(tr("convert.pause"))
        self.clearBtn.setText(tr("convert.clear"))
        self._update_controls()
