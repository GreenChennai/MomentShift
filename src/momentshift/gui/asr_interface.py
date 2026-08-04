"""音频转文字（ASR）界面 —— 视频/音频 → 文字（FunASR HTTP 服务）。

职责边界：
- 做：收集输入文件（视频/音频拖拽或选择）、启动/停止后台转写 worker、把
  worker 信号渲染到两块只读文本区（主 CMD 结果区 + 服务模式日志区）、把完整
  文案保存为 .txt、管理「本地服务模式」配置区。
- 不做：不执行 ffmpeg / HTTP 请求（在 ``core/asr_worker`` / ``core/asr_client``）；
  不持有队列。

架构（v0.8.3）：MomentShift 是 PyInstaller 独立应用，绝不捆绑 FunASR 模型
（884MB+）。本组件是**客户端**，连用户本地/远程已部署的 OpenAI 兼容服务
（``C:\\FunASR\\server.py``：POST /v1/audio/transcriptions、/health、/v1/models）。

服务模式语义：
- 「启用服务模式」开关默认**关**。关闭时用内置默认地址/模型
  （``http://127.0.0.1:8000/v1`` + ``paraformer-zh``）——零配置即可连用户本地
  服务；开启后用配置区里的三件套（持久化到 ``cfg.asrBaseUrl/asrModel/asrApiKey``）。
- 服务模式配置区是独立折叠卡片，默认折叠；卡片内含自己的日志区（请求/响应/
  错误），与主 CMD 结果区分开。
"""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtGui import QTextCursor, QTextOption
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
)
from qfluentwidgets import (
    CaptionLabel,
    PasswordLineEdit,
    SwitchButton,
    TransparentToolButton,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from ..core.asr_client import DEFAULT_BASE_URL, DEFAULT_MODEL, asr_health
from ..core.asr_worker import AsrTranscribeWorker
from ..core.config import cfg
from ..core.ffmpeg import find_ffmpeg, find_ffprobe
from ..core.presets import AUDIO_EXTS, VIDEO_EXTS
from ..core.qt_compat import Signal
from ..i18n.translator import tr
from . import tokens
from .base import InterfaceBase
from .drop_area import DropArea
from .theme import (
    apply_text,
    field_row,
    ghost_btn,
    muted_text,
    primary_btn,
    text_secondary,
    text_strong,
)

# 本组件接受的输入扩展名：视频 + 音频
_ASR_EXTS = VIDEO_EXTS | AUDIO_EXTS
_WRAP_MODE = QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere


class AudioTranscribeInterface(InterfaceBase):
    """音频转文字标签页。"""

    # 健康检查结果从后台线程回传（Qt 队列连接自动切回 GUI 线程）
    _healthResult = Signal(bool, str)

    def __init__(self, parent=None):
        super().__init__("Asr", tr("nav.asr"), tr("asr.subtitle"), parent)
        self._input_path = ""
        self._worker: AsrTranscribeWorker | None = None
        self._health_thread: threading.Thread | None = None
        self._healthResult.connect(self._on_health_result)

        # =====================================================================
        # 输入卡片（拖拽区 + 选择文件按钮 + 当前文件）
        # =====================================================================
        card, vb, self.tInput = self._make_card("asr.input.title")
        self.dropArea = DropArea(self)
        self.dropArea.filesDropped.connect(self._on_files)
        self.dropArea.clicked.connect(self._pick_files)
        vb.addWidget(self.dropArea)

        tools = QHBoxLayout()
        self.addFileBtn = primary_btn(tr("asr.add.file"), icon=FIF.FOLDER_ADD)
        self.addFileBtn.clicked.connect(self._pick_files)
        tools.addWidget(self.addFileBtn)
        vb.addLayout(tools)

        self._fileLabel = CaptionLabel(tr("asr.file.none"))
        apply_text(self._fileLabel, muted_text(), transparent=True)
        vb.addWidget(self._fileLabel)
        self.vbox.addWidget(card)

        # =====================================================================
        # 控制卡片（开始/停止 + 服务状态 + 引导提示 + 进度）
        # =====================================================================
        ccard, cvb, self.tControl = self._make_card("asr.control.title")
        ctrl = QHBoxLayout()
        self.startBtn = primary_btn(tr("asr.start"), icon=FIF.PLAY)
        self.startBtn.clicked.connect(self._on_start)
        self.stopBtn = ghost_btn(tr("asr.stop"), icon=FIF.CANCEL)
        self.stopBtn.clicked.connect(self._on_stop)
        self.stopBtn.setEnabled(False)
        ctrl.addWidget(self.startBtn, 1)
        ctrl.addWidget(self.stopBtn)
        cvb.addLayout(ctrl)

        # 整体进度
        self._progressLabel = CaptionLabel(tr("asr.progress.idle"))
        apply_text(self._progressLabel, text_secondary(), transparent=True)
        cvb.addWidget(self._progressLabel)

        # 服务状态行：状态标签 + 检测按钮
        status_row = QHBoxLayout()
        self._statusLabel = CaptionLabel(tr("asr.status.unknown"))
        apply_text(self._statusLabel, muted_text(), transparent=True)
        status_row.addWidget(self._statusLabel, 1)
        self._checkBtn = TransparentToolButton(FIF.SYNC, self)
        self._checkBtn.setFixedSize(28, 28)
        self._checkBtn.clicked.connect(self._check_service)
        status_row.addWidget(self._checkBtn)
        cvb.addLayout(status_row)

        # 未配置服务时的引导
        self._hintLabel = CaptionLabel(tr("asr.status.hint"))
        self._hintLabel.setWordWrap(True)
        apply_text(self._hintLabel, muted_text(), transparent=True)
        cvb.addWidget(self._hintLabel)
        self.vbox.addWidget(ccard)

        # =====================================================================
        # CMD 结果卡片（完整文案，可滚动、绝不截断）
        # =====================================================================
        rcard, rvb, self.tCmd = self._make_card("asr.cmd.title")
        self.cmdEdit = QPlainTextEdit()
        self.cmdEdit.setReadOnly(True)
        self.cmdEdit.setMinimumHeight(170)
        self.cmdEdit.setWordWrapMode(_WRAP_MODE)
        self.cmdEdit.setPlaceholderText(tr("asr.cmd.ready"))
        apply_text(self.cmdEdit, text_strong(), transparent=False)
        rvb.addWidget(self.cmdEdit)

        save_row = QHBoxLayout()
        self.saveBtn = ghost_btn(tr("asr.save"), icon=FIF.SAVE)
        self.saveBtn.setEnabled(False)
        self.saveBtn.clicked.connect(self._on_save)
        save_row.addStretch(1)
        save_row.addWidget(self.saveBtn)
        rvb.addLayout(save_row)
        self.vbox.addWidget(rcard)

        # =====================================================================
        # 本地服务模式配置卡片（默认折叠 + 默认关闭，含独立服务日志区）
        # =====================================================================
        scard, svb, self.tService = self._make_card("asr.service.title", collapsed=True)
        self._serviceSwitch = SwitchButton()
        self._serviceSwitch.setChecked(False)
        self._serviceSwitch.checkedChanged.connect(self._on_service_mode)
        svb.addWidget(field_row(tr("asr.service.enable"), self._serviceSwitch, label_width=132))

        self._serviceHint = CaptionLabel("")
        self._serviceHint.setWordWrap(True)
        apply_text(self._serviceHint, muted_text(), transparent=True)
        svb.addWidget(self._serviceHint)

        self._urlEdit = QLineEdit(cfg.asrBaseUrl.value)
        self._urlEdit.textChanged.connect(lambda t: setattr(cfg.asrBaseUrl, "value", t))
        self._urlRow = field_row(tr("asr.service.base_url"), self._urlEdit, label_width=132)
        svb.addWidget(self._urlRow)

        self._modelEdit = QLineEdit(cfg.asrModel.value)
        self._modelEdit.textChanged.connect(lambda t: setattr(cfg.asrModel, "value", t))
        self._modelRow = field_row(tr("asr.service.model"), self._modelEdit, label_width=132)
        svb.addWidget(self._modelRow)

        self._keyEdit = PasswordLineEdit()
        self._keyEdit.setText(cfg.asrApiKey.value)
        self._keyEdit.textChanged.connect(lambda t: setattr(cfg.asrApiKey, "value", t))
        self._keyRow = field_row(tr("asr.service.api_key"), self._keyEdit, label_width=132)
        svb.addWidget(self._keyRow)

        log_title = CaptionLabel(tr("asr.service.log.title"))
        apply_text(log_title, text_strong(), weight=700, transparent=True)
        svb.addWidget(log_title)
        self.serviceEdit = QPlainTextEdit()
        self.serviceEdit.setReadOnly(True)
        self.serviceEdit.setMinimumHeight(96)
        self.serviceEdit.setWordWrapMode(_WRAP_MODE)
        apply_text(self.serviceEdit, text_secondary(), transparent=False)
        svb.addWidget(self.serviceEdit)
        self.vbox.addWidget(scard)

        self._on_service_mode(False)
        self._sync_controls()
        self.retheme()
        self.vbox.addStretch(1)

    # =========================================================================
    # 文件选取
    # =========================================================================
    def _on_files(self, paths: list[str]):
        """拖入/选择的文件：过滤出第一个视频或音频文件。"""
        for p in paths:
            if Path(p).suffix.lower() in _ASR_EXTS:
                self._set_input(p)
                return

    def _pick_files(self):
        files = self._ask_open_files(tr("asr.add.file"), _ASR_EXTS)
        if files:
            self._set_input(files[0])

    def _set_input(self, path: str):
        self._input_path = path
        self._fileLabel.setText(tr("asr.file.selected", name=Path(path).name))
        self._sync_controls()

    # =========================================================================
    # 服务模式与健康检查
    # =========================================================================
    def _effective_params(self) -> tuple[str, str, str]:
        """按服务模式开关解析实际使用的 (base_url, model, api_key)。

        未启用服务模式 → 内置默认（零配置连本地服务）；启用 → 配置三件套。
        """
        if self._serviceSwitch.isChecked():
            return cfg.asrBaseUrl.value, cfg.asrModel.value, cfg.asrApiKey.value
        return DEFAULT_BASE_URL, DEFAULT_MODEL, ""

    def _on_service_mode(self, checked: bool):
        """服务模式开关：切换输入区可用状态 + 刷新提示/日志。"""
        for row in (self._urlRow, self._modelRow, self._keyRow):
            row.setEnabled(checked)
        if checked:
            self._serviceHint.setText(tr("asr.service.enable.hint"))
            self._append_service(tr("asr.service.log.enabled"))
        else:
            self._serviceHint.setText(
                tr("asr.service.default", url=DEFAULT_BASE_URL, model=DEFAULT_MODEL)
            )
            self._append_service(tr("asr.service.log.default", url=DEFAULT_BASE_URL))
        self._check_service()

    def _check_service(self):
        """后台探测服务健康（不阻塞界面），结果经信号回传。"""
        base_url, _model, _key = self._effective_params()
        self._statusLabel.setText(tr("asr.status.checking"))
        apply_text(self._statusLabel, muted_text(), transparent=True)

        def _run():
            ok, msg = asr_health(base_url)
            self._healthResult.emit(ok, msg)

        self._health_thread = threading.Thread(target=_run, daemon=True)
        self._health_thread.start()

    def _on_health_result(self, ok: bool, msg: str):
        base_url, model, _key = self._effective_params()
        if ok:
            self._statusLabel.setText(tr("asr.status.ok", model=model))
            apply_text(self._statusLabel, tokens.ACCENT, transparent=True)
        else:
            self._statusLabel.setText(tr("asr.status.fail", msg=msg))
            apply_text(self._statusLabel, tokens.DANGER_STRONG, transparent=True)

    # =========================================================================
    # 启动 / 停止
    # =========================================================================
    def _on_start(self):
        if self._worker is not None:
            return
        if not self._input_path:
            self._append_cmd(tr("asr.error.no_file"))
            return
        if not find_ffmpeg(cfg.ffmpegSource.value):
            self._append_cmd(tr("asr.error.no_ffmpeg"))
            return

        base_url, model, api_key = self._effective_params()
        self.cmdEdit.clear()
        self._append_cmd(tr("asr.cmd.selected", name=Path(self._input_path).name))
        self._append_cmd(
            tr("asr.cmd.service", url=base_url, model=model, key="***" if api_key else "-")
        )

        self._worker = AsrTranscribeWorker(
            self._input_path,
            find_ffmpeg(cfg.ffmpegSource.value) or "",
            find_ffprobe(),
            base_url,
            model,
            api_key,
        )
        self._worker.logMessage.connect(self._append_cmd)
        self._worker.serviceLog.connect(self._append_service)
        self._worker.progressChanged.connect(self._on_progress)
        self._worker.segmentReady.connect(self._on_segment_ready)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()
        self._sync_controls()

    def _on_stop(self):
        if self._worker is not None:
            self._worker.request_stop()
            self.stopBtn.setEnabled(False)
            self._append_cmd(tr("asr.cmd.stopping"))

    def _on_progress(self, pct: int):
        self._progressLabel.setText(tr("asr.progress.value", pct=pct))

    def _on_segment_ready(self, index: int, total: int, marker: str, text: str):
        self._append_cmd(marker)
        self._append_cmd(text if text else tr("asr.cmd.segment_empty"))
        self._append_cmd("")

    def _on_succeeded(self, full_text: str):
        self._append_cmd(tr("asr.cmd.done"))
        self._progressLabel.setText(tr("asr.progress.done"))
        self.saveBtn.setEnabled(bool(full_text))

    def _on_failed(self, msg: str):
        self._append_cmd(tr("asr.cmd.failed", msg=msg))
        self._progressLabel.setText(tr("asr.progress.failed"))

    def _on_worker_finished(self):
        """worker 线程退出后释放引用并复位按钮。"""
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        self._sync_controls()

    def _sync_controls(self):
        running = self._worker is not None
        self.startBtn.setEnabled(not running and bool(self._input_path))
        self.stopBtn.setEnabled(running)

    # =========================================================================
    # CMD / 服务日志追加
    # =========================================================================
    def _append_cmd(self, line: str):
        self.cmdEdit.appendPlainText(line)
        self._scroll_bottom(self.cmdEdit)

    def _append_service(self, line: str):
        self.serviceEdit.appendPlainText(line)
        self._scroll_bottom(self.serviceEdit)

    @staticmethod
    def _scroll_bottom(edit: QPlainTextEdit):
        cursor = edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        edit.setTextCursor(cursor)

    # =========================================================================
    # 保存 .txt
    # =========================================================================
    def _on_save(self):
        text = self.cmdEdit.toPlainText().strip()
        if not text:
            return
        default_name = f"{Path(self._input_path).stem}_transcript.txt" if self._input_path else "transcript.txt"
        path, _ = QFileDialog.getSaveFileName(
            self._dialog_parent(), tr("asr.save"), default_name, "Text (*.txt)"
        )
        if not path:
            return
        try:
            Path(path).write_text(text + "\n", encoding="utf-8")
            self._append_cmd(tr("asr.save.done", path=path))
        except OSError as exc:
            self._append_cmd(tr("asr.save.failed", msg=exc))

    # =========================================================================
    # 主题 / i18n
    # =========================================================================
    def retheme(self):
        super().retheme()
        self.dropArea.retheme()
        self._check_service()

    def retranslateUi(self):
        """语言切换时刷新全部文案（main_window 调用）。"""
        self.titleLabel.setText(tr("nav.asr"))
        self.subLabel.setText(tr("asr.subtitle"))
        self.tInput.setText(tr("asr.input.title"))
        self.tControl.setText(tr("asr.control.title"))
        self.tCmd.setText(tr("asr.cmd.title"))
        self.tService.setText(tr("asr.service.title"))
        self.dropArea.retranslate(
            tr("asr.drop.title"), tr("asr.drop.hint"), tr("asr.drop.formats")
        )
        self.addFileBtn.setText(tr("asr.add.file"))
        self.startBtn.setText(tr("asr.start"))
        self.stopBtn.setText(tr("asr.stop"))
        self.saveBtn.setText(tr("asr.save"))
        self.cmdEdit.setPlaceholderText(tr("asr.cmd.ready"))
        self._urlRow.fieldLabel.setText(tr("asr.service.base_url"))
        self._modelRow.fieldLabel.setText(tr("asr.service.model"))
        self._keyRow.fieldLabel.setText(tr("asr.service.api_key"))
        self._serviceSwitch.setText(tr("asr.service.enable"))
        self._hintLabel.setText(tr("asr.status.hint"))
        if self._serviceSwitch.isChecked():
            self._serviceHint.setText(tr("asr.service.enable.hint"))
        else:
            self._serviceHint.setText(
                tr("asr.service.default", url=DEFAULT_BASE_URL, model=DEFAULT_MODEL)
            )
        self._sync_controls()

