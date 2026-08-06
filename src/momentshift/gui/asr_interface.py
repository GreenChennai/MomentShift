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

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIntValidator, QTextCursor, QTextOption
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    ComboBox,
    EditableComboBox,
    HyperlinkButton,
    LineEdit,
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
from ..core.asr_server import AsrServer
from ..core.asr_worker import AsrTranscribeWorker
from ..core.config import cfg
from ..core.ffmpeg import find_ffmpeg, find_ffprobe
from ..core.hardware import (
    asr_device_label,
    cached_asr_device,
    detect_ram_gb,
    model_hw_satisfied,
)
from ..core.logger import get_logger
from ..core.presets import AUDIO_EXTS, VIDEO_EXTS
from ..core.qt_compat import QApplication, QThreadPool, Signal
from ..i18n.translator import tr
from . import tokens
from .base import (
    InterfaceBase,
    QueueListBase,
    bind_combo_mapping,
    build_detail_label,
    build_row_header,
    build_row_layout,
    combo_value,
    select_combo_value,
)
from .drop_area import DropArea
from .engine_card import open_folder
from .queue_widget import MarqueeName, ProgressBar, StatusPill
from .theme import (
    ThemedCard,
    apply_text,
    border_color,
    danger_color,
    ext_badge,
    field_row,
    ghost_btn,
    icon_btn,
    muted_text,
    primary_btn,
    success_color,
    text_secondary,
    text_strong,
)

log = get_logger("asr_interface")
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

        # v0.8.5 功能 5：模型硬件要求门控（如 paraformer-large-fp32 需 ≥4GB 内存）
        self._hw_ok, self._hw_reason = model_hw_satisfied(
            spec, cached_asr_device(), detect_ram_gb()
        )

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

        # v0.8.9 Bug1：按钮分两行（避免挤出主 UI）——
        #   第一行：一键下载引擎与模型 + 前往下载
        #   第二行：下载源切换 + 打开文件夹
        btns1 = QHBoxLayout()
        btns1.setSpacing(8)
        self.dlBtn = PrimaryPushButton(tr("asr.model.download_engine"), icon=FIF.DOWNLOAD)
        self.dlBtn.setFixedHeight(28)
        self.dlBtn.clicked.connect(self._on_download)
        btns1.addWidget(self.dlBtn)
        # 「前往下载」：打开模型页面（HF 直链推导或 page_url）
        hf_page = self._hf_page_url()
        self.gotoBtn = HyperlinkButton(hf_page, tr("asr.model.goto"))
        self.gotoBtn.setFixedHeight(28)
        btns1.addWidget(self.gotoBtn)
        btns1.addStretch(1)
        vb.addLayout(btns1)

        btns2 = QHBoxLayout()
        btns2.setSpacing(8)
        # v0.8.8 Bug1：下载源切换（默认 GitHub；GitHub 无模型镜像时自动回退 HF）
        self.sourceCombo = ComboBox()
        self.sourceCombo.setFixedHeight(28)
        self.sourceCombo.setMinimumWidth(116)
        self._source_items = [
            ("github", tr("asr.model.source.github")),
            ("hf", tr("asr.model.source.hf")),
        ]
        for _val, disp in self._source_items:
            self.sourceCombo.addItem(disp)
        self.sourceCombo.setCurrentIndex(0)  # 默认 GitHub
        btns2.addWidget(self.sourceCombo)
        self.folderBtn = PushButton(tr("asr.model.open_folder"), icon=FIF.FOLDER)
        self.folderBtn.setFixedHeight(28)
        self.folderBtn.clicked.connect(self._open_folder)
        btns2.addWidget(self.folderBtn)
        btns2.addStretch(1)
        vb.addLayout(btns2)

        # 进度条（下载中显示）
        self.prog = QProgressBar()
        self.prog.setRange(0, 100)
        self.prog.setFixedHeight(3)
        self.prog.setTextVisible(False)
        self.prog.setStyleSheet(tokens.progress_qss("transparent", tokens.ACCENT, 1))
        self.prog.hide()
        vb.addWidget(self.prog)

        # V0.8.20 动画2：行悬停高亮（与引擎卡 EngineRow 同一视觉语言）
        self.setObjectName("asrModelRow")
        self.refresh()

    def enterEvent(self, event):
        self._set_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._set_hover(False)
        super().leaveEvent(event)

    def _set_hover(self, hover: bool):
        """切换行悬停底色。对象选择器只作用于本行，不级联到子标签。"""
        try:
            if hover:
                self.setStyleSheet(
                    f"#asrModelRow{{ background: {tokens.SURFACE_HOVER}; border-radius: 6px; }}"
                )
            else:
                self.setStyleSheet("background: transparent;")
        except RuntimeError:
            pass  # 静默原因：控件可能已随界面销毁

    # -- 下载 --
    def _hf_page_url(self) -> str:
        """模型页面地址：优先清单 page_url（无 urls 的模型），否则从直链推导。"""
        if self.spec.get("page_url"):
            return self.spec["page_url"]
        for f in self.spec.get("files", []):
            for url in f.get("urls", []):
                marker = "/resolve/main/"
                if marker in url:
                    return url.split(marker)[0]
        return "https://huggingface.co/"

    def _on_download(self):
        if self._downloading:
            return
        self._downloading = True
        self.dlBtn.setEnabled(False)
        self.prog.setValue(0)
        self.prog.show()
        source = (
            self._source_items[self.sourceCombo.currentIndex()][0]
            if hasattr(self, "sourceCombo")
            else "github"
        )
        worker = fdl.FunasrModelDownloadWorker(self.spec["id"], source=source)
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

    def _open_folder(self):
        """打开该模型的**二级文件夹**（不存在则创建）——手动下载的模型就放这里。

        v0.8.8 Bug3：不再退回 tools/funasr/ 根目录；每个模型固定
        ``tools/funasr/<id>/``，打开文件夹 = 打开这个二级文件夹。
        """
        try:
            d = fe.model_dir(self.spec["id"])
            d.mkdir(parents=True, exist_ok=True)
            open_folder(str(d))
        except OSError as exc:
            log.warning("打开模型目录失败：%s", exc)

    # -- 状态刷新 --
    def refresh(self) -> None:
        # v0.8.10 Bug2：engine 检查只针对 asr 主模型——FSMN-VAD（kind=vad）与
        # CAM++（kind=spk）是 CPU 可用的辅助模型，engine 字段为 False 但必须可下载；
        # 只有「asr 主模型且无本地推理实现」的（Whisper/Qwen3 等）才灰显
        engine_ok = bool(self.spec.get("engine")) or self.spec.get("kind") in ("vad", "spk")
        if not engine_ok:
            self.statusPill.set_status("failed", text=tr("asr.model.engine_unsupported"))
            self.dot.setStyleSheet(tokens.dot_qss(danger_color().name(), 4))
            self.dlBtn.setEnabled(False)
            self.dlBtn.show()
            self.folderBtn.show()
            return
        if not self._hw_ok:
            # 硬件不满足：禁用下载按钮并显示原因
            self.statusPill.set_status("failed", text=self._hw_reason_text())
            self.dot.setStyleSheet(tokens.dot_qss(danger_color().name(), 4))
            self.dlBtn.setEnabled(False)
            self.dlBtn.show()
            self.folderBtn.show()
            return
        self.dlBtn.setEnabled(True)
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
            self.folderBtn.show()
        if self._downloading:
            # 下载中文案由进度信号实时刷新，这里不打断
            self.statusPill.set_status("compressing", text=tr("asr.model.downloading", pct=self.prog.value()))
            self.dlBtn.hide()

    def _hw_reason_text(self) -> str:
        """硬件不满足时的可读原因（i18n）。

        v0.8.18 修订：nvidia_cuda 与「本地引擎不支持」统一展示
        「硬件不支持」（``asr.model.engine_unsupported``，两者共用同一键值）——
        对用户而言都是「这台机器上跑不了这个模型」，无需区分文案；
        内存不足 / 其他原因仍保留更具体的提示。
        """
        if self._hw_reason == "nvidia_cuda":
            return tr("asr.model.engine_unsupported")
        if self._hw_reason == "min_ram_gb":
            need = self.spec.get("hw_req", {}).get("min_ram_gb", 0)
            return tr("asr.model.hw_reason.min_ram_gb", gb=int(need))
        return tr("asr.model.hw_reason.unknown")

    def retranslateUi(self) -> None:
        self.nameLbl.setText(tr(self.spec["name_key"]))
        self.descLbl.setText(
            f"{tr(self.spec['desc_key'])}  {tr('asr.model.size', size=self.spec['size_mb'])}"
        )
        self.dlBtn.setText(tr("asr.model.download_engine"))
        self.gotoBtn.setText(tr("asr.model.goto"))
        self.folderBtn.setText(tr("asr.model.open_folder"))
        # 源下拉重建（保留当前选中值）
        if hasattr(self, "sourceCombo") and self._source_items:
            cur = self._source_items[self.sourceCombo.currentIndex()][0]
            self.sourceCombo.blockSignals(True)
            self.sourceCombo.clear()
            self._source_items = [
                ("github", tr("asr.model.source.github")),
                ("hf", tr("asr.model.source.hf")),
            ]
            for i, (_val, disp) in enumerate(self._source_items):
                self.sourceCombo.addItem(disp)
                if _val == cur:
                    self.sourceCombo.setCurrentIndex(i)
            self.sourceCombo.blockSignals(False)
        self.refresh()


class AsrItemWidget(ThemedCard):
    """转写队列中的单个任务卡片。

    v0.8.9 Bug4：**逐字对照「放大队列」UpscaleItemWidget 骨架**重做（先删再复制）
    —— 后缀徽标 + 滚动文件名 + 耗时胶囊 + 状态胶囊 + 进度条 + 详情行
    + 复制路径 + 删除按钮；仅去掉放大特有的「对比」按钮、i18n 换成 asr.*。
    """

    removeRequested = Signal(str)

    def __init__(self, item_id: str, src: str, parent=None):
        super().__init__(parent)
        self._id = item_id
        self._src = src
        self._status = "pending"
        # 耗时计时（对齐放大队列：running 起表，done/failed 定格）
        self._start_time = None
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._on_elapsed_tick)

        vb = build_row_layout(self)

        src_ext = Path(src).suffix.upper().lstrip(".")
        self.iconLbl = ext_badge(src_ext, self)
        self.nameLbl = MarqueeName(self)
        self.nameLbl.set_text(Path(src).name)
        self.nameLbl.setObjectName("queueName")
        self.timeLbl = QLabel(tr("asr.elapsed.pending"))
        self.timeLbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timeLbl.setStyleSheet(
            tokens.pill_qss(tokens.SURFACE, tokens.SUCCESS, size=tokens.FONT_CAPTION)
        )
        self.pill = StatusPill("pending")
        vb.addLayout(build_row_header(self.iconLbl, self.nameLbl, self.timeLbl, self.pill))

        self.prog = ProgressBar()
        vb.addWidget(self.prog)

        bottom = QHBoxLayout()
        self.detailLbl = build_detail_label()
        bottom.addWidget(self.detailLbl, 1)
        self.copyBtn = icon_btn(FIF.COPY)
        self.copyBtn.clicked.connect(self._copy_path)
        bottom.addWidget(self.copyBtn)
        self.delBtn = icon_btn(FIF.DELETE)
        self.delBtn.clicked.connect(lambda: self.removeRequested.emit(self._id))
        bottom.addWidget(self.delBtn)
        vb.addLayout(bottom)

        self.set_status("pending")
        self.set_progress(0)

    def _copy_path(self):
        """复制源文件所在目录到剪贴板（对齐放大队列）。"""
        folder = str(Path(self._src).parent)
        QApplication.clipboard().setText(folder)

    def set_progress(self, pct: int):
        self.prog.set_value(pct)

    def set_status(self, status: str, detail: str = ""):
        """status 键与放大队列一致：pending / running / done / failed。"""
        self._status = status
        self.pill.set_status(status)
        self.prog.set_error(status == "failed")
        if status == "running":
            if self._start_time is None:
                import time as _time

                self._start_time = _time.monotonic()
            self._elapsed_timer.start()
            self.detailLbl.setText(tr("asr.status.transcribing"))
        else:
            self._elapsed_timer.stop()
            if status in ("done", "failed"):
                self._update_elapsed_text()  # 定格最终耗时
        if status == "done":
            self.set_progress(100)
            self.detailLbl.setText(detail or tr("asr.queue.status.done"))
        elif status == "failed":
            self.detailLbl.setText((detail or tr("asr.queue.status.failed"))[:80])
        elif status not in ("running",):
            self.detailLbl.setText("")

    def _on_elapsed_tick(self):
        self._update_elapsed_text()

    def _update_elapsed_text(self):
        import time as _time

        secs = int(_time.monotonic() - (self._start_time or 0))
        m, s = divmod(max(0, secs), 60)
        self.timeLbl.setText(f"{tr('asr.elapsed.prefix')} {m}:{s:02d}")


class AsrListWidget(QueueListBase):
    """转写任务列表（v0.8.9 逐字对照「放大队列」UpscaleListWidget 骨架）。"""

    removeRequested = Signal(str)

    _empty_key = "asr.queue.empty"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.statTotal, self.statDone, self.statErr = self._statLabels

    def _update_stats(self):
        """统计总数 / 完成 / 失败（基于行的 ``_status``）。"""
        total = len(self.items)
        done = sum(1 for w in self.items.values() if w._status == "done")
        failed = sum(1 for w in self.items.values() if w._status == "failed")
        self.statTotal.setText(tr("asr.queue.stats.total", n=total))
        self.statDone.setText(tr("asr.queue.stats.done", n=done))
        self.statErr.setText(tr("asr.queue.stats.error", n=failed))

    def add_item(self, item_id: str, src: str):
        if item_id in self.items:
            return
        w = AsrItemWidget(item_id, src)
        w.removeRequested.connect(self.removeRequested)
        self._attach_row(item_id, w)
        self._update_stats()

    def set_status(self, item_id: str, status: str, detail: str = ""):
        w = self.items.get(item_id)
        if w:
            w.set_status(status, detail)
            self._update_stats()

    def set_progress(self, item_id: str, pct: int):
        w = self.items.get(item_id)
        if w:
            w.set_progress(pct)

    def retranslate(self):
        """语言切换：统计栏 + 空态文案 + 行内耗时/状态文案。"""
        self.emptyHint.setText(tr(self._empty_key))
        self._update_stats()
        for w in self.items.values():
            w.pill.set_status(w._status)
            w.timeLbl.setText(tr("asr.elapsed.pending") if w._status == "pending" else w.timeLbl.text())


class AudioTranscribeInterface(InterfaceBase):
    """音频转文字标签页。"""

    # 服务端日志（后台线程发出，Qt 队列连接自动切回 GUI 线程 → 并入转写结果框）
    _serverLog = Signal(str)

    def __init__(self, parent=None):
        super().__init__("Asr", tr("nav.asr"), tr("asr.subtitle"), parent)
        self._queue: list[dict] = []  # 转写队列：[{path, status}]，status=waiting/processing/done/failed
        self._queue_pos = -1  # 当前处理索引；-1 = 未开始
        self._worker: AsrTranscribeWorker | None = None
        # v0.8.9：内置本地服务端（本软件作为服务器，供其他应用调用）
        self._server = AsrServer(log_cb=lambda line: self._serverLog.emit(line))
        self._serverLog.connect(self._append_cmd)
        # v0.8.8 Bug3：启动即预建各模型的二级文件夹（tools/funasr/<id>/）
        fe.ensure_model_dirs()

        # =====================================================================
        # 1. 添加文件卡片（拖拽区 + 选择文件按钮 + 文件计数）
        # =====================================================================
        card, vb, self.tInput = self._make_card("asr.input.title")
        self.dropArea = DropArea(self)
        self.dropArea.filesDropped.connect(self._on_files)
        self.dropArea.clicked.connect(self._pick_files)
        vb.addWidget(self.dropArea)

        tools = QHBoxLayout()
        self.addFileBtn = primary_btn(tr("asr.add.folder"), icon=FIF.FOLDER_ADD)
        self.addFileBtn.clicked.connect(self._pick_folder)
        tools.addWidget(self.addFileBtn)
        vb.addLayout(tools)
        self.vbox.addWidget(card)

        # =====================================================================
        # 2. 模型管理卡片（v0.8.6：默认折叠，排在 ASR 设置前）
        # =====================================================================
        mcard, mvb, self.tModel = self._make_card("asr.model.title", collapsed=True)
        hint = CaptionLabel(tr("asr.model.hint"))
        hint.setWordWrap(True)
        hint.setMinimumWidth(0)
        hint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        apply_text(hint, muted_text(), transparent=True)
        hint_row = QHBoxLayout()
        hint_row.setSpacing(8)
        hint_row.addWidget(hint, 1)
        # v0.8.7 Bug7：刷新按钮——重新检测模型（含自动归位 tools/funasr/ 裸文件），
        # 无需重启软件
        self.modelRefreshBtn = TransparentToolButton(FIF.SYNC, self)
        self.modelRefreshBtn.setFixedSize(30, 30)
        self.modelRefreshBtn.clicked.connect(self._refresh_models)
        hint_row.addWidget(self.modelRefreshBtn)
        mvb.addLayout(hint_row)

        self._model_rows: list[_FunasrModelRow] = []
        for spec in fdl.MODEL_CATALOG:
            self._model_rows.append(_FunasrModelRow(spec, self))

        # v0.8.13 #7：按 category 分组（主要模型 / 可选功能模型），组内排序：
        # 本地就绪或可下载（硬件满足）排前，硬件不支持排后
        groups: dict[str, list[_FunasrModelRow]] = {"main": [], "optional": []}
        for row in self._model_rows:
            cat = row.spec.get("category", "main")
            if cat not in groups:
                cat = "main"
            groups[cat].append(row)

        def _sort_key(r: _FunasrModelRow) -> int:
            return 0 if r._hw_ok else 1

        for cat in ("main", "optional"):
            groups[cat].sort(key=_sort_key)
            # v0.8.14 #1：分组标题放大到标题字号 + 主题色 #238636 + 居中
            header = CaptionLabel(tr(f"asr.model.group.{cat}"))
            apply_text(header, tokens.ACCENT, size=tokens.FONT_TITLE, weight=600, transparent=True)
            header.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            header.setContentsMargins(0, 8, 0, 4)
            mvb.addWidget(header)
            for row in groups[cat]:
                mvb.addWidget(row)
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.HLine)
                sep.setFixedHeight(1)
                sep.setStyleSheet(f"QFrame{{ background: {border_color()}; border: none; }}")
                mvb.addWidget(sep)
        self.vbox.addWidget(mcard)

        # =====================================================================
        # 3. ASR 设置卡片（模型选择 / 分段长度 / 结构化 / 输出位置 / 设备）
        # =====================================================================
        self._build_settings_card()

        # =====================================================================
        # 4. 转写队列卡片（v0.8.6：多文件队列 + 开始/停止/移除/清空 + 进度）
        # =====================================================================
        qcard, qvb, self.tQueue = self._make_card("asr.queue.title")
        # v0.8.8 Bug2：整体照搬「放大队列」结构（QueueListBase + 行卡片），
        # 不再使用通用 QueueListWidget 的适配层
        self._queueList = AsrListWidget()
        self._queueList.setMinimumHeight(120)
        self._queueList.removeRequested.connect(self._remove_by_id)
        qvb.addWidget(self._queueList)

        qctrl = QHBoxLayout()
        self.startBtn = primary_btn(tr("asr.queue.start"), icon=FIF.PLAY)
        self.startBtn.clicked.connect(self._on_start)
        self.stopBtn = ghost_btn(tr("asr.queue.stop"), icon=FIF.CANCEL)
        self.stopBtn.clicked.connect(self._on_stop)
        self.stopBtn.setEnabled(False)
        self.removeBtn = ghost_btn(tr("asr.queue.remove"), icon=FIF.DELETE)
        self.removeBtn.clicked.connect(self._remove_finished)
        self.clearBtn = ghost_btn(tr("asr.queue.clear"), icon=FIF.BROOM)
        self.clearBtn.clicked.connect(self._clear_queue)
        qctrl.addWidget(self.startBtn, 1)
        qctrl.addWidget(self.stopBtn)
        qctrl.addWidget(self.removeBtn)
        qctrl.addWidget(self.clearBtn)
        qvb.addLayout(qctrl)
        self.vbox.addWidget(qcard)

        # =====================================================================
        # 5. 转写结果卡片（CMD 完整文案，v0.8.6：自动保存 txt，无手动保存按钮）
        # =====================================================================
        rcard, rvb, self.tCmd = self._make_card("asr.cmd.title")
        self.cmdEdit = self._make_log_edit(tr("asr.cmd.ready"))
        self.cmdEdit.setMinimumHeight(170)
        rvb.addWidget(self.cmdEdit)
        self.vbox.addWidget(rcard)

        # =====================================================================
        # 6. 本地服务模式卡片（v0.8.9：本软件作为服务器，供其他应用调用
        #    http://127.0.0.1:<port>/v1；默认折叠 + 默认关闭）
        # =====================================================================
        scard, svb, self.tService = self._make_card("asr.service.title", collapsed=True)
        self._serviceSwitch = SwitchButton()
        self._serviceSwitch.setChecked(False)
        self._serviceSwitch.checkedChanged.connect(self._on_service_mode)
        svb.addWidget(field_row(tr("asr.service.enable"), self._serviceSwitch, label_width=132))

        # 监听端口（默认 8000，对齐用户 C:\FunASR\server.py 习惯）
        # v0.8.13 #4：用可编辑数字框（QIntValidator 限定 1024~65535），明确提示可改
        self._portEdit = LineEdit()
        self._portEdit.setValidator(QIntValidator(1024, 65535))
        self._portEdit.setFixedWidth(110)
        # v0.8.15 #1：qfluentwidgets LineEdit 默认 Expanding 策略会在 field_row 的
        # stretch=1 下撑满整行，导致「监听端口」输入框看起来与其他条目不对齐。
        # 显式改 Fixed，使其保持固定宽度并靠左，与开关/下拉等条目一致。
        self._portEdit.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        # v0.8.14 #4：监听端口输入框文本左对齐（默认居中不符合输入习惯）
        self._portEdit.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._portEdit.setText(str(int(cfg.asrServerPort.value)))
        self._portEdit.textChanged.connect(self._on_server_port_changed)
        svb.addWidget(field_row(tr("asr.service.port"), self._portEdit, label_width=132))

        # v0.8.10 Bug3-三件套①：服务地址（自动带端口）+ 一键复制
        url_row = QHBoxLayout()
        url_row.setSpacing(8)
        self._baseUrlEdit = QLineEdit()
        self._baseUrlEdit.setReadOnly(True)
        self._baseUrlEdit.setText(f"http://127.0.0.1:{cfg.asrServerPort.value}/v1")
        url_row.addWidget(self._baseUrlEdit, 1)
        self._copyUrlBtn = TransparentToolButton(FIF.COPY, self)
        self._copyUrlBtn.setFixedSize(32, 32)
        self._copyUrlBtn.clicked.connect(self._copy_base_url)
        url_row.addWidget(self._copyUrlBtn)
        svb.addWidget(field_row(tr("asr.service.base_url"), url_row, label_width=132))

        # 服务端推理模型下拉（engine=True 的 asr 模型）
        self._serverModelCombo = ComboBox()
        self._serverModelCombo.setMinimumWidth(200)
        self._refresh_server_model_combo()
        self._serverModelCombo.currentIndexChanged.connect(self._on_server_model_changed)
        svb.addWidget(field_row(tr("asr.service.model"), self._serverModelCombo, label_width=132))

        # v0.8.22 Bug#2：服务模式同样提供「结构化输出 / 标点恢复 / 情感识别」
        # 三项增强开关。语义与「ASR 设置」里的同名开关一致，但配置项独立
        # （cfg.asrServer*），互不影响；可用性判定复用同一套模型/硬件探测，
        # 不满足条件时灰显禁用并给出原因。
        self._serverStructuredSwitch = SwitchButton()
        self._serverStructuredSwitch.setChecked(bool(cfg.asrServerStructured.value))
        self._serverStructuredSwitch.checkedChanged.connect(
            lambda checked: self._on_server_option(cfg.asrServerStructured, checked)
        )
        self._serviceStructuredRow = field_row(
            tr("asr.settings.structured"),
            self._serverStructuredSwitch,
            label_width=132,
            label_wrap=True,
        )
        svb.addWidget(self._serviceStructuredRow)
        self._serverStructuredHint = self._make_option_hint()
        svb.addWidget(self._serverStructuredHint)

        self._serverPuncSwitch = SwitchButton()
        self._serverPuncSwitch.setChecked(bool(cfg.asrServerPunc.value))
        self._serverPuncSwitch.checkedChanged.connect(
            lambda checked: self._on_server_option(cfg.asrServerPunc, checked)
        )
        self._servicePuncRow = field_row(
            tr("asr.settings.punctuation"),
            self._serverPuncSwitch,
            label_width=132,
            label_wrap=True,
        )
        svb.addWidget(self._servicePuncRow)
        self._serverPuncHint = self._make_option_hint()
        svb.addWidget(self._serverPuncHint)

        self._serverEmotionSwitch = SwitchButton()
        self._serverEmotionSwitch.setChecked(bool(cfg.asrServerEmotion.value))
        self._serverEmotionSwitch.checkedChanged.connect(
            lambda checked: self._on_server_option(cfg.asrServerEmotion, checked)
        )
        self._serviceEmotionRow = field_row(
            tr("asr.settings.emotion"),
            self._serverEmotionSwitch,
            label_width=132,
            label_wrap=True,
        )
        svb.addWidget(self._serviceEmotionRow)
        self._serverEmotionHint = self._make_option_hint()
        svb.addWidget(self._serverEmotionHint)

        self._serviceOptionsNote = CaptionLabel(tr("asr.service.options.note"))
        self._serviceOptionsNote.setWordWrap(True)
        apply_text(self._serviceOptionsNote, muted_text(), transparent=True)
        svb.addWidget(self._serviceOptionsNote)
        self._refresh_server_option_hints()

        # v0.8.10 Bug3-三件套③：可选 api_key（Bearer 鉴权，留空不校验）
        self._apiKeyEdit = PasswordLineEdit()
        self._apiKeyEdit.setText(cfg.asrApiKey.value)
        self._apiKeyEdit.textChanged.connect(lambda t: setattr(cfg.asrApiKey, "value", t))
        svb.addWidget(field_row(tr("asr.service.api_key"), self._apiKeyEdit, label_width=132))

        # 状态行：运行状态 + 监听地址
        self._serverStatusLabel = CaptionLabel(tr("asr.service.stopped"))
        apply_text(self._serverStatusLabel, muted_text(), transparent=True)
        svb.addWidget(self._serverStatusLabel)

        self._serviceHint = CaptionLabel(tr("asr.service.hint"))
        self._serviceHint.setWordWrap(True)
        apply_text(self._serviceHint, muted_text(), transparent=True)
        svb.addWidget(self._serviceHint)

        # 服务日志并入「转写结果」输出框（v0.8.9 Bug3），不再有独立日志区
        self.vbox.addWidget(scard)

        self._on_service_mode(False)
        self._refresh_queue_list()
        self._sync_controls()
        self.retheme()
        self.vbox.addStretch(1)

    # =========================================================================
    # 文件选取
    # =========================================================================
    def _on_files(self, paths: list[str]):
        """拖入/选择的文件：过滤出视频/音频，全部入队（v0.8.6 多文件队列）。"""
        added = [p for p in paths if Path(p).suffix.lower() in _ASR_EXTS]
        if added:
            self._enqueue(added)

    def _pick_files(self):
        files = self._ask_open_files(tr("asr.add.file"), _ASR_EXTS)
        if files:
            self._enqueue(files)

    def _pick_folder(self):
        """v0.8.13 #1：添加文件夹——选取目录后递归收集其中的视频/音频文件。"""
        d = self._ask_directory(tr("asr.add.folder"))
        if not d:
            return
        files = self._expand_paths([d], _ASR_EXTS)
        if files:
            self._enqueue(files)

    def _enqueue(self, paths: list[str]):
        """把文件追加进转写队列（去重 + 更新计数）。"""
        known = {item["path"] for item in self._queue}
        for p in paths:
            if p not in known:
                self._queue.append({"path": p, "status": "waiting"})
                known.add(p)
        self._refresh_queue_list()
        self._sync_controls()

    # =========================================================================
    # 本地服务端模式（v0.8.9：本软件作为服务器，供其他应用调用）
    # =========================================================================
    def _refresh_server_model_combo(self) -> None:
        """v0.8.11 Bug4：服务模型下拉只列**已下载就绪**的 asr 模型，无则「无模型可用」禁用占位。

        与 ASR 设置「推理模型」下拉策略一致——列出可用的，不显示未装的也不写死。
        """
        combo = self._serverModelCombo
        ready_items = [
            spec["id"]
            for spec in fe.MODEL_CATALOG
            if spec.get("kind", "asr") == "asr"
            and spec.get("engine")
            and fe.is_model_ready(spec["id"], spec["quantize"])
        ]
        # v0.8.22 Bug#1：cur 必须在 clear() **之前**读取。
        # clear() 会发 currentIndexChanged(-1) → _on_server_model_changed 把
        # cfg.asrServerModel 写成空串，若之后再读就永远拿不到用户的旧选择。
        # 同时整段重填过程屏蔽信号，避免中间态污染配置。
        cur = cfg.asrServerModel.value
        combo.blockSignals(True)
        try:
            combo.clear()
            if not ready_items:
                combo.addItem(tr("asr.settings.model.none"))
                combo.items[0].isEnabled = False
                cfg.asrServerModel.value = ""
                return
            for mid in ready_items:
                combo.addItem(mid)
            combo.setCurrentText(cur if cur in ready_items else ready_items[0])
        finally:
            combo.blockSignals(False)
        cfg.asrServerModel.value = combo.currentText()

    def _on_server_model_changed(self, _index: int):
        cfg.asrServerModel.value = self._serverModelCombo.currentText()

    # -- v0.8.22 Bug#2：服务模式三项增强开关 --------------------------------
    def _make_option_hint(self) -> CaptionLabel:
        """生成一个自动换行的灰色说明标签（三项开关共用样式）。"""
        hint = CaptionLabel("")
        hint.setWordWrap(True)
        hint.setMinimumWidth(0)
        hint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        apply_text(hint, muted_text(), transparent=True)
        return hint

    def _on_server_option(self, item, checked: bool) -> None:
        """服务模式开关切换：落盘 → 刷新提示 → 若服务在跑则热应用新配置。"""
        setattr(item, "value", bool(checked))
        self._refresh_server_option_hints()
        if self._server.running:
            # 服务端把开关存在实例上，重启一次即可让新配置生效（端口/模型不变）
            self._restart_server_quiet()

    def _restart_server_quiet(self) -> None:
        """静默重启内置服务端，用于开关变更后的热应用（失败则关掉开关）。"""
        self._server.stop()
        ok, msg = self._server.start(
            int(cfg.asrServerPort.value),
            cfg.asrServerModel.value or fe.DEFAULT_MODEL_ID,
            api_key=cfg.asrApiKey.value,
            structured=bool(cfg.asrServerStructured.value),
            punc=bool(cfg.asrServerPunc.value),
            emotion=bool(cfg.asrServerEmotion.value),
        )
        if ok:
            self._serverStatusLabel.setText(tr("asr.service.running", url=self._server.url))
            apply_text(self._serverStatusLabel, tokens.ACCENT, transparent=True)
        else:
            self._serviceSwitch.blockSignals(True)
            self._serviceSwitch.setChecked(False)
            self._serviceSwitch.blockSignals(False)
            self._serverStatusLabel.setText(tr("asr.service.failed", msg=msg))
            apply_text(self._serverStatusLabel, tokens.DANGER_STRONG, transparent=True)

    def _refresh_server_option_hints(self) -> None:
        """按模型/硬件可用性刷新三项开关的可用状态与说明文案。

        判定逻辑与「ASR 设置」完全一致（VAD / ct-punc / emotion2vec+CUDA），
        不满足时灰显禁用，避免用户开了却静默不生效。
        """
        if not hasattr(self, "_serverStructuredHint"):
            return
        # ① 结构化输出：需 FSMN-VAD
        vad_ready = fe.find_ready_vad_model() is not None
        self._serverStructuredSwitch.setEnabled(vad_ready)
        self._serverStructuredHint.setText(
            tr("asr.settings.structured.hint")
            if vad_ready
            else tr("asr.settings.structured.hint.no_vad")
        )
        # ② 标点恢复：需 ct-punc
        punc_ready = fe.is_model_ready("ct-punc", None)
        self._serverPuncSwitch.setEnabled(punc_ready)
        if not cfg.asrServerPunc.value:
            self._serverPuncHint.setText(tr("asr.settings.punctuation.hint.off"))
        elif punc_ready:
            self._serverPuncHint.setText(tr("asr.settings.punctuation.hint.on_ready"))
        else:
            self._serverPuncHint.setText(tr("asr.settings.punctuation.hint.on_missing"))
        # ③ 情感识别：需 emotion2vec+large + CUDA + 完整 funasr
        emo_ok, _reason = fe.emotion_available()
        self._serverEmotionSwitch.setEnabled(emo_ok)
        if not cfg.asrServerEmotion.value:
            self._serverEmotionHint.setText(tr("asr.settings.emotion.hint.off"))
        elif emo_ok:
            self._serverEmotionHint.setText(tr("asr.settings.emotion.hint.on_ready"))
        else:
            self._serverEmotionHint.setText(tr("asr.settings.emotion.hint.on_missing"))

    def _on_server_port_changed(self, text: str):
        """端口变化：写配置 + 联动更新服务地址显示。"""
        try:
            value = int(text)
        except (TypeError, ValueError):
            return
        value = max(1024, min(65535, value))
        cfg.asrServerPort.value = value
        self._baseUrlEdit.setText(f"http://127.0.0.1:{value}/v1")

    def _copy_base_url(self):
        """一键复制服务地址（含端口）到剪贴板。"""
        QApplication.clipboard().setText(self._baseUrlEdit.text())
        self._append_cmd(tr("asr.service.url_copied"))

    def _refresh_models(self):
        """v0.8.7 Bug7：重新检测模型——先自动归位 tools/funasr/ 裸文件，再刷新全部行。"""
        try:
            moved = fe.relocate_loose_model_files()
            if moved:
                self._append_cmd(tr("asr.model.relocated", n=moved))
        except OSError as exc:
            log.warning("模型自动归位失败：%s", exc)
        for row in self._model_rows:
            row.refresh()
        self._refresh_model_combo()
        # v0.8.22 Bug#1：服务模式的「模型」下拉同样要跟着新下载的模型刷新，
        # 否则用户装好模型后服务端下拉仍是旧列表（此前只有 retranslateUi 会刷）。
        self._refresh_server_model_combo()
        self._refresh_structured_hint()
        self._refresh_punc_hint()
        self._refresh_emotion_hint()
        self._refresh_server_option_hints()
        self._sync_controls()

    def _on_service_mode(self, checked: bool):
        """本地服务开关：勾选 → 启动内置 HTTP 服务端；取消 → 停止。"""
        if checked:
            model_id = cfg.asrServerModel.value or fe.DEFAULT_MODEL_ID
            if not fe.is_model_ready(model_id, None):
                self._serviceSwitch.blockSignals(True)
                self._serviceSwitch.setChecked(False)
                self._serviceSwitch.blockSignals(False)
                self._append_cmd(tr("asr.service.no_model", model=model_id))
                self._serverStatusLabel.setText(tr("asr.service.stopped"))
                apply_text(self._serverStatusLabel, muted_text(), transparent=True)
                return
            ok, msg = self._server.start(
                int(cfg.asrServerPort.value),
                model_id,
                api_key=cfg.asrApiKey.value,
                # v0.8.22 Bug#2：把服务模式的三项增强开关一并交给服务端
                structured=bool(cfg.asrServerStructured.value),
                punc=bool(cfg.asrServerPunc.value),
                emotion=bool(cfg.asrServerEmotion.value),
            )
            if ok:
                self._serverStatusLabel.setText(tr("asr.service.running", url=self._server.url))
                apply_text(self._serverStatusLabel, tokens.ACCENT, transparent=True)
            else:
                self._serviceSwitch.blockSignals(True)
                self._serviceSwitch.setChecked(False)
                self._serviceSwitch.blockSignals(False)
                self._serverStatusLabel.setText(tr("asr.service.failed", msg=msg))
                apply_text(self._serverStatusLabel, tokens.DANGER_STRONG, transparent=True)
        else:
            self._server.stop()
            self._serverStatusLabel.setText(tr("asr.service.stopped"))
            apply_text(self._serverStatusLabel, muted_text(), transparent=True)

    def modelChanged(self, model_id: str, ok: bool, msg: str):
        """模型下载完成/失败后：刷新全部行与模型下拉，并在 CMD 追加结果。"""
        for row in self._model_rows:
            row.refresh()
        self._refresh_model_combo()
        # v0.8.22 Bug#1：下载完成的回调也要刷新服务模式模型下拉。
        self._refresh_server_model_combo()
        self._refresh_structured_hint()
        self._refresh_punc_hint()
        self._refresh_emotion_hint()
        self._refresh_server_option_hints()
        if ok:
            self._append_cmd(tr("asr.model.download.done"))
        else:
            self._append_cmd(tr("asr.model.download.failed", msg=msg))

    # =========================================================================
    # ASR 设置卡片（v0.8.5 功能 1）
    # =========================================================================
    def _build_settings_card(self) -> None:
        """「ASR 设置」折叠卡：模型 / 分段 / 结构化 / 设备。"""
        scard, svb, self.tSettings = self._make_card("asr.settings.title", collapsed=True)

        # ① 推理模型下拉：列出全部 asr 模型，已下载可选，未下载置灰（标「未下载」）
        self._modelCombo = ComboBox()
        self._modelCombo.setMinimumWidth(220)
        self._refresh_model_combo()
        self._modelCombo.currentIndexChanged.connect(self._on_model_combo_changed)
        self._settingsModelRow = field_row(
            tr("asr.settings.model"), self._modelCombo, label_width=132
        )
        svb.addWidget(self._settingsModelRow)

        # ② 过长音频分段长度（秒，v0.8.6：下拉 60/120/180/300 + 可手动输入，默认 180）
        self._segmentCombo = EditableComboBox()
        self._segmentCombo.setMinimumWidth(180)
        for sec in (60, 120, 180, 300):
            self._segmentCombo.addItem(f"{sec}")
        cur_seg = str(int(cfg.asrSegmentSec.value))
        if not any(
            self._segmentCombo.itemText(i) == cur_seg
            for i in range(self._segmentCombo.count())
        ):
            self._segmentCombo.addItem(cur_seg)
        self._segmentCombo.setCurrentText(cur_seg)

        def _commit_segment(t: str):
            try:
                v = int(float(t))
                v = max(15, min(300, v))
            except (TypeError, ValueError):
                v = 180
            setattr(cfg.asrSegmentSec, "value", v)

        self._segmentCombo.currentTextChanged.connect(_commit_segment)
        self._settingsSegmentRow = field_row(
            tr("asr.settings.segment"), self._segmentCombo, label_width=132
        )
        svb.addWidget(self._settingsSegmentRow)

        # ③ 结构化输出开关（VAD 时间戳 + 说话人标签 + SenseVoice 标点）
        self._structuredSwitch = SwitchButton()
        self._structuredSwitch.setChecked(bool(cfg.asrStructured.value))
        self._structuredSwitch.checkedChanged.connect(
            lambda checked: setattr(cfg.asrStructured, "value", bool(checked))
        )
        self._settingsStructuredRow = field_row(
            tr("asr.settings.structured"),
            self._structuredSwitch,
            label_width=132,
            label_wrap=True,  # v0.8.8 Bug4：label 允许换行完整显示，不再截断
        )
        svb.addWidget(self._settingsStructuredRow)
        self._structuredHint = CaptionLabel("")
        self._structuredHint.setWordWrap(True)
        self._structuredHint.setMinimumWidth(0)
        self._structuredHint.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        apply_text(self._structuredHint, muted_text(), transparent=True)
        svb.addWidget(self._structuredHint)

        # v0.8.11：标点恢复开关（CPU 可用，Paraformer 转写无标点 → 加上）
        self._puncSwitch = SwitchButton()
        self._puncSwitch.setChecked(bool(cfg.asrPunc.value))
        self._puncSwitch.checkedChanged.connect(
            lambda checked: setattr(cfg.asrPunc, "value", bool(checked))
        )
        self._settingsPuncRow = field_row(
            tr("asr.settings.punctuation"),
            self._puncSwitch,
            label_width=132,
            label_wrap=True,
        )
        svb.addWidget(self._settingsPuncRow)
        self._puncHint = CaptionLabel("")
        self._puncHint.setWordWrap(True)
        self._puncHint.setMinimumWidth(0)
        self._puncHint.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        apply_text(self._puncHint, muted_text(), transparent=True)
        svb.addWidget(self._puncHint)

        # v0.8.14 #2：情感识别调用开关（需 emotion2vec+large 模型 + NVIDIA CUDA
        # + 完整 funasr 包；无模型/无硬件时灰显禁用，调用前再检测，绝不直接调用）
        self._emotionSwitch = SwitchButton()
        self._emotionSwitch.setChecked(bool(cfg.asrEmotion.value))
        self._emotionSwitch.checkedChanged.connect(self._on_emotion_toggled)
        self._settingsEmotionRow = field_row(
            tr("asr.settings.emotion"),
            self._emotionSwitch,
            label_width=132,
            label_wrap=True,
        )
        svb.addWidget(self._settingsEmotionRow)
        self._emotionHint = CaptionLabel("")
        self._emotionHint.setWordWrap(True)
        self._emotionHint.setMinimumWidth(0)
        self._emotionHint.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        apply_text(self._emotionHint, muted_text(), transparent=True)
        svb.addWidget(self._emotionHint)

        # ④ 推理设备：策略下拉（auto/cpu/cuda）+ 检测结果显示
        self._deviceCombo = ComboBox()
        self._deviceCombo.setMinimumWidth(180)
        device_mapping = [
            (tr("asr.settings.device.auto"), "auto"),
            (tr("asr.settings.device.cpu"), "cpu"),
            (tr("asr.settings.device.cuda"), "cuda"),
        ]
        bind_combo_mapping(self._deviceCombo, device_mapping)
        for disp, _val in device_mapping:
            self._deviceCombo.addItem(disp)
        select_combo_value(self._deviceCombo, cfg.asrDevice.value)
        self._deviceCombo.currentIndexChanged.connect(self._on_device_combo_changed)
        self._settingsDeviceRow = field_row(
            tr("asr.settings.device"), self._deviceCombo, label_width=132
        )
        svb.addWidget(self._settingsDeviceRow)
        self._deviceLabel = CaptionLabel("")
        apply_text(self._deviceLabel, text_secondary(), transparent=True)
        svb.addWidget(self._deviceLabel)

        # ⑤ 输出位置（v0.8.7 Bug4：开关「保存在源文件旁」+ 固定目录选择）
        self._outputSwitch = SwitchButton()
        self._outputSwitch.checkedChanged.connect(self._on_output_mode)
        self._settingsOutputModeRow = field_row(
            tr("asr.settings.output"), self._outputSwitch, label_width=132
        )
        svb.addWidget(self._settingsOutputModeRow)
        self._outEdit = QLineEdit(cfg.outputFolder.value)
        self._outEdit.setReadOnly(True)
        self._outBrowse = TransparentToolButton(FIF.FOLDER, self)
        self._outBrowse.setFixedSize(36, 36)
        self._outBrowse.clicked.connect(self._pick_output_dir)
        orow = QHBoxLayout()
        orow.addWidget(self._outEdit, 1)
        orow.addWidget(self._outBrowse)
        self._settingsOutputRow = field_row(
            tr("asr.settings.output.folder"), orow, label_width=132
        )
        svb.addWidget(self._settingsOutputRow)
        self._apply_output_mode()

        self.vbox.addWidget(scard)
        self._refresh_device_label()
        self._refresh_structured_hint()
        self._refresh_punc_hint()
        self._refresh_emotion_hint()

    def _on_output_mode(self, checked: bool):
        """输出位置开关：on=保存在源文件旁(same)，off=固定目录(fixed)。"""
        cfg.outputMode.value = "same" if checked else "fixed"
        self._apply_output_mode()

    def _apply_output_mode(self):
        same = cfg.outputMode.value == "same"
        self._outputSwitch.setChecked(same)
        self._outputSwitch.setText(
            tr("asr.settings.output.same") if same else tr("asr.settings.output.fixed")
        )
        self._settingsOutputRow.setVisible(not same)

    def _pick_output_dir(self):
        """选择转写结果 .txt 的固定输出目录。"""
        d = self._ask_directory(tr("asr.settings.output"))
        if d:
            cfg.outputFolder.value = d
            self._outEdit.setText(d)

    def _model_combo_mapping(self) -> list[tuple[str, str, bool]]:
        """模型下拉映射（v0.8.10 Bug5）：只列**已下载就绪**的 asr 模型。

        没有已装模型时返回空列表，由调用方显示「无模型可用」禁用项。
        """
        mapping: list[tuple[str, str, bool]] = []
        for spec in fe.MODEL_CATALOG:
            if spec.get("kind", "asr") != "asr" or not spec.get("engine"):
                continue
            if not fe.is_model_ready(spec["id"], spec["quantize"]):
                continue
            mapping.append((tr(spec["name_key"]), spec["id"], True))
        return mapping

    def _refresh_model_combo(self) -> None:
        """重建模型下拉（v0.8.10 Bug5）：只显示已装模型；一个都没有显示「无模型可用」。"""
        if not hasattr(self, "_modelCombo"):
            return
        current = cfg.asrModelId.value or ""
        mapping = self._model_combo_mapping()
        combo = self._modelCombo
        combo.blockSignals(True)
        combo.clear()
        if not mapping:
            combo.addItem(tr("asr.settings.model.none"))
            combo.items[0].isEnabled = False  # 禁用占位项
            cfg.asrModelId.value = ""
        else:
            bind_combo_mapping(combo, [(disp, mid) for disp, mid, _ready in mapping])
            for disp, mid, _ready in mapping:
                combo.addItem(disp)
            # 当前配置值仍在已装列表里则保住，否则默认第一个
            if current in [mid for _disp, mid, _ready in mapping]:
                select_combo_value(combo, current)
            else:
                combo.setCurrentIndex(0)
                cfg.asrModelId.value = mapping[0][1]
        combo.blockSignals(False)

    def _on_model_combo_changed(self, _index: int):
        setattr(cfg.asrModelId, "value", combo_value(self._modelCombo))

    def _on_device_combo_changed(self, _index: int):
        setattr(cfg.asrDevice, "value", combo_value(self._deviceCombo))
        self._refresh_device_label()

    def _refresh_device_label(self) -> None:
        """显示当前检测到的推理设备（策略 auto 时按硬件探测结果）。"""
        if not hasattr(self, "_deviceLabel"):
            return
        detected = cached_asr_device()
        self._deviceLabel.setText(
            tr("asr.settings.device.detected", device=asr_device_label(detected))
        )

    def _refresh_device_combo(self) -> None:
        """重建设备下拉（语言切换后候选文案也要翻译）并保住当前值。"""
        if not hasattr(self, "_deviceCombo"):
            return
        current = combo_value(self._deviceCombo) or cfg.asrDevice.value
        mapping = [
            (tr("asr.settings.device.auto"), "auto"),
            (tr("asr.settings.device.cpu"), "cpu"),
            (tr("asr.settings.device.cuda"), "cuda"),
        ]
        self._repopulate_combo(self._deviceCombo, mapping)
        select_combo_value(self._deviceCombo, current)

    def _refresh_structured_hint(self) -> None:
        """v0.8.13 #9：无 VAD 模型时结构化输出开关灰显禁用，并提示需先下载 VAD。"""
        if not hasattr(self, "_structuredHint"):
            return
        vad_ready = fe.find_ready_vad_model() is not None
        self._structuredSwitch.setEnabled(vad_ready)
        if not vad_ready:
            self._structuredHint.setText(tr("asr.settings.structured.hint.no_vad"))
        else:
            self._structuredHint.setText(tr("asr.settings.structured.hint"))

    def _refresh_punc_hint(self) -> None:
        """v0.8.13 #9：ct-punc 未就绪时标点恢复开关灰显禁用，并按状态显示文案。"""
        if not hasattr(self, "_puncHint"):
            return
        punc_ready = fe.is_model_ready("ct-punc", None)
        self._puncSwitch.setEnabled(punc_ready)
        if not cfg.asrPunc.value:
            self._puncHint.setText(tr("asr.settings.punctuation.hint.off"))
        elif punc_ready:
            self._puncHint.setText(tr("asr.settings.punctuation.hint.on_ready"))
        else:
            self._puncHint.setText(tr("asr.settings.punctuation.hint.on_missing"))

    def _on_emotion_toggled(self, checked: bool) -> None:
        """v0.8.14 #2：情感识别开关切换 → 落盘 + 刷新提示。"""
        setattr(cfg.asrEmotion, "value", bool(checked))
        self._refresh_emotion_hint()

    def _refresh_emotion_hint(self) -> None:
        """v0.8.14 #2：emotion2vec 未就绪/无硬件时情感识别开关灰显禁用并按状态提示。"""
        if not hasattr(self, "_emotionHint"):
            return
        ok, _reason = fe.emotion_available()
        self._emotionSwitch.setEnabled(ok)
        if not cfg.asrEmotion.value:
            self._emotionHint.setText(tr("asr.settings.emotion.hint.off"))
        elif ok:
            self._emotionHint.setText(tr("asr.settings.emotion.hint.on_ready"))
        else:
            # 开关开启但环境不再满足（罕见：切设备/删模型）→ 提示不可用，
            # 运行时 worker 仍会二次校验 emotion_available()，不会直接调用
            self._emotionHint.setText(tr("asr.settings.emotion.hint.on_missing"))

    def _resolved_model_id(self) -> str | None:
        """按设置解析实际推理模型：cfg.asrModelId 空 → 自动（第一个已就绪）。"""
        return fe.resolve_model_id(cfg.asrModelId.value)

    # =========================================================================
    # 启动 / 停止
    # =========================================================================
    def _next_index(self, include_failed: bool) -> int:
        """返回下一个可处理条目索引；include_failed=True 允许重试失败项。"""
        for i, item in enumerate(self._queue):
            if item["status"] == "waiting" or (include_failed and item["status"] == "failed"):
                return i
        return -1

    def _on_start(self):
        """从队列取下一个文件启动转写（v0.8.6 队列模式，失败项可手动重试）。"""
        if self._worker is not None:
            return
        if not find_ffmpeg(cfg.ffmpegSource.value):
            self._append_cmd(tr("asr.error.no_ffmpeg"))
            return
        idx = self._next_index(include_failed=True)
        if idx < 0:
            self._append_cmd(tr("asr.error.no_file"))
            return
        self._queue_pos = idx
        self._queue[idx]["status"] = "processing"
        self._refresh_queue_list()
        self._launch_worker(self._queue[idx]["path"])

    def _launch_worker(self, path: str):
        """为单个文件创建并启动 worker（v0.8.9：始终本地推理，服务开关只管服务端）。"""
        self.cmdEdit.clear()
        self._append_cmd(tr("asr.cmd.selected", name=Path(path).name))
        model_id = self._resolved_model_id()
        if model_id is None:
            self._append_cmd(tr("asr.model.no_model"))
            self._on_worker_finished()  # 复位并尝试下一个
            return
        provider = fe.resolve_provider(cfg.asrDevice.value)
        self._append_cmd(tr("asr.cmd.local", model=model_id))
        self._worker = AsrTranscribeWorker(
            path,
            find_ffmpeg(cfg.ffmpegSource.value) or "",
            find_ffprobe(),
            mode="local",
            model_id=model_id,
            segment_sec=float(cfg.asrSegmentSec.value),
            structured=bool(cfg.asrStructured.value),
            vad_model_id=fe.find_ready_vad_model() or "fsmn-vad",
            spk_model_id=fe.find_ready_spk_model() or "",
            use_itn=True,
            provider=provider,
        )
        self._worker.logMessage.connect(self._append_cmd)
        self._worker.serviceLog.connect(self._append_cmd)
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
        if 0 <= self._queue_pos < len(self._queue):
            cur = self._queue[self._queue_pos]
            cur["progress"] = int(pct)
            self._queueList.set_progress(self._queue_id(cur["path"]), int(pct))

    def _on_segment_ready(self, index: int, total: int, marker: str, text: str):
        if marker:
            self._append_cmd(marker)
        self._append_cmd(text if text else tr("asr.cmd.segment_empty"))
        self._append_cmd("")

    def _on_succeeded(self, full_text: str):
        # 记录当前文件状态并自动保存 .txt（v0.8.6，不再手动保存）
        cur = self._queue[self._queue_pos] if 0 <= self._queue_pos < len(self._queue) else None
        self._append_cmd(tr("asr.cmd.done"))
        if cur is not None:
            cur["status"] = "done"
            cur["progress"] = 100
            saved = self._auto_save_txt(cur["path"], full_text)
            if saved:
                self._append_cmd(tr("asr.cmd.saved", path=saved))
        self._refresh_queue_list()

    def _auto_save_txt(self, src_path: str, text: str) -> str | None:
        """转写结果自动保存 .txt：按输出位置设置（same=源文件旁 / fixed=固定目录）。"""
        if not text.strip():
            return None
        try:
            src = Path(src_path)
            if cfg.outputMode.value == "same" or not cfg.outputFolder.value.strip():
                out_dir = src.parent
            else:
                out_dir = Path(cfg.outputFolder.value.strip())
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{src.stem}_transcript.txt"
            out_path.write_text(text, encoding="utf-8")
            return str(out_path)
        except OSError as exc:
            self._append_cmd(tr("asr.error.save_failed", msg=str(exc)))
            return None

    def _on_failed(self, msg: str):
        self._append_cmd(tr("asr.cmd.failed", msg=msg))
        if 0 <= self._queue_pos < len(self._queue):
            self._queue[self._queue_pos]["status"] = "failed"
            self._queue[self._queue_pos]["error"] = msg
        self._refresh_queue_list()
    def _on_worker_finished(self):
        """worker 线程退出后释放引用；队列还有 waiting 项则自动续跑（不自动重试失败项）。"""
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        if self._next_index(include_failed=False) >= 0:
            self._on_start()
        else:
            self._queue_pos = -1
            self._sync_controls()

    @staticmethod
    def _queue_id(path: str) -> str:
        """队列行的唯一 id（用完整路径，天然去重）。"""
        return path

    @staticmethod
    def _asr_status(st: str) -> str:
        """ASR 内部状态 → QueueItemWidget 状态键（Task 常量）。"""
        return {"waiting": "pending", "processing": "running"}.get(st, st)

    def _refresh_queue_list(self):
        """把 self._queue 增量渲染到 AsrListWidget（新增/状态/进度/移除消失项）。"""
        live = set()
        for item in self._queue:
            tid = self._queue_id(item["path"])
            live.add(tid)
            if tid not in self._queueList.items:
                self._queueList.add_item(tid, item["path"])
            st = self._asr_status(item["status"])
            self._queueList.set_status(tid, st, item.get("error", ""))
            self._queueList.set_progress(tid, int(item.get("progress", 0) or 0))
        for tid in list(self._queueList.items):
            if tid not in live:
                self._queueList.remove_item(tid)

    def _remove_by_id(self, task_id: str):
        """行内删除按钮：从队列数据源与渲染中移除。"""
        self._queue[:] = [it for it in self._queue if self._queue_id(it["path"]) != task_id]
        self._refresh_queue_list()
        self._sync_controls()

    def _remove_finished(self):
        """移除队列中已完成/失败的项目（处理中不允许）。"""
        if self._worker is not None:
            return
        self._queue[:] = [it for it in self._queue if it["status"] in ("waiting", "processing")]
        self._refresh_queue_list()
        self._sync_controls()

    def _clear_queue(self):
        """清空队列（处理中不允许）。"""
        if self._worker is not None:
            return
        self._queue.clear()
        self._queue_pos = -1
        self._refresh_queue_list()
        self._sync_controls()

    def _sync_controls(self):
        running = self._worker is not None
        self.startBtn.setEnabled(not running and self._next_index(include_failed=True) >= 0)
        self.stopBtn.setEnabled(running)
        self.removeBtn.setEnabled(not running and len(self._queue) > 0)
        self.clearBtn.setEnabled(not running and len(self._queue) > 0)

    # =========================================================================
    # CMD / 服务日志追加
    # =========================================================================
    @staticmethod
    def _make_log_edit(placeholder: str = "", text_color: str | None = None) -> QPlainTextEdit:
        """只读日志框：自动换行 + 圆角卡片样式（v0.8.8 Bug5）。

        用户反馈旧日志框「矩形 + 无圆角」不好看，这里统一为圆角卡片：
        背景 SURFACE + 1px 边框 + 8px 圆角 + 内边距，聚焦时边框变主色。
        """
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setWordWrapMode(_WRAP_MODE)
        if placeholder:
            edit.setPlaceholderText(placeholder)
        edit.setStyleSheet(
            f"QPlainTextEdit {{"
            f"  color: {text_color or text_strong()};"
            f"  background-color: {tokens.SURFACE};"
            f"  border: 1px solid {border_color()};"
            f"  border-radius: 8px;"
            f"  padding: 6px 8px;"
            f"}}"
            f"QPlainTextEdit:focus {{ border: 1px solid {tokens.ACCENT}; }}"
            f" {tokens.scrollbar_qss()}"
        )
        return edit

    def _append_cmd(self, line: str):
        self.cmdEdit.appendPlainText(line)
        self._scroll_bottom(self.cmdEdit)

    @staticmethod
    def _scroll_bottom(edit: QPlainTextEdit):
        cursor = edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        edit.setTextCursor(cursor)

    # =========================================================================
    # 保存 .txt
    # =========================================================================
    # =========================================================================
    # 主题 / i18n
    # =========================================================================
    def retheme(self):
        super().retheme()
        self.dropArea.retheme()

    def retranslateUi(self):
        """语言切换时刷新全部文案（main_window 调用）。"""
        self.titleLabel.setText(tr("nav.asr"))
        self.subLabel.setText(tr("asr.subtitle"))
        self.tInput.setText(tr("asr.input.title"))
        self.tQueue.setText(tr("asr.queue.title"))
        self.tModel.setText(tr("asr.model.title"))
        self.tCmd.setText(tr("asr.cmd.title"))
        self.tService.setText(tr("asr.service.title"))
        if hasattr(self, "tSettings"):
            self.tSettings.setText(tr("asr.settings.title"))
            self._settingsModelRow.fieldLabel.setText(tr("asr.settings.model"))
            self._settingsSegmentRow.fieldLabel.setText(tr("asr.settings.segment"))
            self._settingsStructuredRow.fieldLabel.setText(tr("asr.settings.structured"))
            self._settingsPuncRow.fieldLabel.setText(tr("asr.settings.punctuation"))
            # v0.8.22：情感识别行此前漏了 retranslate，切语言后仍是旧文案
            self._settingsEmotionRow.fieldLabel.setText(tr("asr.settings.emotion"))
            self._settingsDeviceRow.fieldLabel.setText(tr("asr.settings.device"))
            self._settingsOutputModeRow.fieldLabel.setText(tr("asr.settings.output"))
            self._settingsOutputRow.fieldLabel.setText(tr("asr.settings.output.folder"))
            self._refresh_structured_hint()
            self._refresh_punc_hint()
            self._refresh_emotion_hint()
            self._refresh_device_label()
            self._refresh_model_combo()
            self._refresh_device_combo()
            self._apply_output_mode()
        self.dropArea.retranslate(
            tr("asr.drop.title"), tr("asr.drop.hint"), tr("asr.drop.formats")
        )
        self.addFileBtn.setText(tr("asr.add.folder"))
        self.startBtn.setText(tr("asr.queue.start"))
        self.stopBtn.setText(tr("asr.queue.stop"))
        self.removeBtn.setText(tr("asr.queue.remove"))
        self.clearBtn.setText(tr("asr.queue.clear"))
        self.cmdEdit.setPlaceholderText(tr("asr.cmd.ready"))
        self._serviceSwitch.setText(tr("asr.service.enable"))
        self._serviceHint.setText(tr("asr.service.hint"))
        # v0.8.22 Bug#2：服务模式三项增强开关的标签与说明也要跟随语言
        if hasattr(self, "_serviceStructuredRow"):
            self._serviceStructuredRow.fieldLabel.setText(tr("asr.settings.structured"))
            self._servicePuncRow.fieldLabel.setText(tr("asr.settings.punctuation"))
            self._serviceEmotionRow.fieldLabel.setText(tr("asr.settings.emotion"))
            self._serviceOptionsNote.setText(tr("asr.service.options.note"))
            self._refresh_server_option_hints()
        self._refresh_server_model_combo()
        if self._server.running:
            self._serverStatusLabel.setText(tr("asr.service.running", url=self._server.url))
            apply_text(self._serverStatusLabel, tokens.ACCENT, transparent=True)
        else:
            self._serverStatusLabel.setText(tr("asr.service.stopped"))
            apply_text(self._serverStatusLabel, muted_text(), transparent=True)
        self._queueList.retranslate()
        for row in self._model_rows:
            row.retranslateUi()
        self._refresh_queue_list()
        self._sync_controls()

