"""放大界面 —— 批量 AI 超分辨率放大 / 视频插帧。

职责边界：
- 做：收集输入、按引擎 schema 动态生成参数行、把任务交给 TaskPool、展示前后对比。
- 不做：不执行放大（由 engines 拼出的命令行在 worker 里跑）；不做并发调度。

依赖：core/config、core/engines、core/logger、core/output_path、core/qt_compat、core/task_pool、gui/base、gui/compare_window、gui/drop_area、gui/help_bubble、gui/queue_widget、gui/theme、i18n/translator；被依赖：gui/quick_dialogs。

本界面不硬编码任何引擎，全部由 :mod:`momentshift.core.engines`
的引擎注册表驱动 ——
- 「放大模型」下拉只列出**已安装**的引擎（``tools/<engine-id>/`` 下检测到可执行文件）
- 一个引擎都没有时：下拉禁用并显示「无模型 / 算法可用，请下载」，其余设置项
  全部隐藏，只留一个「检测环境」按钮跳转到关于页
- 有引擎时：按该引擎的参数 schema **动态生成**设置行（模型 / 降噪 / 倍率 /
  分块 / GPU / TTA / 插帧倍率 …），不同引擎参数完全不同

队列调度已下沉到 :class:`~momentshift.core.task_pool.TaskPool`，
本模块与 ``compress_interface`` 里那两套逐行同构的手写线程池循环已删除。
"""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    ComboBox,
    SwitchButton,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from ..core import engines as eng_mod
from ..core.config import cfg
from ..core.logger import get_logger
from ..core.output_path import unique_output_path
from ..core.qt_compat import QApplication, Signal
from ..core.task_pool import PoolItem, ProgressCb, TaskPool, TaskState
from ..i18n.translator import tr
from . import tokens
from .base import (
    InterfaceBase,
    QueueListBase,
    bind_combo_mapping,
    build_detail_label,
    build_row_header,
    build_row_layout,
    combo_mapping,
)
from .compare_window import CompareWindow
from .drop_area import DropArea
from .help_bubble import attach_help
from .queue_widget import (
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
)

log = get_logger("upscale")

# 放大模块支持的视频格式
_VIDEO_EXTS = set(eng_mod.VIDEO_EXTS)
# 放大模块总支持格式
_UPSCALE_EXTS = eng_mod.IMAGE_EXTS | eng_mod.ANIM_EXTS | _VIDEO_EXTS


def _opt_label(raw: str) -> str:
    """解析 schema 里的候选项文案。

    ``@key`` → ``tr(key)``；``Foo (@key)`` → ``Foo (译文)``；其余原样返回。
    """
    if not raw:
        return raw
    if raw.startswith("@"):
        return tr(raw[1:])
    if "(@" in raw and raw.endswith(")"):
        head, _, tail = raw.partition("(@")
        return f"{head.strip()} ({tr(tail[:-1])})"
    return raw


# =============================================================================
# 动态引擎参数面板
# =============================================================================
class EngineParamPanel(QWidget):
    """按引擎的参数 schema 动态生成设置行。"""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        apply_transparent(self)
        self._vb = QVBoxLayout(self)
        self._vb.setContentsMargins(0, 0, 0, 0)
        self._vb.setSpacing(8)
        self._engine: eng_mod.Engine | None = None
        self._controls: dict[str, tuple] = {}  # key -> (kind, widget)
        self._rows: list[QWidget] = []

    # -- 构建 --
    def build(self, engine: eng_mod.Engine | None, values: dict | None = None) -> None:
        self._clear()
        self._engine = engine
        if engine is None:
            return
        values = values or {}
        for p in engine.params:
            widget = self._make_control(p, values.get(p.key, p.default))
            if widget is None:
                continue
            self._controls[p.key] = (p.kind, widget)
            row = field_row(tr(p.label_key), widget)
            self._rows.append(row)
            self._vb.addWidget(row)
            # 功能1：各参数附帮助说明（对齐压缩设置）
            attach_help(row, f"engine.help.{p.key}")

    def _make_control(self, p: eng_mod.Param, current):
        if p.kind == "choice":
            combo = ComboBox()
            mapping = {}
            idx = 0
            for i, (val, label) in enumerate(p.choices):
                text = _opt_label(label)
                combo.addItem(text)
                mapping[text] = val
                if val == current:
                    idx = i
            combo.setCurrentIndex(idx)
            bind_combo_mapping(combo, mapping)
            combo.currentTextChanged.connect(lambda _t: self.changed.emit())
            return combo
        if p.kind == "bool":
            sw = SwitchButton()
            sw.setChecked(bool(current))
            sw.setText(" ")
            sw.checkedChanged.connect(lambda _c, s=sw: (s.setText(" "), self.changed.emit()))
            return sw
        if p.kind == "float":
            spin = QDoubleSpinBox()
            spin.setRange(float(p.minimum), float(p.maximum))
            spin.setSingleStep(float(p.step))
            spin.setDecimals(2)
            spin.setValue(float(current))
            spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
            spin.valueChanged.connect(lambda _v: self.changed.emit())
            return spin
        if p.kind == "int":
            spin = QSpinBox()
            spin.setRange(int(p.minimum), int(p.maximum))
            spin.setSingleStep(int(p.step) or 1)
            spin.setValue(int(current))
            spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            spin.valueChanged.connect(lambda _v: self.changed.emit())
            return spin
        return None

    def _clear(self):
        for row in self._rows:
            self._vb.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._controls.clear()

    # -- 取值 --
    def values(self) -> dict:
        out: dict = {}
        if self._engine is None:
            return out
        for p in self._engine.params:
            entry = self._controls.get(p.key)
            if entry is None:
                out[p.key] = p.default
                continue
            kind, w = entry
            if kind == "choice":
                # 回退到 p.default 而不是显示文案，故不能用 combo_value()
                out[p.key] = combo_mapping(w).get(w.currentText(), p.default)
            elif kind == "bool":
                out[p.key] = w.isChecked()
            else:
                out[p.key] = w.value()
        return out

    def retranslateUi(self) -> None:
        """语言切换后整块重建（候选项文案也需要翻译）。"""
        if self._engine is not None:
            current = self.values()
            self.build(self._engine, current)


# =============================================================================
# 放大执行体（跑在 TaskPool 的工作线程里）/ 队列组件
# =============================================================================
def run_upscale_task(
    item: PoolItem, report: ProgressCb, cancel: threading.Event
) -> tuple[bool, str]:
    """跑一条放大 / 插帧任务。这是喂给 :class:`TaskPool` 的业务执行体。

    Args:
        item: 队列条目。``payload`` 是 :meth:`UpscaleInterface._prepare_item`
            在 GUI 线程冻结好的参数快照。
        report: 进度回调（0~100），直接交给引擎做流式上报（v0.7.7 修复3：
            没有它进度条会一直卡在 0）。
        cancel: 用户清空/移除该任务时被置位。
    Returns:
        ``(是否成功, 引擎给出的明细文本)``。省下的字节数与最终输出路径写进
        ``item.result``，供列表行显示「前后对比」用。
    """
    params = item.payload or {}
    src: str = params["src"]
    out: str = params["out"]
    engine_id: str = params["engine_id"]
    values: dict = params["values"]

    item.result["out"] = out
    item.result["saved"] = 0
    try:
        ok, detail = eng_mod.process_media(engine_id, src, out, values, progress_cb=report)
    except Exception as exc:
        # 保留 str(exc) 作为展示文案：引擎抛出的多是「模型文件缺失」这类
        # 用户能看懂并自行修复的错误，换成通用文案反而帮倒忙。
        log.exception("[upscale] task %s raised", item.iid)
        ok, detail = False, str(exc)
    if ok:
        report(100)

    saved = 0
    try:
        if ok and Path(out).exists():
            saved = Path(src).stat().st_size - Path(out).stat().st_size
    except OSError:
        # 输出文件刚写完就被外部程序挪走/锁住：省了多少不重要，别让任务翻车。
        log.warning("[upscale] 无法统计输出体积：%s", out)
        saved = 0
    item.result["saved"] = saved

    if cancel.is_set() and not ok:
        # 用户中途清空了队列，半成品文件没人要。引擎自身不支持中断，只能在
        # 收尾时补一刀，避免输出目录里留下 0 字节或残缺的图。
        try:
            target = Path(out)
            if target.exists() and target.stat().st_size == 0:
                target.unlink()
        except OSError:
            log.warning("[upscale] 残留文件清理失败：%s", out)
    return bool(ok), str(detail or "")


class UpscaleItemWidget(ThemedCard):
    """放大队列中的单个任务卡片。

    v0.7.6 翻新：对齐「转换队列」视觉 —— 后缀徽标 + 8 字滚动文件名 +
    格式胶囊(.SRC → .TGT) + 状态胶囊 / 进度条 / 大小对比行 + 复制/对比/删除。
    """

    removeRequested = Signal(str)
    compareRequested = Signal(str)

    def __init__(self, item_id: str, src: str, out: str = "", parent=None):
        super().__init__(parent)
        self._id = item_id
        self._src = src
        self._out = out
        self._status = "pending"
        # 调整1：耗时计时
        self._start_time = None
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._on_elapsed_tick)

        vb = build_row_layout(self)

        src_ext = Path(src).suffix.upper().lstrip(".")
        # Adj1：后缀矩形徽标（与转换/压缩队列统一风格）
        self.iconLbl = ext_badge(src_ext, self)
        self.nameLbl = MarqueeName(self)
        self.nameLbl.set_text(Path(src).name)
        self.nameLbl.setObjectName("queueName")
        # 调整1：格式胶囊改为任务耗时显示
        self.timeLbl = QLabel(tr("upscale.elapsed.pending"))
        self.timeLbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timeLbl.setStyleSheet(
            tokens.pill_qss(tokens.SURFACE, tokens.SUCCESS, size=tokens.FONT_CAPTION)
        )
        self.pill = StatusPill("pending")
        vb.addLayout(build_row_header(self.iconLbl, self.nameLbl, self.timeLbl, self.pill))

        self.prog = ProgressBar()
        vb.addWidget(self.prog)

        bottom = QHBoxLayout()
        # 统一到构建器后，本行**新增**了 objectName 与自动换行
        # （三处实现里只有放大队列漏了，长错误信息此前会被截断）。
        self.detailLbl = build_detail_label()
        bottom.addWidget(self.detailLbl, 1)
        self.copyBtn = icon_btn(FIF.COPY)
        self.copyBtn.clicked.connect(self._copy_path)
        bottom.addWidget(self.copyBtn)
        self.cmpBtn = icon_btn(FIF.SEARCH)
        self.cmpBtn.clicked.connect(lambda: self.compareRequested.emit(self._id))
        bottom.addWidget(self.cmpBtn)
        self.delBtn = icon_btn(FIF.DELETE)
        self.delBtn.clicked.connect(lambda: self.removeRequested.emit(self._id))
        bottom.addWidget(self.delBtn)
        vb.addLayout(bottom)

        self.set_status("pending")
        self.set_progress(0)

    def _copy_path(self):
        """复制输出所在目录到剪贴板。

        v0.8.0 RISK-01：``QApplication`` 从来没在本模块导入过，用户一点「复制
        路径」就是 NameError（PyQt 在槽函数里吞掉回溯，界面表现为按钮没反应）。
        现统一从 ``core.qt_compat`` 取，与 queue_widget 的同名逻辑一致。
        """
        folder = str(Path(self._out or self._src).parent)
        QApplication.clipboard().setText(folder)

    def set_progress(self, pct: int):
        self.prog.set_value(pct)

    def set_status(self, status: str, saved: int = 0, detail: str = ""):
        self._status = status
        self.pill.set_status(status)
        self.prog.set_error(status == "failed")
        if status == "running":
            if self._start_time is None:
                import time as _time

                self._start_time = _time.monotonic()
            self._elapsed_timer.start()
            self.detailLbl.setText(tr("upscale.status.upscaling"))
        else:
            self._elapsed_timer.stop()
            if status in ("done", "failed"):
                self._update_elapsed_text()  # 定格最终耗时
        if status == "done":
            # 修复2+3：用 format_size_compare 显示绿/红百分比；进度条满格
            self.set_progress(100)
            src_size = Path(self._src).stat().st_size if Path(self._src).exists() else 0
            dst_size = src_size - saved
            self.detailLbl.setText(format_size_compare(src_size, dst_size))
        elif status == "failed":
            self.detailLbl.setText((detail or tr("convert.status.failed"))[:80])
        elif status not in ("running",):
            self.detailLbl.setText("")

    def _on_elapsed_tick(self):
        self._update_elapsed_text()

    def _update_elapsed_text(self):
        import time as _time

        secs = int(_time.monotonic() - (self._start_time or 0))
        m, s = divmod(max(0, secs), 60)
        self.timeLbl.setText(f"{tr('upscale.elapsed.prefix')} {m}:{s:02d}")

    def retranslate(self):
        self.pill.set_status(self._status)
        self.set_status(self._status)


class UpscaleListWidget(QueueListBase):
    """放大任务列表（带统计栏），继承 QueueListBase 复用统计/空态/增删骨架。

    与另两个队列的差异：行控件带「对比」按钮（``compareRequested`` 信号）、
    状态语义不同（无「压缩中」态）、入队签名多 ``out`` 预览路径。
    """

    removeRequested = Signal(str)
    compareRequested = Signal(str)

    _empty_key = "upscale.queue.empty"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.statTotal, self.statDone, self.statErr = self._statLabels

    def _update_stats(self):
        """重写基类占位：统计总数 / 完成 / 失败（基于行的 ``_status``）。"""
        total = len(self.items)
        done = sum(1 for w in self.items.values() if w._status == "done")
        failed = sum(1 for w in self.items.values() if w._status == "failed")
        self.statTotal.setText(tr("upscale.queue.stats.total", n=total))
        self.statDone.setText(tr("upscale.queue.stats.done", n=done))
        self.statErr.setText(tr("upscale.queue.stats.error", n=failed))

    def add_item(self, item_id: str, src: str, out: str = ""):
        if item_id in self.items:
            return
        w = UpscaleItemWidget(item_id, src, out)
        w.removeRequested.connect(self.removeRequested)
        w.compareRequested.connect(self.compareRequested)
        self._attach_row(item_id, w)
        self._update_stats()

    def set_status(self, item_id: str, status: str, saved: int = 0, detail: str = ""):
        w = self.items.get(item_id)
        if w:
            w.set_status(status, saved, detail)
            self._update_stats()


# =============================================================================
# 放大界面
# =============================================================================
class UpscaleInterface(InterfaceBase):
    """AI 超分辨率放大标签页。

    队列调度委托给 :class:`~momentshift.core.task_pool.TaskPool`（v0.8.0
    DUP-01）；引擎由 :mod:`momentshift.core.engines` 的注册表驱动。
    媒体文件直传队列（无暂存步骤），全局设置驱动整队参数。

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
        super().__init__("Upscale", tr("nav.upscale"), tr("upscale.tagline"), parent)

        # 队列引擎。max_workers 传方法本身，好让设置页改「最大线程数」后下一轮
        # 调度立即生效（放大侧上限比压缩低，见 _max_threads）。
        self._pool = TaskPool(
            run_upscale_task,
            max_workers=self._max_threads,
            parent=self,
            prepare_fn=self._prepare_item,
        )
        self._pool.itemStarted.connect(self._on_pool_started)
        self._pool.itemProgress.connect(self._on_pool_progress)
        self._pool.itemFinished.connect(self._on_pool_finished)
        self._pool.stateChanged.connect(self._update_controls)
        self._pool.allFinished.connect(self._on_pool_all_finished)

        # 整队运行参数快照，_on_start 时从参数面板取一次（见 _on_start 注释）。
        self._run_values: dict | None = None
        # 重入防护：模态对话框自带事件循环，期间用户仍能再次点按钮，
        # 不挡住就会叠出第二个弹框
        self._picking = False

        # 放大参数默认值（：引擎由注册表驱动）
        self._engine_id = ""
        self._fmt = "png"
        self._output_mode = cfg.upscaleMode.value
        self._suffix = cfg.upscaleSuffix.value
        self._folder = cfg.upscaleFolder.value or ""

        # =====================================================================
        # 输入卡片
        # =====================================================================
        card, vb, self.tInput = self._make_card("upscale.input.title")
        self.dropArea = DropArea(self)
        self.dropArea.filesDropped.connect(self._on_files)
        self.dropArea.clicked.connect(self._pick_files)
        vb.addWidget(self.dropArea)
        tools = QHBoxLayout()
        self.addFolderBtn = primary_btn(tr("upscale.add_folder"), icon=FIF.FOLDER_ADD)
        self.addFolderBtn.clicked.connect(self._pick_folder)
        tools.addWidget(self.addFolderBtn)
        vb.addLayout(tools)
        self.vbox.addWidget(card)
        self._inputCard = card

        # =====================================================================
        # 放大设置卡片（：引擎驱动的动态参数面板）
        # =====================================================================
        setc, setvb, self.tSettings = self._make_card("upscale.settings.title")
        self._settingsCard = setc  # 供快速调用设置窗 reparent 复用

        # -- 「放大模型」：只列已安装的引擎 --
        self.modelCombo = ComboBox()
        # 修复5：不设固定最大宽度，让 field_row 决定（对齐下方设置条目）
        self.modelCombo.currentTextChanged.connect(self._on_engine_change)
        self.modelRow = field_row(tr("upscale.model"), self.modelCombo)
        setvb.addWidget(self.modelRow)

        # -- 引擎缺失时的提示 + 检测环境按钮 --
        self.noEngineBox = QWidget(self)
        apply_transparent(self.noEngineBox)
        nb = QVBoxLayout(self.noEngineBox)
        nb.setContentsMargins(0, 4, 0, 0)
        nb.setSpacing(10)
        self.noEngineHint = CaptionLabel(tr("upscale.engine.none_hint"))
        self.noEngineHint.setWordWrap(True)
        apply_text(self.noEngineHint, muted_text(), transparent=True)
        nb.addWidget(self.noEngineHint)
        self.detectBtn = primary_btn(tr("upscale.engine.detect"), icon=FIF.SEARCH)
        self.detectBtn.clicked.connect(self._goto_about)
        nb.addWidget(self.detectBtn)
        setvb.addWidget(self.noEngineBox)

        # -- 动态参数面板 --
        self.paramPanel = EngineParamPanel(self)
        setvb.addWidget(self.paramPanel)

        # -- 输出相关（引擎缺失时整体隐藏）--
        self.outputBox = QWidget(self)
        apply_transparent(self.outputBox)
        ob = QVBoxLayout(self.outputBox)
        ob.setContentsMargins(0, 0, 0, 0)
        ob.setSpacing(8)

        self.fmtCombo = self._make_combo(
            [
                (tr("upscale.fmt.png"), "png"),
                (tr("upscale.fmt.jpg"), "jpg"),
                (tr("upscale.fmt.webp"), "webp"),
            ],
            self._fmt,
            lambda v: setattr(self, "_fmt", v),
        )
        self.fmtRow = field_row(tr("upscale.output.fmt"), self.fmtCombo)
        ob.addWidget(self.fmtRow)

        self.outputSwitch = SwitchButton()
        self.outputSwitch.checkedChanged.connect(self._on_output_mode)
        self.outputModeRow = field_row(tr("upscale.output.mode"), self.outputSwitch)
        ob.addWidget(self.outputModeRow)
        self.suffixEdit = QLineEdit(self._suffix)
        self.suffixEdit.setPlaceholderText(tr("upscale.output.suffix_hint"))
        self.suffixEdit.textChanged.connect(
            lambda t: (setattr(self, "_suffix", t), setattr(cfg.upscaleSuffix, "value", t))
        )
        self.suffixRow = field_row(tr("upscale.output.suffix"), self.suffixEdit)
        ob.addWidget(self.suffixRow)
        self.folderEdit = QLineEdit(self._folder)
        self.folderEdit.setReadOnly(True)
        self.browseBtn = icon_btn(FIF.FOLDER)
        self.browseBtn.clicked.connect(self._pick_output)
        frow = QHBoxLayout()
        frow.addWidget(self.folderEdit, 1)
        frow.addWidget(self.browseBtn)
        self.folderRow = field_row(tr("upscale.output.folder"), frow)
        ob.addWidget(self.folderRow)
        setvb.addWidget(self.outputBox)

        self._apply_output_mode()
        self.vbox.addWidget(setc)

        # =====================================================================
        # 队列卡片
        # =====================================================================
        qcard, qvb, self.tQueue = self._make_card("upscale.queue.title", "upscale.queue.hint")
        self.listWidget = UpscaleListWidget(self)
        self.listWidget.removeRequested.connect(self._on_remove)
        self.listWidget.compareRequested.connect(self._on_compare)
        self.queueScroll = self._make_scroll(280)
        self.queueScroll.setWidget(self.listWidget)
        qvb.addWidget(self.queueScroll)
        # Adj2：队列自动跟随当前处理任务
        self._queue_auto_follow = ScrollAutoFollow(self.queueScroll)
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
        # 引擎扫描必须放在队列控制按钮之后：_update_controls 依赖 startBtn 等
        self.reload_engines()
        self.vbox.addWidget(qcard)

        # =====================================================================
        # 前后对比组件
        # =====================================================================

        self._update_controls()
        self.vbox.addStretch(1)
        self._collapse_ready = True
        self.retheme()

    # =========================================================================
    # 输入处理（文件直传队列，无暂存）
    # =========================================================================

    def _on_files(self, paths):
        self._add_to_queue(self._expand_paths(paths, _UPSCALE_EXTS))

    def _pick_files(self):
        """弹出媒体文件选择器（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            files = self._ask_open_files(tr("upscale.btn.add"), _UPSCALE_EXTS)
            if files:
                self._add_to_queue(self._expand_paths(files, _UPSCALE_EXTS))
        finally:
            self._picking = False

    def _pick_folder(self):
        """弹出文件夹选择器（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            d = self._ask_directory(tr("upscale.add_folder"))
            if d:
                self._add_to_queue(self._expand_paths([d], _UPSCALE_EXTS))
        finally:
            self._picking = False

    def _add_to_queue(self, paths):
        """把文件加进队列。重复路径由池挡掉。

        这里就先算一次输出路径，只为了在列表行上把「会输出到哪」显示出来；
        真正生效的那个由 :meth:`_prepare_item` 在派发前重算（中途改输出设置的
        话，两者可以不一样）。
        """
        if not paths:
            return
        for p in paths:
            preview_out = self._out_path(p)
            if not self._pool.add(p, Path(p).name, payload={"src": p, "out": preview_out}):
                continue
            self.listWidget.add_item(p, p, preview_out)
            self.taskAdded.emit(p, Path(p).name)
        self._update_controls()

    # =========================================================================
    # 快速调用（右键菜单）对接的公开 API ——  ODD-07
    # =========================================================================

    def export_settings(self) -> dict:
        """导出当前放大设置，供另一个 UpscaleInterface 实例套用。

        ODD-07 背景：``quick_runner`` 之前是跨模块直接写别人的私有属性
        （``ui._engine_id = ...`` 一路写到 ``ui._folder``），再调私有方法
        ``ui._add_to_queue()`` / ``ui._on_start()``。这类耦合的代价是：私有字段
        一改名，右键菜单链路就静默失效——而它没有任何测试覆盖。
        用一对 export/apply 把契约显式化。
        """
        try:
            run_values = self.paramPanel.values()
        except (AttributeError, RuntimeError):
            # 无引擎安装时 paramPanel 未 build，取值会失败；此时不带引擎参数。
            run_values = None
        return {
            "engine_id": self._engine_id,
            "fmt": self._fmt,
            "output_mode": self._output_mode,
            "suffix": self._suffix,
            "folder": self._folder,
            "run_values": run_values,
        }

    def apply_settings(self, settings: dict) -> None:
        """套用 :meth:`export_settings` 导出的设置（缺项保持现状）。"""
        self._engine_id = settings.get("engine_id", self._engine_id)
        self._fmt = settings.get("fmt", self._fmt)
        self._output_mode = settings.get("output_mode", self._output_mode)
        self._suffix = settings.get("suffix", self._suffix)
        self._folder = settings.get("folder", self._folder)
        # run_values 为 None 时保持现有值，交给 _on_start 从面板重新取。
        run_values = settings.get("run_values")
        if run_values is not None:
            self._run_values = run_values

    def enqueue_and_start(self, paths: list[str]) -> None:
        """把 ``paths`` 加进放大队列并立即开始（右键菜单入口用）。"""
        self._add_to_queue(paths)
        self._on_start()

    # =========================================================================
    # 引擎装载与切换
    # =========================================================================

    def reload_engines(self) -> None:
        """重新扫描 ``tools/`` 并重建「放大模型」下拉与参数面板。

        无任何引擎时：下拉禁用并显示「无模型 / 算法可用，请下载」，参数面板与
        输出设置全部隐藏，只保留「检测环境」按钮。
        """
        installed = eng_mod.installed_engines()
        self.modelCombo.blockSignals(True)
        self.modelCombo.clear()
        self._engine_map: dict[str, str] = {}

        if not installed:
            self._engine_id = ""
            self.modelCombo.addItem(tr("upscale.engine.none"))
            self.modelCombo.setEnabled(False)
            self.modelCombo.blockSignals(False)
            self.paramPanel.build(None)
            self.paramPanel.setVisible(False)
            self.outputBox.setVisible(False)
            self.noEngineBox.setVisible(True)
            self._update_controls()
            return

        for e in installed:
            full_label = f"{e.name}  ·  {'/'.join(e.algos)}"
            if e.is_interp:
                full_label = f"{full_label}  [{tr('engine.group.interp')}]"
            # 超出 32 字截断 + …（qfluentwidgets ComboBox 不支持逐项 tooltip）
            label = full_label if len(full_label) <= 32 else full_label[:31] + "…"
            self.modelCombo.addItem(label)
            self._engine_map[label] = e.eid
            self._engine_map[label] = e.eid
        if self._engine_id not in [e.eid for e in installed]:
            self._engine_id = installed[0].eid
        for i, e in enumerate(installed):
            if e.eid == self._engine_id:
                self.modelCombo.setCurrentIndex(i)
                break
        self.modelCombo.setEnabled(True)
        self.modelCombo.blockSignals(False)

        self.noEngineBox.setVisible(False)
        self.paramPanel.setVisible(True)
        self.outputBox.setVisible(True)
        self._rebuild_params()
        self._update_controls()

    def _on_engine_change(self, text: str) -> None:
        eid = getattr(self, "_engine_map", {}).get(text)
        if not eid or eid == self._engine_id:
            return
        self._engine_id = eid
        self._rebuild_params()
        self._update_controls()

    def _rebuild_params(self) -> None:
        engine = eng_mod.ENGINE_BY_ID.get(self._engine_id)
        self.paramPanel.build(engine, eng_mod.default_values(self._engine_id))
        # 插帧引擎不吃静态图片，输出格式行没有意义
        self.fmtRow.setVisible(not (engine and engine.is_interp))

    def _goto_about(self) -> None:
        win = self.window()
        if hasattr(win, "goto_about"):
            win.goto_about()

    # =========================================================================
    # 设置交互
    # =========================================================================

    def _on_output_mode(self, checked):
        self._output_mode = "same" if checked else "fixed"
        cfg.upscaleMode.value = self._output_mode
        self._apply_output_mode()

    def _apply_output_mode(self):
        same = self._output_mode == "same"
        self.outputSwitch.setChecked(same)
        self.outputSwitch.setText(tr("convert.output.same") if same else tr("convert.output.fixed"))
        self.suffixRow.setVisible(same)
        self.folderRow.setVisible(not same)

    def _pick_output(self):
        """浏览选择固定输出目录（带重入防护）。"""
        if self._picking:
            return
        self._picking = True
        try:
            d = self._ask_directory(tr("convert.output.browse"), self._folder or "")
            if d:
                self._folder = d
                cfg.upscaleFolder.value = d
                self.folderEdit.setText(d)
        finally:
            self._picking = False

    def _out_path(self, src: str) -> str:
        """放大产物的落盘路径。

        v0.7.5：GIF / 视频保持原容器，只有静态图片才套用「输出格式」下拉的值。
        """
        src_ext = Path(src).suffix.lower()
        ext = "." + self._fmt if src_ext in eng_mod.IMAGE_EXTS else (src_ext or ".mp4")
        return unique_output_path(
            src, ext=ext, output_mode=self._output_mode, suffix=self._suffix, folder=self._folder
        )

    def _max_threads(self) -> int:
        return max(1, min(int(cfg.maxThreads.value), 4))

    # =========================================================================
    # 前后对比
    # =========================================================================

    def _on_compare(self, item_id):
        item = self._pool.item(item_id)
        if item is None:
            return
        payload = item.payload or {}
        # result["out"] 是真正跑出来的那个路径，payload["out"] 是入队时的预估值。
        out = item.result.get("out") or payload.get("out", "")
        self._show_compare(payload.get("src", item_id), out)

    def _show_compare(self, src, out):
        """弹出 1280×720 放大前后对比窗口（v0.3.5）。"""
        dlg = CompareWindow(src, out, parent=self.window())
        dlg.exec()

    # =========================================================================
    # 队列对接（调度本身在 core.task_pool.TaskPool 里）
    # =========================================================================

    def _prepare_item(self, item: PoolItem) -> bool:
        """任务出队前重算输出路径并冻结引擎参数。由池在 GUI 线程串行调用。

        **必须串行**：``_out_path`` 用「文件存不存在」给重名文件挑 ``_1`` /
        ``_2`` 后缀，并发调用会给两条任务分到同一个文件名。
        """
        try:
            out = self._out_path(item.iid)
        except OSError:
            log.exception("[upscale] 输出路径准备失败：%s", item.iid)
            return False
        values = self._run_values or self.paramPanel.values()
        item.payload = {
            "src": item.iid,
            "out": out,
            "engine_id": self._engine_id,
            # 拷一份：参数面板的 values() 每次返回新字典，但调用方可能传的是
            # apply_settings 塞进来的外部字典，别让工作线程读到 UI 侧的活对象。
            "values": dict(values or {}),
        }
        return True

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
        「done / failed 二选一」，快速调用进度窗靠它计数。
        """
        item = self._pool.item(iid)
        saved = int(item.result.get("saved", 0)) if item else 0
        status = "done" if state == TaskState.DONE.value else "failed"
        self.listWidget.set_status(iid, status, saved, message)
        self.taskFinished.emit(iid, status)

    def _on_pool_all_finished(self) -> None:
        self._queue_auto_follow.set_active(False)

    def _on_start(self):
        if not self._engine_id or not eng_mod.find_engine(self._engine_id):
            QMessageBox.warning(self, tr("common.warning"), tr("upscale.toast.no_engine"))
            return
        # 与  一致：再点一次「开始」= 重跑所有待处理 / 失败的条目。
        if not any(it.is_restartable for it in self._pool.items()):
            return
        # 开跑瞬间快照参数面板，运行途中改设置不影响本轮任务。
        self._run_values = self.paramPanel.values()
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
        ready = bool(self._engine_id) and bool(eng_mod.find_engine(self._engine_id))
        has_items = len(self._pool) > 0
        self.startBtn.setEnabled(ready and has_items and not self._pool.is_busy)
        self.pauseBtn.setEnabled(self._pool.is_running)
        self.clearBtn.setEnabled(has_items)
        self.pauseBtn.setText(
            tr("convert.resume")
            if (self._pool.is_running and self._pool.is_paused)
            else tr("convert.pause")
        )

    # =========================================================================
    # 主题 / i18n
    # =========================================================================

    def retheme(self):
        super().retheme()
        self.dropArea.retheme()

    def retranslateUi(self):
        self.titleLabel.setText(tr("nav.upscale"))
        self.subLabel.setText(tr("upscale.tagline"))
        self.tInput.setText(tr("upscale.input.title"))
        self.tSettings.setText(tr("upscale.settings.title"))
        self.tQueue.setText(tr("upscale.queue.title"))
        self.dropArea.retranslate(
            tr("upscale.drop.title"), tr("upscale.drop.hint"), tr("upscale.drop.formats")
        )
        self.addFolderBtn.setText(tr("upscale.add_folder"))
        self.noEngineHint.setText(tr("upscale.engine.none_hint"))
        self.detectBtn.setText(tr("upscale.engine.detect"))
        # v0.8.1 Bug4-②：field_row 行标签同步语言（此前标签是拿不到引用的局部变量）
        self.modelRow.fieldLabel.setText(tr("upscale.model"))
        self.fmtRow.fieldLabel.setText(tr("upscale.output.fmt"))
        self.outputModeRow.fieldLabel.setText(tr("upscale.output.mode"))
        self.suffixRow.fieldLabel.setText(tr("upscale.output.suffix"))
        self.folderRow.fieldLabel.setText(tr("upscale.output.folder"))
        self.reload_engines()
        self._apply_output_mode()
        self.startBtn.setText(tr("convert.start"))
        self.pauseBtn.setText(tr("convert.pause"))
        self.clearBtn.setText(tr("convert.clear"))
        self.listWidget.retranslate()
        self._update_controls()
