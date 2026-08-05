"""压缩界面 —— 批量图片压缩。

职责边界：
- 做：界面布局、参数收集、把任务交给 TaskPool、展示队列与结果。
- 不做：不做队列调度、并发上限、暂停/继续与 worker 生命周期管理，
  这些一律交给 :class:`~momentshift.core.task_pool.TaskPool`。

依赖：core/compressor、core/config、core/logger、core/output_path、core/platform、
core/presets、core/qt_compat、core/task_pool、core/tools_download、gui/base、
gui/drop_area、gui/help_bubble、gui/queue_widget、gui/theme、i18n/translator；
被依赖：gui/quick_dialogs。

历史背景（DUP-01）：本文件曾有一套和放大界面逐行同构的 ``_pending`` /
``_active`` / ``_workers`` 手写调度，改一处必须记得改另一处，已删除并下沉。

支持的后端见 :data:`momentshift.core.compressor.BACKENDS`
（auto/oxipng/jpegoptim/gifsicle/pillow）。
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    StrongBodyLabel,
    SwitchButton,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from ..core import compressor
from ..core.config import cfg
from ..core.logger import get_logger
from ..core.output_path import unique_output_path
from ..core.platform import tools_dir
from ..core.presets import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS

# 压缩模块受理的媒体范围（带点的扩展名集合，供文件对话框筛选与后缀校验）。
MEDIA_EXTS = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS
from ..core.qt_compat import QThreadPool, Signal
from ..core.task_pool import PoolItem, ProgressCb, TaskPool, TaskState
from ..core.tools_download import ToolsDownloadWorker
from ..i18n.translator import tr
from . import tokens
from .base import (
    InterfaceBase,
    QueueListBase,
    build_detail_label,
    build_row_header,
    build_row_layout,
    select_combo_value,
)
from .drop_area import DropArea
from .help_bubble import attach_help
from .queue_widget import (
    FormatPill,
    MarqueeName,
    ProgressBar,
    ScrollAutoFollow,
    StatusPill,
    format_size_compare,
)
from .theme import (
    ThemedCard,
    apply_text,
    apply_transparent,
    ext_badge,
    field_row,
    ghost_btn,
    icon_btn,
    muted_text,
    primary_btn,
    sub_text,
)

log = get_logger("compress")


# =============================================================================
# 压缩执行体（跑在 TaskPool 的工作线程里）
# =============================================================================
def run_compress_task(
    item: PoolItem, report: ProgressCb, cancel: threading.Event
) -> tuple[bool, str]:
    """压缩一个媒体文件（图片 / 音频 / 视频）。喂给 :class:`TaskPool` 的业务执行体。

    Args:
        item: 队列条目。``payload`` 是 :meth:`CompressInterface._prepare_item`
            在 GUI 线程冻结好的参数快照。
        report: 进度回调（0~100）。
        cancel: 用户清空/移除该任务时被置位。
    Returns:
        ``(是否成功, 展示给用户的明细文本)``。省下的字节数与实际生效的后端另外
        写进 ``item.result``——Qt 信号只带得回两个值，塞不下业务细节。
    Notes:
        这里刻意**不读**任何界面控件的当前值。旧实现是在 GUI 线程构造 worker 时
        把参数抄进 worker 字段，效果一样；换成快照字典之后这条约束才是显式的：
        队列跑到一半用户拖动滑块，不该影响已经派发出去的任务。
    """
    params = item.payload or {}
    src: str = params["src"]
    out: str = params["out"]
    target_fmt: str = params["target"]
    mode: str = params["mode"]
    quality: int = params["quality"]
    preferred: str = params["program"]
    opts: dict = params["opts"]

    src_ext = Path(src).suffix.lower().lstrip(".")
    # "same" → 用源文件扩展名
    effective = src_ext if target_fmt in ("same", "", None) else target_fmt
    # 解析「实际使用的后端」：若用户所选程序无法处理该格式，回退到能处理的程序
    backend = compressor.best_backend(effective, mode, preferred)
    backend = compressor.fallback_to_pillow(backend, src)
    item.result["backend"] = backend
    item.result["saved"] = 0
    log.info(
        "[compress] start id=%s src=%s ext=%s target=%s effective=%s mode=%s "
        "quality=%s preferred=%s resolved=%s",
        item.iid,
        src,
        src_ext,
        target_fmt,
        effective,
        mode,
        quality,
        preferred,
        backend,
    )
    # V0.8.16：ffmpeg 压视频动辄几分钟，必须把 ffmpeg 的进度透传出来，否则进度条
    # 会从 0 直接跳到 100，用户看着像卡死。图片后端不会调这个回调。
    def _on_progress(pct: int) -> None:
        try:
            report(max(0, min(100, int(pct))))
        except Exception:  # 静默原因：进度上报失败不应中断压缩本身
            log.debug("[compress] 进度回调失败 id=%s", item.iid)

    try:
        if compressor.needs_conversion(src_ext, effective):
            ok, detail, saved = compressor.transcode_and_compress(
                src,
                out,
                effective,
                mode,
                quality,
                opts,
                preferred=backend,
                on_progress=_on_progress,
                cancel_event=cancel,
            )
        else:
            ok, detail, saved = compressor.compress_auto(
                src,
                out,
                mode,
                quality,
                opts,
                preferred=backend,
                on_progress=_on_progress,
                cancel_event=cancel,
            )
    except Exception:
        log.exception("[compress] task %s raised an exception", item.iid)
        # RISK-10：异常路径也要把半成品清干净，否则用户输出目录里会攒 .tmp。
        compressor.cleanup_temp_files(out)
        return False, "exception (see log)"

    item.result["saved"] = int(saved or 0)
    if cancel.is_set():
        # 用户在压缩过程中清空了队列：产物没人要，顺手把中间文件也收拾掉。
        compressor.cleanup_temp_files(out)
    log.info("[compress] finished id=%s ok=%s saved=%d backend=%s", item.iid, ok, saved, backend)
    return bool(ok), str(detail or "")


# =============================================================================
# 队列列表组件
# =============================================================================
# 后端显示名（用于「自动切换」后的状态提示，如 "jpegoptim 完成"）
BACKEND_NAMES = {
    "oxipng": "oxipng",
    "jpegoptim": "jpegoptim",
    "gifsicle": "Gifsicle",
    "pillow": "Pillow",
    "ffmpeg": "FFmpeg",
}


class CompressItemWidget(ThemedCard):
    """压缩队列中的单个任务卡片。

    v0.7.3 Bug4 重构：结构与「转换队列」的 QueueItemWidget 对齐 ——
    类别图标 + 文件名 + 格式胶囊 + 状态胶囊 / 进度条 / 大小对比行 + 操作按钮。
    功能不变，仅统一视觉；同时修复完成时进度条停在旧值不满格的问题。
    """

    removeRequested = Signal(str)

    def __init__(
        self, item_id: str, src: str, selected: str = "auto", target: str = "same", parent=None
    ):
        super().__init__(parent)
        self._id = item_id
        self._src = src
        self._saved = 0
        self._status = "pending"
        self._selected = selected  # 用户选择的压缩程序（用于判断是否"自动切换"）
        self._target = target
        self._src_size = self._read_src_size()

        vb = build_row_layout(self)

        # Adj1：后缀矩形徽标取代固定图片图标
        self.iconLbl = ext_badge(Path(src).suffix.upper().lstrip("."), self)
        self.nameLbl = MarqueeName(self)
        self.nameLbl.set_text(Path(src).name)
        self.nameLbl.setObjectName("queueName")
        self.fmtPill = FormatPill(self._format_text())
        self.pill = StatusPill("pending")
        vb.addLayout(build_row_header(self.iconLbl, self.nameLbl, self.fmtPill, self.pill))

        self.prog = ProgressBar()
        vb.addWidget(self.prog)

        # 大小对比行（黑字 + 百分比绿/红），与操作按钮同行右对齐
        bottom = QHBoxLayout()
        self.detailLbl = build_detail_label()
        bottom.addWidget(self.detailLbl, 1)
        self.delBtn = icon_btn(FIF.DELETE)
        self.delBtn.clicked.connect(lambda: self.removeRequested.emit(self._id))
        bottom.addWidget(self.delBtn)
        vb.addLayout(bottom)

        self.set_status("pending")
        self.set_progress(0)

    # -- 辅助 -----------------------------------------------------------
    def _read_src_size(self) -> int:
        try:
            p = Path(self._src)
            return p.stat().st_size if p.exists() else 0
        except OSError:
            return 0

    def _format_text(self) -> str:
        """格式胶囊文案：``.PNG → .JPG``；目标为「与源相同」时两端一致。"""
        src_ext = Path(self._src).suffix.upper().lstrip(".")
        tgt = src_ext if self._target in ("", "same") else self._target.upper()
        return f".{src_ext} → .{tgt}"

    def set_target(self, target: str):
        self._target = target
        self.fmtPill.setText(self._format_text())

    # -- 状态 -----------------------------------------------------------
    def set_progress(self, pct: int):
        self.prog.set_value(pct)

    def set_status(self, status: str, saved: int = 0, detail: str = "", backend: str = ""):
        self._status = status
        if status == "done":
            self._saved = saved
            # 修复4：tr('compress.done.by') 含 {backend} 占位符，必须 .format() 替换
            if (
                backend
                and self._selected != backend
                and backend in BACKEND_NAMES
            ):
                name = BACKEND_NAMES.get(backend, backend)
                self.pill.set_status("done_sw", text=tr("compress.done.by").format(backend=name))
            else:
                self.pill.set_status("done")
            # Bug4：完成时进度条必须走满，否则停在最后一次回调的旧值
            self.prog.set_error(False)
            self.prog.set_value(100)
            before = self._src_size or self._read_src_size()
            if before:
                # 修复3：无论 saved 是否为零，都显示大小对比
                after = before - saved if saved else before
                self.detailLbl.setText(format_size_compare(before, after))
            else:
                self.detailLbl.setText(tr("compress.done"))
        else:
            self.pill.set_status(status)
            self.prog.set_error(status == "failed")
            if status == "failed":
                self.detailLbl.setText((detail or tr("compress.failed"))[:60])
            elif status == "running":
                self.detailLbl.setText("")
            else:
                self.prog.set_value(0)
                self.detailLbl.setText("")

    def retranslate(self):
        self.pill.set_status(self._status)


class CompressListWidget(QueueListBase):
    """压缩任务列表（带统计栏），继承 QueueListBase 复用统计/空态/增删骨架。

    与转换/放大队列的真正差异在于：统计口径按 ``_status``（done 计数而非
    running）、入队签名多 ``selected``/``target`` 两参、无「转换前/后」双段对比。
    这些差异留给本类，公共的「统计栏 + 行容器 + 空态 + 按 key 增删」收口在基类。
    """

    removeRequested = Signal(str)

    _empty_key = "compress.queue.empty"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.statTotal, self.statDone, self.statErr = self._statLabels

    def _update_stats(self):
        """重写基类占位：统计总数 / 完成 / 失败（基于行的 ``_status``）。"""
        total = len(self.items)
        done = sum(1 for w in self.items.values() if w._status == "done")
        failed = sum(1 for w in self.items.values() if w._status == "failed")
        self.statTotal.setText(tr("compress.queue.stats.total", n=total))
        self.statDone.setText(tr("compress.queue.stats.done", n=done))
        self.statErr.setText(tr("compress.queue.stats.error", n=failed))

    def add_item(self, item_id: str, src: str, selected: str = "auto", target: str = "same"):
        if item_id in self.items:
            return
        w = CompressItemWidget(item_id, src, selected, target)
        w.removeRequested.connect(self.removeRequested)
        self._attach_row(item_id, w)
        self._update_stats()

    def set_target(self, target: str):
        """目标格式变更后同步刷新所有未开始任务的格式胶囊。"""
        for w in self.items.values():
            w.set_target(target)

    def set_status(
        self, item_id: str, status: str, saved: int = 0, detail: str = "", backend: str = ""
    ):
        w = self.items.get(item_id)
        if w:
            w.set_status(status, saved, detail, backend)
            self._update_stats()


# =============================================================================
# 压缩界面
# =============================================================================
class CompressInterface(InterfaceBase):
    """图片压缩标签页。

    队列调度委托给 :class:`~momentshift.core.task_pool.TaskPool`（v0.8.0
    DUP-01），本类只提供两样东西：``run_compress_task`` 需要的参数快照
    （:meth:`_prepare_item`），以及把池发出的信号渲染成列表行的一组槽。

    支持 auto/oxipng/jpegoptim/gifsicle/pillow 多种后端与无损/有损两种模式。
    对外暴露 ``taskAdded`` / ``taskProgress`` / ``taskFinished`` 三条信号供快速
    调用进度窗使用（v0.7.12）。
    """

    # (item_id, 文件名)
    taskAdded = Signal(str, str)
    # (item_id, pct)
    taskProgress = Signal(str, int)
    # (item_id, "done"/"failed")
    taskFinished = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__("Compress", tr("nav.compress"), tr("compress.subtitle"), parent)

        # 队列引擎。max_workers 传的是**方法本身**而不是取值结果，这样用户在
        # 设置页改「最大线程数」时下一轮调度立即生效——与旧代码每次循环重读
        # cfg.maxThreads 的行为一致。
        self._pool = TaskPool(
            run_compress_task,
            max_workers=self._max_threads,
            parent=self,
            prepare_fn=self._prepare_item,
        )
        self._pool.itemAdded.connect(self._on_pool_added)
        self._pool.itemStarted.connect(self._on_pool_started)
        self._pool.itemProgress.connect(self._on_pool_progress)
        self._pool.itemFinished.connect(self._on_pool_finished)
        self._pool.stateChanged.connect(self._update_controls)
        self._pool.allFinished.connect(self._on_pool_all_finished)

        # 重入防护：模态对话框自带事件循环，期间用户仍能再次点按钮，
        # 不挡住就会叠出第二个弹框
        self._picking = False

        # 压缩参数默认值（：oxipng / jpegoptim / pillow 三后端）
        # 调整1：元数据默认保留（strip=none, jo_strip=none）
        self._program = "auto"
        self._tool_opts = {
            "oxipng": {
                "level": 3,
                "interlace": True,
                "strip": "none",
                "filter": 0,
                "zc": 6,
                "alpha": False,
            },
            "jpegoptim": {
                "jo_mode": "lossless",
                "jo_max": 85,
                "jo_strip": "none",
                "jo_progressive": "auto",
                "jo_threshold": 0,
                "jo_preserve": True,
                "jo_retry": False,
            },
            "gifsicle": {"gs_optimize": 3, "gs_loop": 0, "gs_lossy": 0},
            "pillow": {
                "pil_quality": 95,
                "pil_optimize": True,
                "pil_progressive": True,
                "pil_subsampling": "4:4:4",
            },
            # ffmpeg 三段参数（视频 / 音频 / 图片）全部默认键，UI 按类别挑选展示。
            "ffmpeg": compressor.ffmpeg_param_defaults(),
        }
        self._target = "same"
        self._switches: list = []
        self._output_mode = cfg.compressMode.value
        self._suffix = cfg.compressSuffix.value
        self._folder = cfg.compressFolder.value or ""
        # v0.8.1 Bug4-②：后端参数分区里的 field_row 行标签，retranslateUi 按
        # ``(row, i18n_key)`` 逐行刷新（field_row 的标签此前是拿不到引用的局部变量）
        self._param_rows: list[tuple] = []
        # FFmpeg 面板：每类别的参数控件 setter（供预设档一键套用）、分类小标题、
        # 预设下拉框，均需在语言切换时刷新。
        self._ff_setters: dict = {}
        self._ff_cat_headers: dict = {}
        self._ff_profile_combos: dict = {}

        # =====================================================================
        # 输入卡片
        # =====================================================================
        card, vb, self.tInput = self._make_card("compress.input.title")
        self.dropArea = DropArea(self)
        self.dropArea.filesDropped.connect(self._on_files)
        self.dropArea.clicked.connect(self._pick_files)
        vb.addWidget(self.dropArea)
        tools = QHBoxLayout()
        self.addFolderBtn = primary_btn(tr("compress.add.folder"), icon=FIF.FOLDER_ADD)
        self.addFolderBtn.clicked.connect(self._pick_folder)
        tools.addWidget(self.addFolderBtn)
        vb.addLayout(tools)
        self.vbox.addWidget(card)
        self._inputCard = card

        # =====================================================================
        # 压缩设置卡片
        # =====================================================================
        scard, svb, self.tSettings = self._make_card("compress.settings.title", collapsed=True)
        self._settingsCard = scard

        # 压缩后端选择（：auto / oxipng / jpegoptim / pillow；：+ gifsicle）
        self.programCombo = self._make_combo(
            [
                (tr("advanced.compression.auto"), "auto"),
                (tr("advanced.compression.oxipng"), "oxipng"),
                (tr("advanced.compression.jpegoptim"), "jpegoptim"),
                (tr("advanced.compression.gifsicle"), "gifsicle"),
                (tr("advanced.compression.pillow"), "pillow"),
                (tr("advanced.compression.ffmpeg"), "ffmpeg"),
            ],
            self._program,
            lambda v: self._on_program(v),
        )
        self.backendRow = field_row(tr("advanced.compression.backend"), self.programCombo)
        svb.addWidget(self.backendRow)

        # 路由提示（仅 auto 时显示）
        rhint = CaptionLabel(tr("advanced.compression.route"))
        rhint.setWordWrap(True)
        apply_text(rhint, muted_text(), transparent=True)
        svb.addWidget(rhint)
        self._route_hint = rhint

        # 通用压缩参数（目标格式）
        self.paramsGroup = QWidget()
        # 强制透明背景，防止在深/浅色主题下出现异常色块 (, #6)
        apply_transparent(self.paramsGroup)
        fq = QVBoxLayout(self.paramsGroup)
        fq.setContentsMargins(0, 0, 0, 0)
        fq.setSpacing(6)
        self.targetCombo = self._make_combo(
            [
                (tr("compress.target.same"), "same"),
                ("PNG", "png"),
                ("JPG", "jpg"),
                ("WebP", "webp"),
                ("BMP", "bmp"),
                ("TIFF", "tiff"),
            ],
            self._target,
            self._on_target,
        )
        self.targetRow = field_row(tr("compress.target"), self.targetCombo)
        fq.addWidget(self.targetRow)
        svb.addWidget(self.paramsGroup)

        # 各后端专用参数组（：三后端独立面板）
        # Bug3：auto 模式三组同显时，除了容器间距，还要有分区小标题，
        # 否则十几行参数视觉上连成一片，分不清哪几行属于哪个后端。
        self._backend_container = QWidget()
        apply_transparent(self._backend_container)
        _bcont_ly = QVBoxLayout(self._backend_container)
        _bcont_ly.setContentsMargins(0, 0, 0, 0)
        _bcont_ly.setSpacing(20)
        self.oxipngGroup = self._backend_section("oxipng", self._build_oxipng())
        _bcont_ly.addWidget(self.oxipngGroup)
        self.joGroup = self._backend_section("jpegoptim", self._build_jpegoptim())
        _bcont_ly.addWidget(self.joGroup)
        self.gsGroup = self._backend_section("gifsicle", self._build_gifsicle())
        _bcont_ly.addWidget(self.gsGroup)
        self.pilGroup = self._backend_section("pillow", self._build_pillow())
        _bcont_ly.addWidget(self.pilGroup)
        self.ffmpegGroup = self._backend_section("ffmpeg", self._build_ffmpeg())
        _bcont_ly.addWidget(self.ffmpegGroup)
        svb.addWidget(self._backend_container)

        # 输出位置
        self.outputSwitch = SwitchButton()
        self.outputSwitch.checkedChanged.connect(self._on_output_mode)
        self.outputModeRow = field_row(tr("compress.output.mode"), self.outputSwitch)
        svb.addWidget(self.outputModeRow)
        self.suffixEdit = QLineEdit(self._suffix)
        self.suffixEdit.textChanged.connect(
            lambda t: (setattr(self, "_suffix", t), setattr(cfg.compressSuffix, "value", t))
        )
        self.suffixRow = field_row(tr("compress.output.suffix"), self.suffixEdit)
        svb.addWidget(self.suffixRow)
        self.folderEdit = QLineEdit(self._folder)
        self.folderEdit.setReadOnly(True)
        self.browseBtn = icon_btn(FIF.FOLDER)
        self.browseBtn.clicked.connect(self._pick_output)
        frow = QHBoxLayout()
        frow.addWidget(self.folderEdit, 1)
        frow.addWidget(self.browseBtn)
        self.folderRow = field_row(tr("compress.output.folder"), frow)
        svb.addWidget(self.folderRow)
        self._apply_output_mode()

        # 工具下载按钮（仅非 pillow 后端且缺失时显示）
        self.toolsBtn = primary_btn(tr("compress.tools.download"), icon=FIF.DOWNLOAD)
        self.toolsBtn.clicked.connect(self._on_download_tools)
        self.toolsStatus = CaptionLabel()
        apply_text(self.toolsStatus, muted_text())
        svb.addWidget(self.toolsBtn)
        svb.addWidget(self.toolsStatus)
        self.vbox.addWidget(scard)

        # =====================================================================
        # 队列卡片
        # =====================================================================
        qcard, qvb, self.tQueue = self._make_card("compress.queue.title")
        self.listWidget = CompressListWidget(self)
        self.listWidget.removeRequested.connect(self._on_remove)
        self.queueScroll = self._make_scroll(280)
        self.queueScroll.setWidget(self.listWidget)
        qvb.addWidget(self.queueScroll)
        # Adj2：队列自动跟随当前处理任务
        self._queue_auto_follow = ScrollAutoFollow(self.queueScroll)
        ctrl = QHBoxLayout()
        self.startBtn = primary_btn(tr("compress.start"), icon=FIF.PLAY)
        self.startBtn.clicked.connect(self._on_start)
        self.pauseBtn = ghost_btn(tr("compress.pause"), icon=FIF.PAUSE)
        self.pauseBtn.clicked.connect(self._on_pause)
        self.clearBtn = ghost_btn(tr("compress.clear"), icon=FIF.DELETE)
        self.clearBtn.clicked.connect(self._on_clear)
        ctrl.addWidget(self.startBtn, 1)
        ctrl.addWidget(self.pauseBtn)
        ctrl.addWidget(self.clearBtn)
        qvb.addLayout(ctrl)
        self.vbox.addWidget(qcard)

        self._on_program(self._program)
        self._restyle_switches()
        self.vbox.addStretch(1)
        self._collapse_ready = True
        self.retheme()

    # =========================================================================
    # 后端专用参数面板
    # =========================================================================

    def _backend_section(self, key: str, inner: QWidget) -> QWidget:
        """给后端参数组包一层带小标题的分区（标题仅 auto 模式显示）。"""
        w = QWidget()
        apply_transparent(w)
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)
        hdr = StrongBodyLabel(tr(f"advanced.compression.{key}"))
        apply_text(hdr, sub_text(), transparent=True)
        ly.addWidget(hdr)
        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {tokens.BORDER}; border: none;")
        ly.addWidget(rule)
        ly.addWidget(inner)
        w._header = hdr
        w._rule = rule
        return w

    def _param_row(self, key: str, control, label_width: int = 96):
        """建一个后端参数行，并把 ``(row, i18n_key)`` 登记进 ``_param_rows``。

        v0.8.1 Bug4-②：后端分区参数行的标签此前是 field_row 里拿不到引用的
        局部变量，语言切换后不刷新。统一走这个构造器，retranslateUi 就能按
        登记的 key 批量刷新标签。
        """
        fr = field_row(tr(key), control, label_width=label_width)
        self._param_rows.append((fr, key))
        return fr

    def _build_oxipng(self):
        grp = self._tool_opts["oxipng"]
        w = QWidget()
        apply_transparent(w)
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)
        lvl = QSlider(Qt.Orientation.Horizontal)
        lvl.setRange(0, 6)
        lvl.setValue(int(grp["level"]))
        lvl_label = QLabel(str(grp["level"]))
        lvl.valueChanged.connect(lambda v: (grp.__setitem__("level", v), lvl_label.setText(str(v))))
        row = QHBoxLayout()
        row.addWidget(lvl_label)
        row.addWidget(lvl, 1)
        fr = self._param_row("advanced.level", row)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.level")
        inter = SwitchButton()
        inter.setChecked(bool(grp["interlace"]))
        inter.checkedChanged.connect(lambda b: grp.__setitem__("interlace", b))
        self._switches.append(inter)
        fr = self._param_row("advanced.interlace", inter)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.interlace")
        strip = self._make_combo(
            [
                (tr("advanced.strip.none"), "none"),
                (tr("advanced.strip.safe"), "safe"),
                (tr("advanced.strip.all"), "all"),
            ],
            grp["strip"],
            lambda v: grp.__setitem__("strip", v),
        )
        fr = self._param_row("advanced.strip", strip)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.strip")
        filt = self._make_combo(
            [
                (tr("advanced.filter.none"), 0),
                (tr("advanced.filter.sub"), 1),
                (tr("advanced.filter.up"), 2),
                (tr("advanced.filter.average"), 3),
                (tr("advanced.filter.paeth"), 4),
                (tr("advanced.filter.mixed"), 5),
            ],
            grp["filter"],
            lambda v: grp.__setitem__("filter", int(v)),
        )
        fr = self._param_row("advanced.filter", filt)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.filter")
        zc = QSlider(Qt.Orientation.Horizontal)
        zc.setRange(1, 9)
        zc.setValue(int(grp["zc"]))
        zc_label = QLabel(str(grp["zc"]))
        zc.valueChanged.connect(lambda v: (grp.__setitem__("zc", v), zc_label.setText(str(v))))
        zc_row = QHBoxLayout()
        zc_row.addWidget(zc_label)
        zc_row.addWidget(zc, 1)
        fr = self._param_row("advanced.zc", zc_row)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.zc")
        alpha = SwitchButton()
        alpha.setChecked(bool(grp["alpha"]))
        alpha.checkedChanged.connect(lambda b: grp.__setitem__("alpha", b))
        self._switches.append(alpha)
        fr = self._param_row("advanced.alpha", alpha)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.alpha")
        return w

    def _build_jpegoptim(self):
        grp = self._tool_opts["jpegoptim"]
        w = QWidget()
        apply_transparent(w)
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)
        jo_mode = self._make_combo(
            [
                (tr("advanced.jo.mode.lossless"), "lossless"),
                (tr("advanced.jo.mode.lossy"), "lossy"),
            ],
            grp["jo_mode"],
            lambda v: (grp.__setitem__("jo_mode", v), self._sync_jo_max(v)),
        )
        fr = self._param_row("advanced.jo.mode", jo_mode)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.jo.mode")
        jo_max = QSlider(Qt.Orientation.Horizontal)
        jo_max.setRange(0, 100)
        jo_max.setValue(int(grp["jo_max"]))
        jo_max_spin = QSpinBox()
        jo_max_spin.setRange(0, 100)
        jo_max_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        jo_max_spin.setValue(int(grp["jo_max"]))
        jo_max.valueChanged.connect(
            lambda v: (grp.__setitem__("jo_max", v), jo_max_spin.setValue(v))
        )
        jo_max_spin.valueChanged.connect(
            lambda v: (grp.__setitem__("jo_max", v), jo_max.setValue(v))
        )
        jm_row = QHBoxLayout()
        jm_row.addWidget(jo_max, 1)
        jm_row.addWidget(jo_max_spin)
        jo_max_fr = self._param_row("advanced.jo.max", jm_row)
        ly.addWidget(jo_max_fr)
        attach_help(jo_max_fr, "advanced.help.jo.max")
        jo_strip = self._make_combo(
            [
                (tr("advanced.jo.strip.none"), "none"),
                (tr("advanced.jo.strip.meta"), "meta"),
                (tr("advanced.jo.strip.exif"), "exif"),
                (tr("advanced.jo.strip.icc"), "icc"),
                (tr("advanced.jo.strip.all"), "all"),
            ],
            grp["jo_strip"],
            lambda v: grp.__setitem__("jo_strip", v),
        )
        fr = self._param_row("advanced.jo.strip", jo_strip)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.jo.strip")
        jo_prog = self._make_combo(
            [
                (tr("advanced.jo.prog.auto"), "auto"),
                (tr("advanced.jo.prog.keep"), "keep"),
                (tr("advanced.jo.prog.progressive"), "progressive"),
                (tr("advanced.jo.prog.normal"), "normal"),
            ],
            grp["jo_progressive"],
            lambda v: grp.__setitem__("jo_progressive", v),
        )
        fr = self._param_row("advanced.jo.prog", jo_prog)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.jo.prog")
        jo_thr = QSpinBox()
        jo_thr.setRange(0, 99)
        jo_thr.setSuffix("%")
        jo_thr.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        jo_thr.setValue(int(grp["jo_threshold"]))
        jo_thr.valueChanged.connect(lambda v: grp.__setitem__("jo_threshold", v))
        fr = self._param_row("advanced.jo.threshold", jo_thr)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.jo.threshold")
        jo_pres = SwitchButton()
        jo_pres.setChecked(bool(grp["jo_preserve"]))
        jo_pres.checkedChanged.connect(lambda b: grp.__setitem__("jo_preserve", b))
        self._switches.append(jo_pres)
        fr = self._param_row("advanced.jo.preserve", jo_pres)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.jo.preserve")
        jo_retry = SwitchButton()
        jo_retry.setChecked(bool(grp["jo_retry"]))
        jo_retry.checkedChanged.connect(lambda b: grp.__setitem__("jo_retry", b))
        self._switches.append(jo_retry)
        fr = self._param_row("advanced.jo.retry", jo_retry)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.jo.retry")
        self._jo_max_fr = jo_max_fr
        self._sync_jo_max(grp["jo_mode"])
        return w

    def _sync_jo_max(self, mode: str):
        if hasattr(self, "_jo_max_fr"):
            self._jo_max_fr.setEnabled(mode == "lossy")

    def _build_gifsicle(self):
        """v0.7.28：Gifsicle 动图压缩参数（优化级别 / 循环次数 / 有损阈值）。"""
        grp = self._tool_opts["gifsicle"]
        w = QWidget()
        apply_transparent(w)
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)

        # 优化级别 1-3
        lvl = QSlider(Qt.Orientation.Horizontal)
        lvl.setRange(1, 3)
        lvl.setValue(int(grp.get("gs_optimize", 3)))
        lvl_label = QLabel(str(grp.get("gs_optimize", 3)))
        lvl.valueChanged.connect(
            lambda v: (grp.__setitem__("gs_optimize", v), lvl_label.setText(str(v)))
        )
        row = QHBoxLayout()
        row.addWidget(lvl_label)
        row.addWidget(lvl, 1)
        fr = self._param_row("advanced.gifsicle.optimize", row)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.gifsicle.optimize")

        # 循环次数 0-100（0=无限）
        loop = QSpinBox()
        loop.setRange(0, 100)
        loop.setValue(int(grp.get("gs_loop", 0)))
        loop.valueChanged.connect(lambda v: grp.__setitem__("gs_loop", v))
        fr = self._param_row("advanced.gifsicle.loop", loop)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.gifsicle.loop")

        # 有损阈值 0-200（0=无损）
        lossy = QSlider(Qt.Orientation.Horizontal)
        lossy.setRange(0, 200)
        lossy.setValue(int(grp.get("gs_lossy", 0)))
        lossy_label = QLabel(str(grp.get("gs_lossy", 0)))
        lossy.valueChanged.connect(
            lambda v: (grp.__setitem__("gs_lossy", v), lossy_label.setText(str(v)))
        )
        row = QHBoxLayout()
        row.addWidget(lossy_label)
        row.addWidget(lossy, 1)
        fr = self._param_row("advanced.gifsicle.lossy", row)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.gifsicle.lossy")

        return w

    def _build_pillow(self):
        grp = self._tool_opts["pillow"]
        w = QWidget()
        apply_transparent(w)
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)
        pq = QSlider(Qt.Orientation.Horizontal)
        pq.setRange(0, 95)
        pq.setValue(int(grp["pil_quality"]))
        pq_spin = QSpinBox()
        pq_spin.setRange(0, 95)
        pq_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        pq_spin.setValue(int(grp["pil_quality"]))
        pq.valueChanged.connect(lambda v: (grp.__setitem__("pil_quality", v), pq_spin.setValue(v)))
        pq_spin.valueChanged.connect(lambda v: (grp.__setitem__("pil_quality", v), pq.setValue(v)))
        pq_row = QHBoxLayout()
        pq_row.addWidget(pq, 1)
        pq_row.addWidget(pq_spin)
        fr = self._param_row("advanced.pil.quality", pq_row)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.pil.quality")
        pil_opt = SwitchButton()
        pil_opt.setChecked(bool(grp["pil_optimize"]))
        pil_opt.checkedChanged.connect(lambda b: grp.__setitem__("pil_optimize", b))
        self._switches.append(pil_opt)
        fr = self._param_row("advanced.pil.optimize", pil_opt)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.pil.optimize")
        pil_prog = SwitchButton()
        pil_prog.setChecked(bool(grp["pil_progressive"]))
        pil_prog.checkedChanged.connect(lambda b: grp.__setitem__("pil_progressive", b))
        self._switches.append(pil_prog)
        fr = self._param_row("advanced.pil.progressive", pil_prog)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.pil.progressive")
        pil_sub = self._make_combo(
            [
                (tr("advanced.pil.sub.444"), "4:4:4"),
                (tr("advanced.pil.sub.422"), "4:2:2"),
                (tr("advanced.pil.sub.420"), "4:2:0"),
            ],
            grp["pil_subsampling"],
            lambda v: grp.__setitem__("pil_subsampling", v),
        )
        fr = self._param_row("advanced.pil.subsampling", pil_sub)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.pil.subsampling")
        return w

    # ------------------------------------------------------------------
    # FFmpeg 压缩面板（视频 / 音频 / 图片 三段独立参数 + 预设档）
    # ------------------------------------------------------------------
    def _ff_profile_mapping(self, kind: str) -> list:
        """某类别的预设下拉项：翻译后的展示名 → 预设名（末项永远是「自定义」）。"""
        presets = compressor.FFMPEG_PRESETS.get(kind, {})
        mapping = [(tr(f"ffmpeg.profile.{name}"), name) for name in presets.keys()]
        mapping.append((tr("ffmpeg.profile.custom"), "custom"))
        return mapping

    def _ff_profile_key(self, kind: str) -> str:
        return {"video": "ff_v_profile", "audio": "ff_a_profile", "image": "ff_i_profile"}[kind]

    def _build_ffmpeg(self):
        """FFmpeg 压缩参数面板：视频 / 音频 / 图片 三个独立分区。"""
        grp = self._tool_opts["ffmpeg"]
        w = QWidget()
        apply_transparent(w)
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(18)
        for kind in ("video", "audio", "image"):
            ly.addWidget(self._build_ffmpeg_category(kind, grp))
        return w

    def _build_ffmpeg_category(self, kind: str, grp: dict):
        params = compressor.FFMPEG_PARAMS_BY_KIND.get(kind, {})
        profile_key = self._ff_profile_key(kind)

        w = QWidget()
        apply_transparent(w)
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)

        # 分区头：类别名 + 预设档下拉
        hdr = QHBoxLayout()
        cat_lbl = StrongBodyLabel(tr(f"ffmpeg.cat.{kind}"))
        apply_text(cat_lbl, sub_text(), transparent=True)
        prof_combo = self._make_combo(
            self._ff_profile_mapping(kind),
            grp.get(profile_key, "balanced"),
            lambda v: self._on_ff_profile(kind, v),
        )
        hdr.addWidget(cat_lbl)
        hdr.addStretch(1)
        hdr.addWidget(prof_combo)
        ly.addLayout(hdr)
        self._ff_cat_headers[kind] = cat_lbl
        self._ff_profile_combos[kind] = prof_combo

        setters: dict = {}
        for pkey, spec in params.items():
            if pkey == profile_key:
                continue
            control, setter = self._build_ff_param(grp, pkey, spec)
            fr = self._param_row(f"ffmpeg.{pkey}", control)
            ly.addWidget(fr)
            attach_help(fr, f"ffmpeg.help.{pkey}")
            setters[pkey] = setter
        self._ff_setters[kind] = setters
        return w

    def _build_ff_param(self, grp: dict, pkey: str, spec: dict):
        """构建一个 ffmpeg 参数控件，返回 (控件, 设值函数)。

        设值函数供「预设档」一键套用时回写控件显示，保证控件与 opts 一致。
        """
        t = spec.get("type")
        if t == "bool":
            ctl = SwitchButton()
            ctl.setChecked(bool(grp.get(pkey, spec.get("default", False))))
            ctl.checkedChanged.connect(lambda b: grp.__setitem__(pkey, b))
            self._switches.append(ctl)
            return ctl, (lambda v: ctl.setChecked(bool(v)))
        if t == "choice":
            vals = spec.get("values", [])
            # 编解码器 / 选项用其技术名作显示（如 libx264 / copy），无需翻译。
            mapping = [(v, v) for v in vals]
            ctl = self._make_combo(
                mapping, grp.get(pkey, spec.get("default")), lambda v: grp.__setitem__(pkey, v)
            )
            return ctl, (lambda v: select_combo_value(ctl, v))
        # int（含带范围的数字参数）
        lo = int(spec.get("min", 0))
        hi = int(spec.get("max", 100))
        cur = int(grp.get(pkey, spec.get("default", lo)) or lo)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(cur)
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        spin.setValue(cur)
        slider.valueChanged.connect(lambda v: (grp.__setitem__(pkey, v), spin.setValue(v)))
        spin.valueChanged.connect(lambda v: (grp.__setitem__(pkey, v), slider.setValue(v)))
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(spin)
        return row, (lambda v: (slider.setValue(int(v)), spin.setValue(int(v))))

    def _on_ff_profile(self, kind: str, preset: str):
        """切换预设档：自定义则保留当前各参数；否则把预设覆盖写回控件与 opts。"""
        grp = self._tool_opts["ffmpeg"]
        grp[self._ff_profile_key(kind)] = preset
        if preset == "custom":
            return
        overrides = compressor.ffmpeg_preset_values(kind, preset)
        setters = self._ff_setters.get(kind, {})
        for k, v in overrides.items():
            if k in grp:
                grp[k] = v
            s = setters.get(k)
            if s:
                s(v)

    def _on_program(self, p):
        self._program = p
        auto = p == "auto"
        # 容器始终可见；auto 显示全部三个组（带分区标题），指定程序只留对应组
        self._backend_container.setVisible(True)
        for key, grp_w in (
            ("oxipng", self.oxipngGroup),
            ("jpegoptim", self.joGroup),
            ("gifsicle", self.gsGroup),
            ("pillow", self.pilGroup),
            ("ffmpeg", self.ffmpegGroup),
        ):
            grp_w.setVisible(auto or p == key)
            grp_w._header.setVisible(auto)
            grp_w._rule.setVisible(auto)
        self._route_hint.setVisible(auto)
        self._refresh_tool_status()
        # Bug3：可见控件数量变了，解除折叠卡片残留的 maximumHeight 上限，
        # 否则新出现的条目会被压扁成一团。
        card = getattr(self, "_settingsCard", None)
        if card is not None:
            card.refresh_content_height()

    def _refresh_tool_status(self):
        if self._program in ("pillow", "auto"):
            self.toolsBtn.setVisible(False)
            self.toolsStatus.setVisible(False)
            return
        installed = False
        if self._program == "oxipng":
            installed = compressor.find_tool("oxipng") is not None
        elif self._program == "jpegoptim":
            installed = compressor.find_tool("jpegoptim") is not None
        elif self._program == "gifsicle":
            installed = compressor.find_tool("gifsicle") is not None
        elif self._program == "ffmpeg":
            installed = compressor.find_tool("ffmpeg") is not None
        self.toolsBtn.setVisible(not installed)
        self.toolsStatus.setVisible(installed)
        if installed:
            self.toolsStatus.setText(tr("compress.tools.done"))

    def _current_mode(self):
        if self._program == "auto":
            return "lossless"
        if self._program == "oxipng":
            return "lossless"
        if self._program == "jpegoptim":
            return self._tool_opts["jpegoptim"].get("jo_mode", "lossless")
        if self._program == "ffmpeg":
            return "lossy"
        return "lossy"

    def _current_quality(self) -> int:
        if self._program == "jpegoptim":
            o = self._tool_opts["jpegoptim"]
            return int(o["jo_max"]) if o.get("jo_mode") == "lossy" else 100
        if self._program == "pillow":
            return int(self._tool_opts["pillow"]["pil_quality"])
        return 100

    def _current_opts(self):
        if self._program == "auto":
            merged: dict = {}
            for g in self._tool_opts.values():
                merged.update(g)
            return merged
        return self._tool_opts.get(self._program, {})

    def _on_output_mode(self, checked):
        self._output_mode = "same" if checked else "fixed"
        cfg.compressMode.value = self._output_mode
        self._apply_output_mode()

    def _apply_output_mode(self):
        same = self._output_mode == "same"
        self.outputSwitch.setChecked(same)
        self.outputSwitch.setText(
            tr("compress.output.same") if same else tr("compress.output.fixed")
        )
        self.suffixRow.setVisible(same)
        self.folderRow.setVisible(not same)

    def _restyle_switches(self):
        for sw in getattr(self, "_switches", []):
            sw.setOnText(tr("common.on"))
            sw.setOffText(tr("common.off"))

    def _pick_output(self):
        """浏览选择固定输出目录（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            d = self._ask_directory(tr("compress.output.browse"), self._folder or "")
            if d:
                self._folder = d
                cfg.compressFolder.value = d
                self.folderEdit.setText(d)
        finally:
            self._picking = False

    def _on_download_tools(self):
        self.toolsBtn.setEnabled(False)
        self.toolsStatus.setVisible(True)
        self.toolsStatus.setText(tr("compress.tools.downloading"))
        worker = ToolsDownloadWorker(self._program, str(tools_dir()))
        worker.signals.finished.connect(self._on_tools_downloaded)
        QThreadPool.globalInstance().start(worker)

    def _on_tools_downloaded(self, tool_id, ok, msg):
        self.toolsBtn.setEnabled(True)
        if ok:
            self.toolsStatus.setText(tr("compress.tools.done"))
        else:
            self.toolsStatus.setText(tr("compress.tools.failed", msg=msg))
        self._refresh_tool_status()

    # =========================================================================
    # 输入处理
    # =========================================================================

    def _on_files(self, paths):
        for p in self._expand_paths(paths, MEDIA_EXTS):
            self._add_item(p)
        self._update_controls()

    def _pick_files(self):
        """弹出媒体文件选择器（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            files = self._ask_open_files(tr("compress.add.files"), MEDIA_EXTS, tr("compress.filter.media"))
            if files:
                self._on_files(files)
        finally:
            self._picking = False

    def _pick_folder(self):
        """弹出文件夹选择器（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            d = self._ask_directory(tr("compress.add.folder"))
            if d:
                self._on_files([d])
        finally:
            self._picking = False

    def _on_target(self, v: str):
        self._target = v
        self.listWidget.set_target(v)

    def _add_item(self, src):
        """入队一个源文件。重复路径由池自己挡掉，这里不用再判一次。"""
        self._pool.add(src, Path(src).name)

    # =========================================================================
    # 快速调用（右键菜单）对接的公开 API ——  ODD-07
    # =========================================================================

    def export_settings(self) -> dict:
        """导出当前压缩设置，供另一个 CompressInterface 实例套用。

        ODD-07 背景：``quick_runner`` 之前是跨模块直接写别人的私有属性
        （``ci._program = ...`` 一路写到 ``ci._folder``），再调私有方法
        ``ci._add_item()`` / ``ci._on_start()``。私有字段一改名，右键菜单链路
        就静默失效，而这条链路没有测试覆盖。用 export/apply 把契约显式化。

        ``_tool_opts`` 做深拷贝：它是 {程序: {参数...}} 的嵌套字典，浅拷贝会让
        弹窗实例和主窗口实例共享同一份子字典，关掉弹窗后再改主窗口参数会串台。
        """
        return {
            "program": self._program,
            "tool_opts": copy.deepcopy(self._tool_opts),
            "target": self._target,
            "output_mode": self._output_mode,
            "suffix": self._suffix,
            "folder": self._folder,
        }

    def apply_settings(self, settings: dict) -> None:
        """套用 :meth:`export_settings` 导出的设置（缺项保持现状）。"""
        self._program = settings.get("program", self._program)
        tool_opts = settings.get("tool_opts")
        if tool_opts is not None:
            self._tool_opts = copy.deepcopy(tool_opts)
        self._target = settings.get("target", self._target)
        self._output_mode = settings.get("output_mode", self._output_mode)
        self._suffix = settings.get("suffix", self._suffix)
        self._folder = settings.get("folder", self._folder)

    def enqueue_and_start(self, paths: list[str]) -> None:
        """把 ``paths`` 加进压缩队列并立即开始（右键菜单入口用）。"""
        for path in paths:
            self._add_item(path)
        self._on_start()

    # =========================================================================
    # 输出路径计算
    # =========================================================================

    def _out_path(self, src: str) -> str:
        """压缩产物的落盘路径。目标格式为「与源相同」时沿用源扩展名。"""
        ext = Path(src).suffix if self._target == "same" else "." + self._target
        return unique_output_path(
            src, ext=ext, output_mode=self._output_mode, suffix=self._suffix, folder=self._folder
        )

    def _max_threads(self) -> int:
        return max(1, min(int(cfg.maxThreads.value), 8))

    # =========================================================================
    # 队列对接（调度本身在 core.task_pool.TaskPool 里）
    # =========================================================================

    def _prepare_item(self, item: PoolItem) -> bool:
        """任务出队前的准备：算好输出路径，并把当前参数冻结进 ``payload``。

        由 TaskPool 在 GUI 线程串行调用。**必须串行**：``_out_path`` 是靠
        「这个文件名存不存在」来给重名文件挑 ``_1`` / ``_2`` 后缀的，两条任务
        并发问同一个问题会同时得到「不存在」，然后一起往同一个路径写。

        Returns:
            ``False`` 表示放弃该任务（池会把它标成失败），只有建不出输出目录
            这种情况会走到这里。旧代码里 ``_out_path`` 抛异常会直接打断整个
            派发循环，剩下的任务全部卡在「等待中」不动。
        """
        try:
            out = self._out_path(item.iid)
        except OSError:
            log.exception("[compress] 输出路径准备失败：%s", item.iid)
            return False
        item.payload = {
            "src": item.iid,
            "out": out,
            "target": self._target,
            "mode": self._current_mode(),
            "quality": self._current_quality(),
            "program": self._program,
            # 深拷贝：_current_opts() 对具体后端返回的是 self._tool_opts 里的
            # **活字典**，直接交给工作线程等于让 UI 和 worker 共享可变状态。
            "opts": copy.deepcopy(self._current_opts()),
        }
        return True

    def _on_pool_added(self, iid: str, name: str) -> None:
        self.listWidget.add_item(iid, iid, self._program, self._target)
        self.taskAdded.emit(iid, name)

    def _on_pool_started(self, iid: str) -> None:
        self.listWidget.set_status(iid, "running")
        widget = self.listWidget.items.get(iid)
        if widget is not None:
            self._queue_auto_follow.ensure(widget)

    def _on_pool_progress(self, iid: str, pct: int) -> None:
        self.listWidget.set_progress(iid, pct)
        self.taskProgress.emit(iid, pct)

    def _on_pool_finished(self, iid: str, state: str, message: str) -> None:
        """把池里的结束状态渲染到列表行。

        ``canceled`` 归到 ``failed`` 展示：``taskFinished`` 的对外契约是
        「done / failed 二选一」，快速调用进度窗靠它计数，多一种取值会漏计。
        """
        item = self._pool.item(iid)
        saved = int(item.result.get("saved", 0)) if item else 0
        backend = str(item.result.get("backend", "")) if item else ""
        status = "done" if state == TaskState.DONE.value else "failed"
        self.listWidget.set_status(iid, status, saved, message, backend)
        self.taskFinished.emit(iid, status)

    def _on_pool_all_finished(self) -> None:
        self._queue_auto_follow.set_active(False)

    def _on_start(self):
        # 与  一致：再点一次「开始」= 重跑所有待处理 / 失败的条目。
        if not any(it.is_restartable for it in self._pool.items()):
            return
        self._queue_auto_follow.set_active(True)
        self._pool.start()

    def _on_pause(self):
        self._pool.toggle_pause()

    def _on_clear(self):
        self._pool.clear()
        self.listWidget.clear()
        self._update_controls()

    def _on_remove(self, item_id):
        self._pool.remove(item_id)
        self.listWidget.remove_item(item_id)

    def _update_controls(self):
        has_items = len(self._pool) > 0
        self.startBtn.setEnabled(has_items and not self._pool.is_busy)
        self.pauseBtn.setEnabled(self._pool.is_running)
        self.clearBtn.setEnabled(has_items)
        self.pauseBtn.setText(
            tr("compress.resume")
            if (self._pool.is_running and self._pool.is_paused)
            else tr("compress.pause")
        )

    # =========================================================================
    # 主题 / i18n
    # =========================================================================

    def retheme(self):
        super().retheme()
        self.dropArea.retheme()

    def retranslateUi(self):
        self.titleLabel.setText(tr("nav.compress"))
        self.subLabel.setText(tr("compress.subtitle"))
        self.tInput.setText(tr("compress.input.title"))
        self.tSettings.setText(tr("compress.settings.title"))
        self.tQueue.setText(tr("compress.queue.title"))
        self.dropArea.retranslate(
            tr("compress.drop.title"), tr("compress.drop.hint"), tr("compress.drop.formats")
        )
        self.addFolderBtn.setText(tr("compress.add.folder"))
        self.toolsBtn.setText(tr("compress.tools.download"))
        self._repopulate_combo(
            self.programCombo,
            [
                (tr("advanced.compression.auto"), "auto"),
                (tr("advanced.compression.oxipng"), "oxipng"),
                (tr("advanced.compression.jpegoptim"), "jpegoptim"),
                (tr("advanced.compression.gifsicle"), "gifsicle"),
                (tr("advanced.compression.pillow"), "pillow"),
                (tr("advanced.compression.ffmpeg"), "ffmpeg"),
            ],
        )
        self._repopulate_combo(
            self.targetCombo,
            [
                (tr("compress.target.same"), "same"),
                ("PNG", "png"),
                ("JPG", "jpg"),
                ("WebP", "webp"),
                ("BMP", "bmp"),
                ("TIFF", "tiff"),
            ],
        )
        # Bug3：后端分区小标题同步语言
        for key, grp_w in (
            ("oxipng", self.oxipngGroup),
            ("jpegoptim", self.joGroup),
            ("gifsicle", self.gsGroup),
            ("pillow", self.pilGroup),
            ("ffmpeg", self.ffmpegGroup),
        ):
            grp_w._header.setText(tr(f"advanced.compression.{key}"))
        # FFmpeg 面板：三类（视频 / 音频 / 图片）各自的分类标题与预设下拉
        for kind, lbl in self._ff_cat_headers.items():
            lbl.setText(tr(f"ffmpeg.cat.{kind}"))
        for kind, combo in self._ff_profile_combos.items():
            self._repopulate_combo(combo, self._ff_profile_mapping(kind))
        # v0.8.1 Bug4-②：field_row 行标签同步语言（此前标签是拿不到引用的局部变量）
        self.backendRow.fieldLabel.setText(tr("advanced.compression.backend"))
        self.targetRow.fieldLabel.setText(tr("compress.target"))
        self.outputModeRow.fieldLabel.setText(tr("compress.output.mode"))
        self.suffixRow.fieldLabel.setText(tr("compress.output.suffix"))
        self.folderRow.fieldLabel.setText(tr("compress.output.folder"))
        for row, key in self._param_rows:
            row.fieldLabel.setText(tr(key))
        self._apply_output_mode()
        self._restyle_switches()
        self.listWidget.retranslate()
        self.startBtn.setText(tr("compress.start"))
        self.pauseBtn.setText(tr("compress.pause"))
        self.clearBtn.setText(tr("compress.clear"))
        self._update_controls()
