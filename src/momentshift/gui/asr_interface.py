"""音频转文字（ASR）界面 —— 视频/音频 → 文字（内置 FunASR 本地推理 / HTTP 服务）。

职责边界：
- 做：收集输入文件（视频/音频拖拽或选择）、启动/停止后台转写 worker、把
  worker 信号渲染到两块只读文本区（主 CMD 结果区 + 服务模式日志区）、把完整
  文案保存为 .txt、管理「本地模型」下载与「本地服务模式」配置区。
- 不做：不执行 ffmpeg / HTTP 请求 / ONNX 推理（在 ``core/asr_worker`` /
  ``core/asr_client`` / ``core/funasr_engine``）；不持有队列。

架构（v0.8.4）：FunASR **功能内置**（本地推理），**模型不内置**（下载到
``tools/funasr/``，绝不进 repo / 不打包）。默认（服务模式开关关）优先用本地
模型；未下载模型时引导下载或启用服务模式。v0.8.3 的「服务模式」（连用户
本地/远程 OpenAI 兼容服务 ``C:\\FunASR\\server.py``）保留为可选后端。

服务模式语义：
- 「启用服务模式」开关默认**关**。关闭时用本地推理（模型已下载）或提示下载；
  开启后用配置区里的三件套（持久化到 ``cfg.asrBaseUrl/asrModel/asrApiKey``）。
- 服务模式配置区是独立折叠卡片，默认折叠；卡片内含自己的日志区（请求/响应/
  错误），与主 CMD 结果区分开。
"""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtGui import QTextCursor, QTextOption
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    PasswordLineEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SwitchButton,
    TransparentToolButton,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from ..core import funasr_download as fdl
from ..core import funasr_engine as fe
from ..core.asr_client import DEFAULT_BASE_URL, DEFAULT_MODEL, asr_health
from ..core.asr_worker import AsrTranscribeWorker
from ..core.config import cfg
from ..core.ffmpeg import find_ffmpeg, find_ffprobe
from ..core.presets import AUDIO_EXTS, VIDEO_EXTS
from ..core.qt_compat import QThreadPool, Signal
from ..i18n.translator import tr
from . import tokens
from .base import InterfaceBase
from .drop_area import DropArea
from .engine_card import open_folder
from .queue_widget import StatusPill
from .theme import (
    apply_text,
    border_color,
    danger_color,
    field_row,
    ghost_btn,
    muted_text,
    primary_btn,
    success_color,
    text_secondary,
    text_strong,
)

# 本组件接受的输入扩展名：视频 + 音频
_ASR_EXTS = VIDEO_EXTS | AUDIO_EXTS
_WRAP_MODE = QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere


class _FunasrModelRow(QWidget):
    """单个 FunASR 模型的下载/状态行（仿 engine_card.EngineRow，放 ASR 组件内）。

    一行 = 状态点 + 名称 + 状态胶囊（已就绪 / 未下载 / 下载中 x%）+ 简介
    （含体积）+ 下载按钮（未下载时主色高亮）+ 打开文件夹按钮（已下载时）。
    """

    def __init__(self, spec: dict, parent=None):
        super().__init__(parent)
        self.spec = spec
        self._downloading = False
        from .theme import apply_transparent

        apply_transparent(self)

        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(5)

        # 第一行：状态点 + 名称 + 状态胶囊
        top = QHBoxLayout()
        top.setSpacing(8)
        self.dot = QLabel()
        self.dot.setFixedSize(8, 8)
        top.addWidget(self.dot)
        self.nameLbl = StrongBodyLabel(tr(spec["name_key"]))
        self.nameLbl.setWordWrap(True)
        self.nameLbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        top.addWidget(self.nameLbl, 1)
        self.statusPill = StatusPill("pending")
        top.addWidget(self.statusPill)
        vb.addLayout(top)

        # 第二行：简介 + 体积
        self.descLbl = CaptionLabel(
            f"{tr(spec['desc_key'])}  {tr('asr.model.size', size=spec['size_mb'])}"
        )
        self.descLbl.setWordWrap(True)
        self.descLbl.setMinimumWidth(0)
        self.descLbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        apply_text(self.descLbl, muted_text(), transparent=True)
        vb.addWidget(self.descLbl)

        # 第三行：按钮（下载 / 打开文件夹）
        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.dlBtn = PrimaryPushButton(tr("asr.model.download"), icon=FIF.DOWNLOAD)
        self.dlBtn.setFixedHeight(28)
        self.dlBtn.clicked.connect(self._on_download)
        btns.addWidget(self.dlBtn)
        self.folderBtn = PushButton(tr("asr.model.open_folder"), icon=FIF.FOLDER)
        self.folderBtn.setFixedHeight(28)
        self.folderBtn.clicked.connect(
            lambda: open_folder(str(fe.model_dir(self.spec["id"])))
        )
        btns.addWidget(self.folderBtn)
        btns.addStretch(1)
        vb.addLayout(btns)

        # 进度条（下载中显示）
        self.prog = QProgressBar()
        self.prog.setRange(0, 100)
        self.prog.setFixedHeight(3)
        self.prog.setTextVisible(False)
        self.prog.setStyleSheet(tokens.progress_qss("transparent", tokens.ACCENT, 1))
        self.prog.hide()
        vb.addWidget(self.prog)

        self.refresh()

    # -- 下载 --
    def _on_download(self):
        if self._downloading:
            return
        self._downloading = True
        self.dlBtn.setEnabled(False)
        self.prog.setValue(0)
        self.prog.show()
        worker = fdl.FunasrModelDownloadWorker(self.spec["id"])
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_dl_done)
        QThreadPool.globalInstance().start(worker)

    def _on_progress(self, _model_id: str, pct: int):
        self.prog.setValue(pct)
        self.statusPill.set_status("compressing", text=tr("asr.model.downloading", pct=pct))

    def _on_dl_done(self, model_id: str, ok: bool, msg: str):
        self._downloading = False
        self.prog.hide()
        self.refresh()
        parent = self.parent()
        while parent is not None and not hasattr(parent, "modelChanged"):
            parent = parent.parent()
        if parent is not None:
            parent.modelChanged(model_id, ok, msg)

    # -- 状态刷新 --
    def refresh(self) -> None:
        ready = fe.is_model_ready(self.spec["id"], self.spec["quantize"])
        if ready:
            self.statusPill.set_status("done", text=tr("asr.model.ready"))
            self.dot.setStyleSheet(tokens.dot_qss(success_color().name(), 4))
            self.dlBtn.hide()
            self.folderBtn.show()
        else:
            self.statusPill.set_status("failed", text=tr("asr.model.missing"))
            self.dot.setStyleSheet(tokens.dot_qss(danger_color().name(), 4))
            self.dlBtn.show()
            self.folderBtn.hide()
        if self._downloading:
            # 下载中文案由进度信号实时刷新，这里不打断
            self.statusPill.set_status("compressing", text=tr("asr.model.downloading", pct=self.prog.value()))
            self.dlBtn.hide()

    def retranslateUi(self) -> None:
        self.nameLbl.setText(tr(self.spec["name_key"]))
        self.descLbl.setText(
            f"{tr(self.spec['desc_key'])}  {tr('asr.model.size', size=self.spec['size_mb'])}"
        )
        self.dlBtn.setText(tr("asr.model.download"))
        self.folderBtn.setText(tr("asr.model.open_folder"))
        self.refresh()


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
        # 模型管理卡片（本地推理模型：下载 / 状态 / 打开文件夹）
        # =====================================================================
        mcard, mvb, self.tModel = self._make_card("asr.model.title")
        hint = CaptionLabel(tr("asr.model.hint"))
        hint.setWordWrap(True)
        hint.setMinimumWidth(0)
        hint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        apply_text(hint, muted_text(), transparent=True)
        mvb.addWidget(hint)

        self._model_rows: list[_FunasrModelRow] = []
        for spec in fdl.MODEL_CATALOG:
            row = _FunasrModelRow(spec, self)
            self._model_rows.append(row)
            mvb.addWidget(row)
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setFixedHeight(1)
            sep.setStyleSheet(f"QFrame{{ background: {border_color()}; border: none; }}")
            mvb.addWidget(sep)
        self.vbox.addWidget(mcard)

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
    # 服务模式、本地模型状态与健康检查
    # =========================================================================
    def _effective_params(self) -> tuple[str, str, str]:
        """按服务模式开关解析实际使用的 (base_url, model, api_key)。

        仅服务模式（HTTP）使用；本地模式不走这里。
        """
        if self._serviceSwitch.isChecked():
            return cfg.asrBaseUrl.value, cfg.asrModel.value, cfg.asrApiKey.value
        return DEFAULT_BASE_URL, DEFAULT_MODEL, ""

    def _on_service_mode(self, checked: bool):
        """服务模式开关：切换输入区可用状态 + 刷新提示/日志/状态。"""
        for row in (self._urlRow, self._modelRow, self._keyRow):
            row.setEnabled(checked)
        if checked:
            self._serviceHint.setText(tr("asr.service.enable.hint"))
            self._hintLabel.setText(tr("asr.status.hint"))
            self._append_service(tr("asr.service.log.enabled"))
        else:
            self._serviceHint.setText(
                tr("asr.service.default", url=DEFAULT_BASE_URL, model=DEFAULT_MODEL)
            )
            self._append_service(tr("asr.service.log.default", url=DEFAULT_BASE_URL))
        self._check_service()

    def _check_service(self):
        """按模式刷新状态行：服务模式 → HTTP 健康检查；默认 → 本地模型状态。"""
        if not self._serviceSwitch.isChecked():
            self._refresh_local_status()
            return
        base_url, _model, _key = self._effective_params()
        self._statusLabel.setText(tr("asr.status.checking"))
        apply_text(self._statusLabel, muted_text(), transparent=True)

        def _run():
            ok, msg = asr_health(base_url)
            self._healthResult.emit(ok, msg)

        self._health_thread = threading.Thread(target=_run, daemon=True)
        self._health_thread.start()

    def _refresh_local_status(self):
        """默认（未启用服务模式）时的状态行：显示本地模型就绪情况。"""
        model_id = fe.find_ready_model()
        if model_id is not None:
            self._statusLabel.setText(tr("asr.model.local.ready", model=model_id))
            apply_text(self._statusLabel, tokens.ACCENT, transparent=True)
            self._hintLabel.setText(tr("asr.model.local.hint"))
        else:
            self._statusLabel.setText(tr("asr.model.local.missing"))
            apply_text(self._statusLabel, tokens.DANGER_STRONG, transparent=True)
            self._hintLabel.setText(tr("asr.model.no_model"))

    def modelChanged(self, model_id: str, ok: bool, msg: str):
        """模型下载完成/失败后：刷新全部行与状态行，并在 CMD 追加结果。"""
        for row in self._model_rows:
            row.refresh()
        if ok:
            self._append_cmd(tr("asr.model.download.done"))
            self._check_service()
        else:
            self._append_cmd(tr("asr.model.download.failed", msg=msg))

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

        self.cmdEdit.clear()
        self._append_cmd(tr("asr.cmd.selected", name=Path(self._input_path).name))

        if self._serviceSwitch.isChecked():
            base_url, model, api_key = self._effective_params()
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
                mode="http",
            )
        else:
            model_id = fe.find_ready_model()
            if model_id is None:
                self._append_cmd(tr("asr.model.no_model"))
                return
            self._append_cmd(tr("asr.cmd.local", model=model_id))
            self._worker = AsrTranscribeWorker(
                self._input_path,
                find_ffmpeg(cfg.ffmpegSource.value) or "",
                find_ffprobe(),
                mode="local",
                model_id=model_id,
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
        self.tModel.setText(tr("asr.model.title"))
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
        for row in self._model_rows:
            row.retranslateUi()
        if self._serviceSwitch.isChecked():
            self._serviceHint.setText(tr("asr.service.enable.hint"))
            self._hintLabel.setText(tr("asr.status.hint"))
        else:
            self._serviceHint.setText(
                tr("asr.service.default", url=DEFAULT_BASE_URL, model=DEFAULT_MODEL)
            )
        self._check_service()
        self._sync_controls()

