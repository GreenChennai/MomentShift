"""压缩界面 —— 批量图片 / 音频 / 视频压缩（V0.8.18 重构版）。

职责边界：
- 做：输入卡片收集文件 → **按文件类型**分别打开「创建压缩任务」弹窗
  （:class:`QuickCompressDialog`）→ 确认后按冻结设置入队 → 展示队列与结果；
  提供「输出位置」卡片（与大组件压缩弹窗内的保存位置双向同步）。
- 不做：不做队列调度、并发上限、暂停/继续与 worker 生命周期管理，
  这些一律交给 :class:`~momentshift.core.task_pool.TaskPool`。

V0.8.18 变更（文件入队重构）：
- 旧流程「选文件 → 直接入队 → 按主界面压缩设置压缩」改为
  「选文件 → 每类文件单独打开创建压缩任务窗口 → 按该窗口设置压缩 → 入队」。
- 主界面**删除「压缩设置」卡片**，保留「输出位置」卡片（样式对齐转换界面）；
  弹窗内的文件保存位置与主界面「输出位置」双向同步（同一份
  ``cfg.compressMode/Suffix/Folder``）。
- 主界面入队的任务**不自动开始**（用户按「开始」）；右键快速调用入队的
  任务**自动开始**（见 ``quick_runner``）。
- 每个任务携带**冻结设置快照**（``payload``），运行期间改设置不影响已入队任务。

依赖：core/compressor、core/config、core/logger、core/output_path、core/presets、
core/qt_compat、core/task_pool、gui/compress_task_panel、gui/drop_area、
gui/queue_widget、gui/quick_dialogs、gui/theme、i18n/translator；被依赖：
gui/quick_dialogs、quick_runner。
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    SwitchButton,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from ..core import compressor
from ..core.config import cfg
from ..core.logger import get_logger
from ..core.output_path import unique_output_path
from ..core.presets import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS
from ..core.qt_compat import QThreadPool, Signal
from ..core.task_pool import PoolItem, ProgressCb, TaskPool, TaskState
from ..i18n.translator import tr
from .base import InterfaceBase, QueueListBase, build_detail_label, build_row_header, build_row_layout
from .compress_task_panel import compress_kind, settings_mode, settings_opts, settings_quality
from .drop_area import DropArea
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
    ext_badge,
    field_row,
    ghost_btn,
    icon_btn,
    muted_text,
    primary_btn,
)

# 压缩模块受理的媒体范围（带点的扩展名集合，供文件对话框筛选与后缀校验）。
MEDIA_EXTS = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS

log = get_logger("compress")


# =============================================================================
# 压缩执行体（跑在 TaskPool 的工作线程里）
# =============================================================================
def run_compress_task(
    item: PoolItem, report: ProgressCb, cancel: threading.Event
) -> tuple[bool, str]:
    """压缩一个媒体文件（图片 / 音频 / 视频）。喂给 :class:`TaskPool` 的业务执行体。

    Args:
        item: 队列条目。``payload`` 是 V0.8.18 起在「创建压缩任务」弹窗确认时
            冻结好的参数快照（含 program/mode/quality/target/opts/输出设置）。
        report: 进度回调（0~100）。
        cancel: 用户清空/移除该任务时被置位。
    Returns:
        ``(是否成功, 展示给用户的明细文本)``。省下的字节数与实际生效的后端另外
        写进 ``item.result``——Qt 信号只带得回两个值，塞不下业务细节。
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
        compressor.cleanup_temp_files(out)
        return False, "exception (see log)"

    item.result["saved"] = int(saved or 0)
    if cancel.is_set():
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
            if (
                backend
                and self._selected != backend
                and backend in BACKEND_NAMES
            ):
                name = BACKEND_NAMES.get(backend, backend)
                self.pill.set_status("done_sw", text=tr("compress.done.by").format(backend=name))
            else:
                self.pill.set_status("done")
            self.prog.set_error(False)
            self.prog.set_value(100)
            before = self._src_size or self._read_src_size()
            if before:
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
    """压缩任务列表（带统计栏），继承 QueueListBase 复用统计/空态/增删骨架。"""

    removeRequested = Signal(str)

    _empty_key = "compress.queue.empty"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.statTotal, self.statDone, self.statErr = self._statLabels

    def _update_stats(self):
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
    """压缩标签页（V0.8.18：弹窗式入队 + 输出位置卡片 + 每项冻结设置）。

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

        # 兼容 API 用的实例级默认设置（export/apply/enqueue_and_start 等 ODD-07
        # 契约；V0.8.18 正常入队走弹窗快照，这些只作为兜底默认值）
        self._program = "auto"
        self._target = "same"
        from .compress_task_panel import _default_tool_opts

        self._tool_opts = _default_tool_opts()
        self._output_mode = cfg.compressMode.value
        self._suffix = cfg.compressSuffix.value or ""
        self._folder = cfg.compressFolder.value or ""

        # =====================================================================
        # 输入卡片
        # =====================================================================
        card, vb, self.tInput = self._make_card("compress.input.title")
        self.dropArea = DropArea(self)
        self.dropArea.filesDropped.connect(self._open_setup)
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
        # 输出位置卡片（V0.8.18：替代「压缩设置」内的输出控件，样式对齐转换）
        # =====================================================================
        ocard, ovb, self.tOutput = self._make_card("compress.output.title", collapsed=True)
        self.outputSwitch = SwitchButton()
        self.outputSwitch.checkedChanged.connect(self._on_output_mode)
        self.outputModeRow = field_row(tr("compress.output.mode"), self.outputSwitch)
        ovb.addWidget(self.outputModeRow)
        self.suffixEdit = QLineEdit(self._suffix)
        self.suffixEdit.setPlaceholderText(tr("compress.output.suffix_hint"))
        self.suffixEdit.textChanged.connect(self._on_suffix_changed)
        self.suffixRow = field_row(tr("compress.output.suffix"), self.suffixEdit)
        ovb.addWidget(self.suffixRow)
        self.folderEdit = QLineEdit(self._folder)
        self.folderEdit.setReadOnly(True)
        self.browseBtn = icon_btn(FIF.FOLDER, self)
        self.browseBtn.setFixedSize(36, 36)
        self.browseBtn.clicked.connect(self._pick_output)
        frow = QHBoxLayout()
        frow.addWidget(self.folderEdit, 1)
        frow.addWidget(self.browseBtn)
        self.folderRow = field_row(tr("compress.output.folder"), frow)
        ovb.addWidget(self.folderRow)
        self._apply_output_mode()
        self.vbox.addWidget(ocard)

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

        self._update_controls()
        self.vbox.addStretch(1)
        self._collapse_ready = True
        self.retheme()

    # =========================================================================
    # 输入处理（V0.8.18：按文件类型分别弹「创建压缩任务」窗口）
    # =========================================================================

    def _open_setup(self, paths: list[str]):
        """展开路径 → 按文件类型分组 → 每类单独打开创建压缩任务弹窗。

        需求 V0.8.18-1：不再直接入队；每种类型一个窗口，确认后才入队。
        主界面（非快速调用）入队的任务**不自动开始**，等用户按「开始」。
        """
        expanded = self._expand_paths(paths, MEDIA_EXTS)
        if not expanded:
            return
        from .quick_dialogs import QuickCompressDialog

        groups: dict[str, list[str]] = {}
        for p in expanded:
            k = compress_kind(p)
            if k:
                groups.setdefault(k, []).append(p)
        if not groups:
            return
        for kind, kind_paths in groups.items():
            dlg = QuickCompressDialog(
                self.window(), kind, kind_paths, on_confirm=self._enqueue, main_iface=self
            )
            dlg.exec()
        self._update_controls()

    def _on_files(self, paths):
        self._open_setup(paths)

    def _pick_files(self):
        """弹出媒体文件选择器（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            files = self._ask_open_files(tr("compress.add.files"), MEDIA_EXTS, tr("compress.filter.media"))
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
            d = self._ask_directory(tr("compress.add.folder"))
            if d:
                self._open_setup([d])
        finally:
            self._picking = False

    def _enqueue(self, paths: list[str], settings: dict):
        """创建压缩任务弹窗确认回调：按快照入队（不自动开始）。"""
        self.enqueue_with_settings([(p, dict(settings or {})) for p in paths], auto_start=False)

    def enqueue_with_settings(self, items: list[tuple[str, dict]], auto_start: bool = False) -> None:
        """按冻结设置入队（V0.8.18 主入口）。

        Args:
            items: ``[(源文件路径, 设置快照), ...]``。快照键与
                :func:`run_compress_task` 的 payload 一致。
            auto_start: True = 入队后立即开始（右键快速调用）；
                False = 只入队，等用户按「开始」（主界面）。
        """
        for src, settings in items:
            payload = dict(settings or {})
            payload["src"] = src
            self._pool.add(src, Path(src).name, payload=payload)
        self._update_controls()
        if auto_start:
            self._on_start()

    # =========================================================================
    # 输出位置设置（与弹窗内保存位置双向同步）
    # =========================================================================

    def _on_output_mode(self, checked: bool):
        """切换输出模式：同目录 + 后缀 vs 固定目录。"""
        self._output_mode = "same" if checked else "fixed"
        cfg.compressMode.value = self._output_mode
        self._apply_output_mode()

    def _on_suffix_changed(self, text: str):
        self._suffix = text
        cfg.compressSuffix.value = text

    def _apply_output_mode(self):
        """根据当前输出模式显示/隐藏对应 UI 行。"""
        same = self._output_mode == "same"
        self.outputSwitch.setChecked(same)
        self.outputSwitch.setText(
            tr("compress.output.same") if same else tr("compress.output.fixed")
        )
        self.suffixRow.setVisible(same)
        self.folderRow.setVisible(not same)

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

    def apply_output_state(self, mode: str, suffix: str, folder: str) -> None:
        """弹窗内保存位置变更 → 同步主界面「输出位置」卡片（V0.8.18-3）。

        弹窗是模态的，主界面控件虽然被挡在下面，但改动必须即时反映过去，
        这样关闭弹窗后主界面的「输出位置」就是最后一次确认的样子。
        """
        self._output_mode = mode
        self._suffix = suffix or ""
        self._folder = folder or ""
        cfg.compressMode.value = self._output_mode
        cfg.compressSuffix.value = self._suffix
        cfg.compressFolder.value = self._folder
        self.suffixEdit.setText(self._suffix)
        self.folderEdit.setText(self._folder)
        self._apply_output_mode()

    # =========================================================================
    # 快速调用（右键菜单）对接的公开 API ——  ODD-07（V0.8.18 保留为兼容层）
    # =========================================================================

    def export_settings(self) -> dict:
        """导出当前实例级默认设置（兼容 ODD-07；正常入队走弹窗快照）。"""
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
        self._apply_output_mode()

    def enqueue_and_start(self, paths: list[str]) -> None:
        """按实例级默认设置入队并立即开始（兼容 ODD-07 语义）。"""
        settings = {
            "program": self._program,
            "target": self._target,
            "mode": settings_mode(self._program, self._tool_opts),
            "quality": settings_quality(self._program, self._tool_opts),
            "opts": settings_opts(self._program, self._tool_opts),
        }
        self.enqueue_with_settings([(p, dict(settings)) for p in paths], auto_start=True)

    # =========================================================================
    # 输出路径计算
    # =========================================================================

    def _out_path(self, src: str, settings: dict | None = None) -> str:
        """压缩产物的落盘路径（按该项的冻结设置，缺项回退实例默认值）。

        Args:
            src: 源文件路径。
            settings: 该项设置快照（V0.8.18 起每项携带）；None 时用默认。
        """
        s = settings or {}
        target = s.get("target", self._target)
        ext = Path(src).suffix if target in ("same", "", None) else "." + target
        return unique_output_path(
            src,
            ext=ext,
            output_mode=s.get("output_mode", self._output_mode),
            suffix=s.get("suffix", self._suffix),
            folder=s.get("folder", self._folder),
        )

    def _max_threads(self) -> int:
        return max(1, min(int(cfg.maxThreads.value), 8))

    # =========================================================================
    # 队列对接（调度本身在 core.task_pool.TaskPool 里）
    # =========================================================================

    def _prepare_item(self, item: PoolItem) -> bool:
        """任务出队前准备：按该项快照算输出路径，并补齐 src/out。

        快照（program/mode/quality/target/opts/输出三件套）在弹窗确认时已冻结，
        这里只补 ``src`` 与 ``out``；若某条任务没有快照（兼容 API 兜底），
        用实例级默认值补齐。输出路径去重依赖「文件是否已存在」的串行探测，
        因此本函数必须由 TaskPool 在 GUI 线程串行调用。
        """
        payload = dict(item.payload or {})
        # 缺项兜底：任何入队路径（enqueue_with_settings / enqueue_and_start）
        # 都保证 payload 至少能喂给 run_compress_task
        defaults = {
            "program": self._program,
            "target": self._target,
            "mode": settings_mode(self._program, self._tool_opts),
            "quality": settings_quality(self._program, self._tool_opts),
            "opts": settings_opts(self._program, self._tool_opts),
        }
        for key, value in defaults.items():
            payload.setdefault(key, value)
        try:
            out = self._out_path(item.iid, payload)
        except OSError:
            log.exception("[compress] 输出路径准备失败：%s", item.iid)
            return False
        payload["src"] = item.iid
        payload["out"] = out
        item.payload = payload
        return True

    def _on_pool_added(self, iid: str, name: str) -> None:
        # V0.8.18：行的「程序 / 目标格式」取自该项自己的冻结快照
        item = self._pool.item(iid)
        payload = item.payload if item else None
        program = (payload or {}).get("program", self._program)
        target = (payload or {}).get("target", self._target)
        self.listWidget.add_item(iid, iid, program, target)
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
        # 再点一次「开始」= 重跑所有待处理 / 失败的条目。
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
        self.tOutput.setText(tr("compress.output.title"))
        self.tQueue.setText(tr("compress.queue.title"))
        self.dropArea.retranslate(
            tr("compress.drop.title"), tr("compress.drop.hint"), tr("compress.drop.formats")
        )
        self.addFolderBtn.setText(tr("compress.add.folder"))
        self.outputModeRow.fieldLabel.setText(tr("compress.output.mode"))
        self.suffixRow.fieldLabel.setText(tr("compress.output.suffix"))
        self.folderRow.fieldLabel.setText(tr("compress.output.folder"))
        self.suffixEdit.setPlaceholderText(tr("compress.output.suffix_hint"))
        self._apply_output_mode()
        self.listWidget.retranslate()
        self.startBtn.setText(tr("compress.start"))
        self.pauseBtn.setText(tr("compress.pause"))
        self.clearBtn.setText(tr("compress.clear"))
        self._update_controls()
