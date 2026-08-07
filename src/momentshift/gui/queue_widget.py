"""转换队列的界面组件：进度条、状态胶囊、条目卡片、列表控件。

职责边界：
- 做：把队列任务渲染成可滚动的条目卡片，提供进度 / 状态更新与删除 / 重试信号。
- 不做：不管理任务调度（交给 core/queue 与 core/task_pool）。

依赖：core/qt_compat、i18n/translator、gui/animations、gui/base、gui/theme、gui/tokens；
被依赖：gui/convert_interface、gui/compress_interface、gui/upscale_interface。

v0.8.0 B3 动效接入点：``StatusPill`` 状态底色渐变、``ProgressBar`` 进度补间。
两者都只改「变化过程」，稳态样式与改造前逐字节一致（见各自的 Notes）。

公开 API：
- human_size(n) / format_size_compare(before, after)
- StatusPill(status) + set_status(status)
- ProgressBar() + set_value(int) / set_error(bool)
- QueueItemWidget(task) + set_progress/set_status/retranslate
  及信号 removeRequested(str) / retryRequested(str)
- QueueListWidget() + add_item/update_progress/update_status/remove_item/
  sync/clear/retranslate/_update_stats 及同名两个信号
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, pyqtProperty
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QScrollArea, QSizePolicy
from qfluentwidgets import BodyLabel
from qfluentwidgets import FluentIcon as FIF

from ..core.ffmpeg_progress import format_eta, format_speed
from ..core.logger import get_logger
from ..core.qt_compat import QApplication, QWidget, Signal
from ..i18n.translator import tr
from . import animations, tokens
from .base import (
    QueueListBase,
    build_detail_label,
    build_row_header,
    build_row_layout,
)
from .theme import (
    ThemedCard,
    accent_color,
    border_color,
    danger_color,
    ext_badge,
    icon_btn,
    success_color,
    text_strong,
)

log = get_logger("queue_widget")

# 详情行里各段之间的分隔符。用全角间距而不是 " | "：详情行是 CaptionLabel 小字，
# 竖线在小字号下会和相邻数字糊成一团，中点加宽间距在视觉上更松弛。
DETAIL_SEP = "   ·   "


# --------------------------------------------------------------------------
# 格式化辅助函数
# --------------------------------------------------------------------------
def join_detail(*parts: str) -> str:
    """把详情行的若干片段用 :data:`DETAIL_SEP` 连接，自动跳过空串。

    Args:
        *parts: 片段文本，``None`` / 空串会被丢弃。
    Returns:
        连接后的字符串；全为空时返回空串。
    """
    return DETAIL_SEP.join(p for p in parts if p)


def format_stats(snap) -> str:
    """把一次 :class:`~core.ffmpeg_progress.ProgressSnapshot` 渲染成
    ``1.53x   ·   剩余 02:31``。

    Args:
        snap: 进度快照；``None`` 或跑完（``finished``）时返回空串。
    Returns:
        可直接拼进详情行的文本；速度与剩余时间都拿不到时返回空串。

    Notes:
        **跑完就不显示**：``progress=end`` 那一帧的 ETA 恒为 0、速度是全程均值，
        贴在「已完成」旁边只会误导，所以直接吐空串让详情行回落到大小对比。
    """
    if snap is None or getattr(snap, "finished", False):
        return ""
    parts: list[str] = []
    spd = format_speed(getattr(snap, "speed", None))
    if spd != "--":
        parts.append(spd)
    eta = getattr(snap, "eta_sec", None)
    if eta is not None and eta >= 0:
        parts.append(tr("convert.label.eta", t=format_eta(eta)))
    return join_detail(*parts)



def human_size(n: int) -> str:
    n = int(n or 0)
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}"


def format_size_compare(before: int, after: int) -> str:
    """返回富文本：``1.2 MB → 800.0 KB <font color>(-33%)</font>``。

    百分比颜色：变小取 ``tokens.SUCCESS``，变大取 ``tokens.DANGER``，
    几乎无变化取 ``tokens.TEXT_BLACK``。
    """
    before, after = int(before or 0), int(after or 0)
    if not before:
        return human_size(after) if after else ""
    if not after:
        return human_size(before)
    delta = (after - before) / before * 100
    if abs(delta) < 0.5:
        pct = "±0%"
        color = tokens.TEXT_BLACK
    elif delta < 0:
        pct = f"{delta:.0f}%"
        color = tokens.SUCCESS
    else:
        pct = f"+{delta:.0f}%"
        color = tokens.DANGER
    return f'{human_size(before)} → {human_size(after)} <font color="{color}">({pct})</font>'


# 状态胶囊配色，规则是「胶囊底 = 状态色，文字 = 反色」：
# 底色用高饱和的状态色且不随主题变化，文字统一取近白色，
# 这样无论落在哪种状态色上、无论明暗主题都能读清。
# 状态流转：
# 等待中(灰) → 转换中(蓝) → 已完成(绿) →〔开启压缩时〕压缩中(黄) → 压缩完成(蓝)
_STATUS_PILL_BG = {
    "pending": tokens.PENDING,
    "running": tokens.RUNNING,
    "done": tokens.SUCCESS,
    "failed": tokens.DANGER,
    "canceled": tokens.PENDING,
    "compressing": tokens.WARNING,
    "compress_done": tokens.INFO,
    "done_sw": tokens.INFO,
}
_STATUS_PILL_FG = tokens.SURFACE


# --------------------------------------------------------------------------
# 进度条
# --------------------------------------------------------------------------
class ProgressBar(QWidget):
    """队列行内的细进度条。

    为什么自绘而不用 QProgressBar：需要极扁的高度与圆角，
    且要能跟随主题实时改色，改 QSS 在各平台表现不一致。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0
        self._error = False
        self.setFixedHeight(6)

    # -- 平滑追值（v0.8.0 B3 接入点 5）-----------------------------------
    # 属性动画只能作用在 Qt 属性上，而 ``_value`` 是普通 Python 整数，
    # 因此包一层 ``barValue``：对外仍是 0~100 的整数语义，对内给动画一个
    # 可插值的浮点通道。取整放在 setter 里，绘制逻辑与改造前完全一致。
    def _get_bar_value(self) -> float:
        return float(self._value)

    def _set_bar_value(self, value: float) -> None:
        self._value = max(0, min(100, int(round(value))))
        self.update()

    barValue = pyqtProperty(float, fget=_get_bar_value, fset=_set_bar_value)

    def set_value(self, v: int):
        """设置进度百分比，必要时补一段短平滑。

        Args:
            v: 目标百分比，会被夹到 0~100。

        Notes:
            只对「足够大的**前进**」补间，两种情况一律硬切：

            1. 增量小于 ``PROGRESS_SMOOTH_MIN_STEP`` —— 本来就连续，补间无意义；
            2. 进度**回退**（重试 / ``set_value(0)`` 重置）—— 让进度条慢慢缩回去
               是在撒谎，用户需要立刻看到「这条被重置了」。

            高频回调不会积压：同一属性上的旧动画会被 :func:`animations.animate_value`
            先行打断，永远只有一段在跑，起点取当前实际值，不会出现追赶抖动。
        """
        target = max(0, min(100, int(v)))
        if target - self._value < animations.PROGRESS_SMOOTH_MIN_STEP:
            animations.stop(self, b"barValue")
            self._set_bar_value(target)
            return
        animations.animate_value(
            self,
            b"barValue",
            float(target),
            duration=animations.DURATION_FAST,
            curve=animations.CURVE_SMOOTH,
            animate=animations.should_animate(self),
        )

    def set_error(self, b: bool):
        """切换失败态配色。

        Args:
            b: 是否失败。

        Notes:
            失败时顺手掐掉在跑的补间 —— 任务都已经挂了，进度条还在往前爬
            是明确的错误信息，必须定格在出错那一刻。
            成功路径（``b=False``）不打断，否则每次状态刷新都会把补间掐断。
        """
        self._error = bool(b)
        if self._error:
            animations.stop(self, b"barValue")
        self.update()

    def paintEvent(self, event):
        from PyQt6.QtCore import QRect

        w, h = self.width(), self.height()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        track = QColor(border_color())
        painter.setBrush(QBrush(track))
        painter.drawRoundedRect(QRect(0, 0, w, h), h // 2, h // 2)
        fw = int(w * self._value / 100)
        if fw <= 0:
            return
        if self._error:
            fill = danger_color()
        elif self._value >= 100:
            fill = success_color()
        else:
            fill = accent_color()
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(QRect(0, 0, fw, h), h // 2, h // 2)


# --------------------------------------------------------------------------
# 状态胶囊
# --------------------------------------------------------------------------
class StatusPill(QLabel):
    """展示任务状态的圆角色块，配色取自 _STATUS_PILL_BG。

    v0.8.0 B3 接入点 2：状态切换时底色不再瞬切，而是过渡 200ms。
    这是三个队列页里信息密度最高的一处 —— 一行里同时有文件名、格式、进度、
    大小对比，状态胶囊瞬间从灰变绿很容易被眼睛漏掉；给一段可追踪的颜色位移，
    余光就能捕捉到「刚才那条完成了」。

    为什么不用 ``QGraphicsColorizeEffect`` / 淡入淡出：胶囊的文字必须全程清晰，
    整体透明度变化会让文案跟着糊；这里只动 QSS 里的 ``background`` 一个值。
    """

    def __init__(self, status: str = "pending", parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 修复1：胶囊严格按内部文字定宽，绝不随 UI 宽度拉伸
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._status = "pending"
        self._bg = _STATUS_PILL_BG["pending"]
        self._bg_from = self._bg
        self._bg_to = self._bg
        self._bg_t = 1.0
        self.set_status(status)

    # -- 底色渐变驱动 ---------------------------------------------------
    def _get_bg_t(self) -> float:
        return self._bg_t

    def _set_bg_t(self, value: float) -> None:
        """把 0~1 的进度翻译成一次 QSS 重写。

        Args:
            value: 0 取起始色，1 取目标色。

        Notes:
            **快照安全**：到达终点（``value >= 1``）时写的是 ``tokens`` 令牌
            **原文**（大写十六进制），而不是 :func:`animations.blend_color`
            算出来的小写值。QSS 快照比的是字符串，稳态一旦变成 ``#3eb68f``
            就会被判成实质差异 —— 明明什么都没改。
        """
        self._bg_t = float(value)
        color = (
            self._bg_to
            if self._bg_t >= 1.0
            else animations.blend_color(self._bg_from, self._bg_to, self._bg_t)
        )
        self.setStyleSheet(tokens.pill_qss(_STATUS_PILL_FG, color))

    bgT = pyqtProperty(float, fget=_get_bg_t, fset=_set_bg_t)

    def _current_bg(self) -> str:
        """返回此刻屏幕上真实的底色（可能停在某段渐变的中途）。"""
        if self._bg_t >= 1.0:
            return self._bg_to
        return animations.blend_color(self._bg_from, self._bg_to, self._bg_t)

    def set_status(self, status: str, text: str = None):
        """切换状态文案与底色。

        Args:
            status: ``_STATUS_PILL_BG`` 里的状态键；未知键回落到 ``pending``。
            text: 覆盖显示文案；``None`` 表示按状态键取翻译。

        Notes:
            同色（如 ``pending`` → ``canceled`` 都是灰）不起动画：播一段看不出
            变化的渐变只是白花开销。
        """
        self._status = status
        bg = _STATUS_PILL_BG.get(status, _STATUS_PILL_BG["pending"])
        label = text if text is not None else tr(f"convert.status.{status}")
        self.setText(label)

        prev, self._bg = self._bg, bg
        if prev == bg or not animations.should_animate(self):
            animations.stop(self, b"bgT")
            self._bg_from = self._bg_to = bg
            self._set_bg_t(1.0)
            return

        # 起点取「此刻真实的颜色」而非上一个状态的稳态色：
        # 快速连切（running → done → compress_done）时不会先闪回再重走。
        self._bg_from = self._current_bg()
        self._bg_to = bg
        self._bg_t = 0.0
        animations.animate_value(
            self,
            b"bgT",
            1.0,
            duration=animations.DURATION_MEDIUM,
            curve=animations.CURVE_SMOOTH,
            animate=True,
        )


class FormatPill(QLabel):
    """格式指示胶囊（v0.7.2 Feat5）：显示「.SRC → .TGT」。

    v0.7.3 调整2：底色由中性浅灰改为品牌绿 ``tokens.SUCCESS``，
    文字随之改为近白 ``tokens.SURFACE``，保证对比度可读。
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 修复1：胶囊严格按内部文字定宽，绝不随 UI 宽度拉伸
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            tokens.pill_qss(tokens.SURFACE, tokens.SUCCESS, size=tokens.FONT_CAPTION)
        )
        self.setText(text)


# --------------------------------------------------------------------------
# 队列行
# --------------------------------------------------------------------------
class QueueItemWidget(ThemedCard):
    """单条任务在队列里的可视行：文件名、状态胶囊、进度、操作按钮。

    信号：
        removeRequested(str): 请求移除该任务，参数为 task.id。
        retryRequested(str): 请求重试该任务，参数为 task.id。

    线程约定：仅在 GUI 主线程更新；后台进度经 ConversionManager 的信号
    转到主线程后再调用本控件的 update_* 方法。
    """

    removeRequested = Signal(str)
    retryRequested = Signal(str)

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self._task = task
        # v0.8.21 E1：实时统计片段（速度 / 剩余时间），由 set_stats 刷新。
        self._stats_text = ""
        # 转换 / 压缩两阶段各自的百分比，都只存在视图里。
        # 刻意**不**回写 task.progress：那是 core 侧的字段，GUI 线程反向写它
        # 会和调度线程抢同一个属性，且真正的真相源始终在 manager 那边。
        self._run_pct = int(getattr(task, "progress", 0) or 0)
        self._comp_pct = 0
        # 当前**展示**中的状态。不能直接读 task.status：压缩态刻意不写回
        # task.status（见 set_status），只看 task 会把压缩中误判成 done。
        self._disp_status = task.status
        self._build()

    def _build(self):
        vb = build_row_layout(self)

        # Adj1：左侧徽标显示文件后缀（矩形 + 居中文字），取代类别图标
        src_ext = Path(self._task.input_path).suffix.upper().lstrip(".")
        self.iconLbl = ext_badge(src_ext, self)
        self.nameLbl = MarqueeName(self)
        self.nameLbl.set_text(_basename(self._task.input_path))
        self.nameLbl.setObjectName("queueName")
        # Feat5：格式指示胶囊 .SRC → .TGT（如 .JPG → .PNG）
        tgt = (self._task.target_format or "").upper()
        self.fmtPill = FormatPill(f".{src_ext} → .{tgt}")
        self.pill = StatusPill(self._task.status)
        vb.addLayout(build_row_header(self.iconLbl, self.nameLbl, self.fmtPill, self.pill))

        self.prog = ProgressBar()
        vb.addWidget(self.prog)

        # 大小对比文本（独立成行，自动换行； 黑字 + 百分比绿/红）
        self.detailLbl = build_detail_label()

        self.retryBtn = icon_btn(FIF.SYNC, self)
        self.retryBtn.clicked.connect(lambda: self.retryRequested.emit(self._task.id))
        self.copyBtn = icon_btn(FIF.COPY, self)
        self.copyBtn.clicked.connect(self._copy_path)
        self.delBtn = icon_btn(FIF.DELETE, self)
        self.delBtn.clicked.connect(lambda: self.removeRequested.emit(self._task.id))

        # Feat6：大小对比文本与操作按钮同行右对齐，按钮水平对齐文本行
        bottom = QHBoxLayout()
        bottom.addWidget(self.detailLbl, 1)
        bottom.addWidget(self.retryBtn)
        bottom.addWidget(self.copyBtn)
        bottom.addWidget(self.delBtn)
        vb.addLayout(bottom)

        self.set_status(self._task.status, self._task.error)
        self.set_progress(self._task.progress)

    def set_progress(self, pct: int):
        self.prog.set_value(pct)
        self._run_pct = int(pct)
        # 进度条动了，详情行里的「45%   ·   1.53x   ·   剩余 02:31」也得跟着动。
        # 改造前详情行的百分比只在 set_status 里写一次，之后一路停在旧值，
        # 进度条和文字长期对不上（真进度接上后这个偏差尤其扎眼）。
        if self._disp_status == "running":
            self._refresh_live()

    # -- 详情行 ---------------------------------------------------------
    def _convert_sizes(self) -> tuple[int, int]:
        """(转换前, 转换后)。压缩已跑过时，转换后大小存在 pre_compress_size。"""
        before = int(getattr(self._task, "src_size", 0) or 0)
        pre = int(getattr(self._task, "pre_compress_size", 0) or 0)
        after = pre or int(getattr(self._task, "dst_size", 0) or 0)
        return before, after

    def _compress_sizes(self) -> tuple[int, int]:
        """(压缩前, 压缩后)。"""
        pre = int(getattr(self._task, "pre_compress_size", 0) or 0)
        post = int(getattr(self._task, "dst_size", 0) or 0)
        return pre, post

    def _detail_text(self) -> str:
        """组合「转换前后」与「压缩前后」两段对比（v0.7.0 Bug 2，v0.7.1 换行）。

        转换阶段： ``转换 1.2 MB → 900.0 KB (-25%)``
        压缩之后：两段各占一行，百分比绿/红着色。
        """
        parts: list[str] = []

        cb, ca = self._convert_sizes()
        conv = format_size_compare(cb, ca)
        if conv:
            parts.append(f"{tr('convert.label.convert')} {conv}")

        if getattr(self._task, "compress_done", False):
            pb, pa = self._compress_sizes()
            comp = format_size_compare(pb, pa)
            if comp:
                parts.append(f"{tr('convert.label.compress')} {comp}")

        return "<br>".join(parts)

    # -- 实时统计（v0.8.21 E1） -------------------------------------------
    def _refresh_live(self) -> None:
        """按当前展示状态重绘详情行的「运行态」内容。

        Notes:
            只处理 ``running`` / ``compressing`` 两个进行态。终态（done /
            failed / canceled）的详情行由 :meth:`set_status` 独占，这里不碰，
            否则速度片段会在任务结束后残留在大小对比旁边。

            v0.8.24 Bug#1：压缩阶段改为**两行**——第一行是转换大小对比
            （``转换 57.3 MB → 103.0 MB（+80%）``），第二行是压缩实时数据
            （``压缩 5% · 2.02x · 剩余01:42``）。此前用 ``·`` 把两段拼成
            一行，拥挤且「22%」容易误读成转换阶段的进度。
        """
        if self._disp_status == "running":
            self.detailLbl.setText(join_detail(f"{self._run_pct}%", self._stats_text))
        elif self._disp_status == "compressing":
            conv_line = self._detail_text()  # 第一行：转换大小对比（可能为空）
            comp_line = join_detail(
                f"{tr('convert.label.compress')} {self._comp_pct}%", self._stats_text
            )
            self.detailLbl.setText("<br>".join(p for p in (conv_line, comp_line) if p))

    def set_stats(self, snap) -> None:
        """接收 ffmpeg 实时统计（速度 / 剩余时间）并刷新详情行。

        Args:
            snap: :class:`~core.ffmpeg_progress.ProgressSnapshot`；``None``
                表示清空统计片段。

        Notes:
            转换阶段与压缩阶段共用这一个入口 —— 两条执行链都把快照发到
            ``ConversionManager.task_stats``，行控件只按「当前是哪个阶段」
            决定把片段拼到哪一行，不需要区分来源。
        """
        self._stats_text = format_stats(snap)
        self._refresh_live()

    # -- 状态 -----------------------------------------------------------
    def set_status(self, status: str, error: str = ""):
        """更新状态胶囊与详情行。

        v0.7.0：压缩相关状态也走这里统一上色，不再用 setStyleSheet 打补丁。
        任务已进入压缩阶段后，再收到 ``done`` 不会把胶囊刷回绿色。
        """
        compressed = getattr(self._task, "compress_done", False)
        if status == "done" and compressed:
            status = "compress_done"

        if status not in ("compressing", "compress_done"):
            self._task.status = status
        self._disp_status = status
        self.pill.set_status(status)
        self.prog.set_error(status == "failed")
        self.retryBtn.setVisible(status in ("failed", "canceled"))

        # 离开进行态就把速度/剩余时间清掉，避免「已完成」旁边挂着一个
        # 早已过期的 ETA。转换→压缩的切换不清（那是换阶段不是结束）。
        if status not in ("running", "compressing"):
            self._stats_text = ""
        if status in ("pending", "running"):
            # 重试会把 core 侧的 progress 归零，这里跟着同步，否则详情行会先
            # 闪一下上一轮残留的「100%」再被首个进度回调刷掉。
            self._run_pct = int(getattr(self._task, "progress", 0) or 0)

        if status in ("done", "compress_done"):
            self.detailLbl.setText(self._detail_text())
        elif status == "failed":
            self.detailLbl.setText((error or tr("convert.status.failed"))[:80])
        elif status in ("running", "compressing"):
            self._refresh_live()
        else:
            self.detailLbl.setText("")

    def set_compress(self, pct: int, done: bool = False):
        """压缩阶段：进度 + 黄「压缩中」/ 蓝「压缩完成」胶囊。"""
        self.prog.set_value(pct)
        self.prog.set_error(False)
        self.pill.set_status("compress_done" if done else "compressing")
        self._comp_pct = int(pct)
        if done:
            self._disp_status = "compress_done"
            self._stats_text = ""
            self.detailLbl.setText(self._detail_text())
        else:
            self._disp_status = "compressing"
            self._refresh_live()

    def restore_after_compress(self):
        """回到「已完成」态（压缩过的任务保持蓝色压缩完成）。"""
        self.set_status("done")

    def retranslate(self):
        self.pill.set_status(self._task.status)
        self.set_status(self._task.status, self._task.error)

    def _copy_path(self):
        # Bug5：只复制输出文件所在文件夹路径，而非完整文件路径
        folder = str(Path(self._task.output_path).parent)
        QApplication.clipboard().setText(folder)


# --------------------------------------------------------------------------
# 队列列表
# --------------------------------------------------------------------------
class QueueListWidget(QueueListBase):
    """任务队列列表容器，按 task.id 管理所有 QueueItemWidget。

    典型用法::

        queue.add_item(task)
        queue.update_progress(task.id, 42)

    信号：
        removeRequested(str) / retryRequested(str): 由子行透传上来。
    """

    removeRequested = Signal(str)
    retryRequested = Signal(str)

    _empty_key = "convert.queue.empty"
    # 三个队列里只有转换队列历史上给空态标签设了 objectName，保留原样
    # （它会进 QSS 快照的键名，统一掉反而会改动快照）。
    _empty_object_name = "queueEmpty"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.statTotal, self.statRun, self.statErr = self._statLabels

    def add_item(self, task):
        """追加一条任务行；同 id 重复调用会被忽略。

        Args:
            task: core.models.Task 实例。
        """
        if task.id in self.items:
            return
        w = QueueItemWidget(task)
        w.removeRequested.connect(self.removeRequested)
        w.retryRequested.connect(self.retryRequested)
        # 与改造前一致：入队不刷统计，由随后的 sync() 兜底
        self._attach_row(task.id, w)

    def update_progress(self, task_id: str, pct: int):
        """转换队列的历史方法名，语义同基类的 ``set_progress``。"""
        self.set_progress(task_id, pct)

    def update_stats(self, task_id: str, snap):
        """接 ``ConversionManager.task_stats``，语义同基类的 ``set_stats``。"""
        self.set_stats(task_id, snap)

    def update_status(self, task_id: str, status: str, error: str = ""):
        w = self.items.get(task_id)
        if w:
            w.set_status(status, error)
            if status in ("done", "failed", "canceled"):
                self._update_stats()

    def update_compress(self, task_id: str, pct: int, done: bool = False):
        """v0.6.0：更新压缩阶段 UI（蓝色进度条）。"""
        w = self.items.get(task_id)
        if w:
            w.set_compress(pct, done)

    def restore_compress(self, task_id: str):
        """压缩完成后恢复绿色已完成状态。"""
        w = self.items.get(task_id)
        if w:
            w.restore_after_compress()

    def update_compress_start(self, task_id: str):
        """压缩开始 → 黄色「压缩中」。"""
        w = self.items.get(task_id)
        if w:
            w.set_compress(0, done=False)

    def update_compress_waiting(self, task_id: str):
        """排队等待压缩 → 灰色「等待中」。"""
        w = self.items.get(task_id)
        if w:
            w.pill.set_status("pending")
            w.prog.set_value(0)

    def update_compress_done(self, task_id: str):
        """压缩完成 → 蓝色「压缩完成」+ 双段大小对比。"""
        w = self.items.get(task_id)
        if w:
            w.set_compress(100, done=True)
            self._update_stats()

    def sync(self, tasks):
        ids = {t.id for t in tasks}
        for tid in list(self.items):
            if tid not in ids:
                self.remove_item(tid)
        for t in tasks:
            if t.id not in self.items:
                self.add_item(t)
        self._update_stats()

    def _update_stats(self):
        """重写基类占位：统计总数 / 进行中 / 失败（基于行的 _task.status）。"""
        counts = _counts_from(self.items)
        self.statTotal.setText(tr("convert.queue.total", n=counts.get("total", 0)))
        self.statRun.setText(tr("convert.queue.running", n=counts.get("running", 0)))
        self.statErr.setText(tr("convert.queue.failed", n=counts.get("failed", 0)))


# --------------------------------------------------------------------------
# ScrollAutoFollow — 队列滚动自动跟随当前任务（ Adj2）
# --------------------------------------------------------------------------
class ScrollAutoFollow(QObject):
    """队列滚动自动跟随当前正在处理的任务。

    - ``set_active(True)`` 进入跟随模式（任务进行中）；``set_active(False)`` 退出。
    - 任务开始处理时调用 ``ensure(item_widget)`` 将条目滚入可视区域。
    - 用户手动拖动/滚轮/键盘操作滚动条时暂停跟随，停止操作后 3s 自动恢复。

    暂停判定通过事件过滤器捕获视口滚轮/键盘事件，以及滚动条滑块的
    ``sliderPressed``/``sliderMoved``（仅拖动时触发，程序化 ``ensureWidgetVisible``
    走 ``setValue`` 不会触发，故不会自我死锁）。
    """

    RESUME_DELAY_MS = 3000

    def __init__(self, scroll_area: QScrollArea, parent=None):
        super().__init__(parent or scroll_area)
        self._scroll = scroll_area
        self._active = False
        self._user_paused = False
        self._resume_timer = QTimer(self)
        self._resume_timer.setSingleShot(True)
        self._resume_timer.timeout.connect(self._on_resume)

        sb = scroll_area.verticalScrollBar()
        sb.sliderPressed.connect(self._on_user_scroll)
        sb.sliderMoved.connect(self._on_user_scroll)

        scroll_area.installEventFilter(self)
        scroll_area.viewport().installEventFilter(self)

    def set_active(self, active: bool):
        """进入/退出跟随模式。"""
        self._active = bool(active)
        self._resume_timer.stop()
        if active:
            # 新任务开始即重置用户暂停状态，重新跟随
            self._user_paused = False

    def ensure(self, widget: QWidget):
        """把 widget 滚入可视区域（仅在跟随模式且用户未手动接管时）。"""
        if not self._active or self._user_paused or widget is None:
            return
        self._scroll.ensureWidgetVisible(widget, 10, 10)

    def _on_user_scroll(self, *_):
        if not self._active or self._user_paused:
            return
        self._user_paused = True
        self._resume_timer.start(self.RESUME_DELAY_MS)

    def _on_resume(self):
        self._user_paused = False

    def eventFilter(self, obj, event):
        if self._active and not self._user_paused:
            t = event.type()
            if t == QEvent.Type.Wheel or t == QEvent.Type.KeyPress:
                self._on_user_scroll()
        return super().eventFilter(obj, event)


def _basename(path: str) -> str:
    from pathlib import Path

    return Path(path).name


# --------------------------------------------------------------------------
# MarqueeName — 文件名显示控件（ 修复 1）
# --------------------------------------------------------------------------
class MarqueeName(QWidget):
    """文件名显示：横向滚动轮流显示超长文本。

    v0.7.6  固定 ``max_chars`` 汉字宽，超出则横向滚动。
    v0.7.8  改为自适应宽度：由外层布局决定窗口宽，``resizeEvent``
            实时更新，长文则滚动，短文则静止。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._offset = 0
        self._char_w = 1
        self._text_w = 0
        self._window_w = 0
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)
        # 水平方向填充可用空间，竖向固定高度防止纵向撑大
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        try:
            self.setFont(BodyLabel().font())
        except Exception:
            log.debug("设置队列字体失败，忽略")  # 静默原因：字体度量失败可回退默认，不影响渲染
        self._fm = self.fontMetrics()
        self._char_w = max(1, self._fm.horizontalAdvance("中"))
        self.setFixedHeight(self._fm.height() + 2)

    def set_text(self, text: str) -> None:
        self._text = text or ""
        self._fm = self.fontMetrics()
        self._char_w = max(1, self._fm.horizontalAdvance("中"))
        self._text_w = self._fm.horizontalAdvance(self._text)
        self._restart_timer()
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._window_w = self.width()
        self._restart_timer()

    def _restart_timer(self) -> None:
        if self._text_w > self._window_w > 0:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self._offset = 0

    def _tick(self) -> None:
        if self._text_w <= self._window_w:
            self._timer.stop()
            self._offset = 0
            self.update()
            return
        self._offset -= 2
        gap = self._char_w * 3
        if -self._offset >= self._text_w + gap:
            self._offset = self._window_w
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(self.rect())
        painter.setPen(QPen(QColor(text_strong())))
        fm = self._fm
        base_y = (self.height() + fm.ascent() - fm.descent()) // 2
        painter.drawText(int(self._offset), int(base_y), self._text)
        painter.end()


def _counts_from(items: dict) -> dict:
    out = {"total": len(items), "running": 0, "failed": 0}
    for w in items.values():
        st = w._task.status
        if st == "running":
            out["running"] += 1
        elif st == "failed":
            out["failed"] += 1
    return out
