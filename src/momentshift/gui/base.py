"""基础滚动界面 —— 所有功能标签页的共享基类与共享构建器。

职责边界：
- 做：统一标题头、滚动容器、折叠卡片注册与守卫、文件/目录选择对话框封装、
  三个队列页共用的行/列表骨架构建器、映射型下拉框的公开读写 API。
- 不做：不承载任何具体功能页的业务逻辑。

依赖：core/qt_compat、gui/theme、gui/tokens、i18n/translator；被依赖：全部功能页
（about/compress/convert/quick_launch/setting/upscale）与 gui/queue_widget。

为什么文件对话框收在基类：各页面直接调 QFileDialog 时曾出现 parent=None 与
DontUseNativeDialog 混用，导致弹窗跑到屏幕外或样式不一致；收口后只有一处实现。

提供：
- 统一标题头（标题 + 副标题 + accent 下划线）
- 折叠卡片注册与"至少保留一个展开"守卫
- 可折叠卡片的自动折叠/展开策略
- 共享 UI 组件构建器（``InterfaceBase._make_*``）
- 队列骨架构建器（v0.8.0 B4，模块级 5 个 ``build_*``）：
  ``build_row_layout`` / ``build_row_header`` / ``build_detail_label`` /
  ``build_stats_bar`` / ``build_list_body``
- 队列列表容器基类（v0.8.0 B4）：``QueueListBase``；v0.8.0 B3 在此接入行进出动效
- 映射型下拉框公开 API（v0.8.0 ODD-07，4 个）：
  ``bind_combo_mapping`` / ``combo_mapping`` / ``combo_value`` /
  ``select_combo_value``
"""

import os
import time
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QScrollArea
from qfluentwidgets import (
    CaptionLabel,
    ComboBox,
    ScrollArea,
    TitleLabel,
)

from ..core.qt_compat import QVBoxLayout, QWidget
from ..i18n.translator import tr
from . import animations, tokens
from .theme import (
    WINDOW_BG,
    CollapsibleCard,
    ThemedCard,
    apply_text,
    scrollbar_qss,
)

# ===========================================================================
# 映射型下拉框的公开 API（v0.8.0 ODD-07）
# ===========================================================================
# 背景：项目里的 ComboBox 需要「显示文案 -> 逻辑值」的映射，而 qfluentwidgets 的
# ComboBox 没有这个概念，于是历史实现直接往控件对象上挂了一个 ``_mapping`` 属性，
# 再在 advanced_panel / upscale_interface 等**别的模块**里直接读 ``combo._mapping``。
# 这是典型的跨模块私有属性穿透：属性名一改、或哪天换掉控件库，所有读取点都会
# 静默失效（PyQt 在槽函数里吞异常，界面表现为「下拉框选了没反应」，极难定位）。
# 现在把「挂」和「读」都收口成下面三个公开函数，属性名成为本模块的实现细节。
_MAPPING_ATTR = "_mapping"


def bind_combo_mapping(combo, mapping) -> None:
    """把「显示文案 -> 逻辑值」映射绑定到下拉框上。

    Args:
        combo: 目标下拉框。
        mapping: ``[(显示文案, 逻辑值), ...]`` 或等价的可转 dict 对象。
    """
    setattr(combo, _MAPPING_ATTR, dict(mapping))


def combo_mapping(combo) -> dict:
    """取下拉框上绑定的完整「显示文案 -> 逻辑值」映射。

    Args:
        combo: 目标下拉框。

    Returns:
        映射的**浅拷贝**；未绑定过时返回空 dict。返回拷贝是刻意的：
        调用方（如 quick_dialogs 裁剪候选项）会就地增删，不能让它改到
        控件真实持有的那一份；确实要改绑请显式再调 ``bind_combo_mapping``。
    """
    return dict(getattr(combo, _MAPPING_ATTR, {}) or {})


def combo_value(combo) -> str:
    """取下拉框当前选中项对应的逻辑值。

    Args:
        combo: 目标下拉框。

    Returns:
        映射命中的逻辑值；未绑定过映射或文案不在映射里时，回退为显示文案本身
        （与改造前 ``combo._mapping.get(t, t)`` 的兜底行为完全一致）。
    """
    text = combo.currentText()
    return getattr(combo, _MAPPING_ATTR, {}).get(text, text)


def select_combo_value(combo, value) -> bool:
    """按**逻辑值**选中下拉框中的对应项。

    Args:
        combo: 目标下拉框（须已 ``bind_combo_mapping``）。
        value: 想选中的逻辑值。

    Returns:
        是否命中并完成选中。未命中时不改变当前选择。

    Notes:
        按显示文案 ``findText`` 定位而不是拿映射的下标去 ``setCurrentIndex``：
        映射一旦存在重复显示文案，``dict()`` 会折叠掉一项，下标就和控件里的
        实际行号对不上了。按文案找永远对得上。
    """
    for disp, val in getattr(combo, _MAPPING_ATTR, {}).items():
        if val == value:
            idx = combo.findText(disp)
            if idx >= 0:
                combo.setCurrentIndex(idx)
                return True
    return False


# ===========================================================================
# 队列骨架构建器（v0.8.0 B4 DUP）
# ===========================================================================
# 转换 / 压缩 / 放大三个队列的「行卡片」和「列表容器」在视觉上本来就要求完全一致，
# 但历史上是三份各自演化的复制代码 —— 于是出现过「只在压缩队列修好、另外两个还是
# 老样子」的偏差（如 detailLbl 少了 objectName、少了自动换行）。
# 下面 5 个构建器把三份共同的骨架收成一份，差异部分（中间挂件、操作按钮、状态语义）
# 留给各自的子类声明。
_ROW_MARGINS = (14, 12, 14, 12)
_ROW_SPACING = 8


def build_row_layout(card) -> QVBoxLayout:
    """建立队列行卡片的外层纵向布局（统一内边距与行距）。

    Args:
        card: 承载布局的卡片控件。

    Returns:
        已挂到 ``card`` 上的 ``QVBoxLayout``。
    """
    vb = QVBoxLayout(card)
    vb.setContentsMargins(*_ROW_MARGINS)
    vb.setSpacing(_ROW_SPACING)
    return vb


def build_row_header(icon_widget, name_widget, *trailing) -> QHBoxLayout:
    """建立队列行的顶部横向条：后缀徽标 + 文件名（占满剩余宽） + 若干右侧挂件。

    Args:
        icon_widget: 左侧后缀徽标。
        name_widget: 中间文件名控件，拿到唯一的伸缩因子。
        *trailing: 依次追加到右侧的挂件（格式胶囊 / 耗时标签 / 状态胶囊等），
            ``None`` 会被跳过，方便调用方写 ``build_row_header(a, b, maybe, pill)``。

    Returns:
        构建好的 ``QHBoxLayout``（尚未加入任何父布局）。
    """
    top = QHBoxLayout()
    top.addWidget(icon_widget)
    top.addWidget(name_widget, 1)
    for w in trailing:
        if w is not None:
            top.addWidget(w)
    return top


def build_detail_label() -> CaptionLabel:
    """建立队列行底部的「大小对比 / 错误详情」文本标签。

    Returns:
        已设好 objectName、自动换行与黑字透明底样式的 ``CaptionLabel``。

    Notes:
        三处历史实现里只有放大队列漏了 ``objectName`` 与 ``setWordWrap``，
        导致长错误信息在放大队列里被截断而在另外两个队列里能换行。
        统一到这里之后不会再出现这种单点偏差。
        ``queueStatus`` 目前不被任何 QSS 选择器引用，仅作调试标识。
    """
    lbl = CaptionLabel()
    lbl.setObjectName("queueStatus")
    lbl.setWordWrap(True)
    apply_text(lbl, tokens.TEXT_BLACK, transparent=True)
    return lbl


def build_stats_bar(count: int = 3) -> tuple[QWidget, list[CaptionLabel]]:
    """建立队列顶部的统计栏（总数 / 进行中 / 失败 之类的并排小字）。

    每个统计项由「6px 语义色圆点 + 数字文案」组成，圆点颜色与队列行的
    状态胶囊同源（等待灰 / 运行蓝 / 失败红……），扫一眼即可对上；
    颜色由各队列的 ``_update_stats`` 经 :meth:`QueueListBase._set_stat_dots` 设置。

    Args:
        count: 统计项个数。

    Returns:
        ``(统计栏容器, [统计标签, ...])``；文案由调用方自行填。
        圆点标签列表挂在 ``容器.dots`` 上。
    """
    bar = QWidget()
    hb = QHBoxLayout(bar)
    hb.setContentsMargins(2, 0, 2, 0)
    hb.setSpacing(14)
    labels: list[CaptionLabel] = []
    dots: list[QLabel] = []
    for _ in range(count):
        cell = QWidget()
        cell.setStyleSheet("background: transparent;")
        cb = QHBoxLayout(cell)
        cb.setContentsMargins(0, 0, 0, 0)
        cb.setSpacing(6)
        dot = QLabel()
        dot.setFixedSize(6, 6)
        dot.setStyleSheet(tokens.dot_qss(tokens.PENDING, radius=3))
        cb.addWidget(dot)
        dots.append(dot)
        lbl = CaptionLabel()
        # v0.8.10 Bug4：必须 transparent=True——QLabel 设 color 不给透明背景时
        # Qt 会铺默认白底，表现为「共 0 项 完成 0 失败 0」文字后一块 #FFFFFF
        apply_text(lbl, tokens.TEXT_BLACK, weight=600, transparent=True)
        cb.addWidget(lbl)
        hb.addWidget(cell)
        labels.append(lbl)
    hb.addStretch(1)
    bar.dots = dots
    return bar, labels


class EmptyState(QWidget):
    """队列空态组件：淡底圆形图标 + 标题 + 引导文案，水平居中。

    v0.9 UI 重构恢复空态展示（此前「队列为空」文案被产品隐藏，列表只剩一片
    空白，首次使用者得不到任何指引）。图标用 FluentIcon 渲染进品牌淡底圆，
    不引入任何图片资源；文案走 ``queue.empty.*``，四种队列共用。
    """

    def __init__(self, icon=None, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 12, 0, 10)
        vb.setSpacing(8)
        vb.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.iconBadge = QLabel(self)
        self.iconBadge.setFixedSize(52, 52)
        self.iconBadge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.iconBadge.setStyleSheet(
            f"background: {tokens.ACCENT_SOFT}; border-radius: 26px;"
        )
        self._icon = icon
        vb.addWidget(self.iconBadge, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.titleLabel = CaptionLabel()
        apply_text(self.titleLabel, tokens.TEXT_SECONDARY, size=tokens.FONT_BODY,
                   weight=600, transparent=True)
        self.titleLabel.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        vb.addWidget(self.titleLabel)

        self.hintLabel = CaptionLabel()
        apply_text(self.hintLabel, tokens.TEXT_MUTED, size=tokens.FONT_CAPTION,
                   transparent=True)
        self.hintLabel.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        vb.addWidget(self.hintLabel)

        if icon is not None:
            self.setIcon(icon)
        # v0.9.1 修复：把自身 sizeHint 钉成最小高度。父布局空间不足时
        # QVBoxLayout 会把无最小高度的标签压缩/叠绘到图标上（ASR 转写队列
        # 空态「队列为空」叠在麦克风图标上的根因）；钉死后父布局只能长高。
        self.setMinimumHeight(self.sizeHint().height())

    def setIcon(self, icon) -> None:
        """更换空态图标并按当前主题色重绘。"""
        self._icon = icon
        if icon is None:
            return
        self.iconBadge.setPixmap(
            icon.icon(QColor(tokens.ACCENT_HOVER)).pixmap(24, 24)
        )

    def set_text(self, title: str, hint: str) -> None:
        self.titleLabel.setText(title)
        self.hintLabel.setText(hint)
        self.setToolTip("")


def build_list_body(empty_text: str) -> tuple[QWidget, QVBoxLayout, EmptyState]:
    """建立队列的行容器与空态组件。

    Args:
        empty_text: 空态标题文案（兼容旧签名；标题/引导由 ``EmptyState.set_text``
            在 ``retranslate`` 时统一回填）。

    Returns:
        ``(行容器, 行容器布局, 空态组件)``。行容器布局末尾已放好弹簧，
        新行应插到 ``count() - 1`` 位置，否则会被顶到弹簧下方。
        空态组件插在弹簧之前、行列表之后，由 ``QueueListBase._refresh_empty``
        按列表是否为空切换显隐。

    Notes:
        v0.9 UI 重构：空态从「恒隐藏的 CaptionLabel」升级为可见的
        :class:`EmptyState`。objectName 沿用转换队列历史上的 ``queueEmpty``
        约定由调用方设置（进 QSS 快照键名）。
    """
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addStretch(1)

    empty = EmptyState()
    empty.setObjectName("queueEmpty")
    empty.set_text(empty_text, "")
    empty.setVisible(False)
    layout.insertWidget(layout.count() - 1, empty)
    return holder, layout, empty


class QueueListBase(QWidget):
    """转换 / 压缩 / 放大三个队列列表容器的共同骨架。

    负责统计栏 + 行容器 + 空态提示的搭建，以及「按 key 增删行」这套三处
    逐字重复的管理逻辑。各队列真正的差异（行控件类型、入队签名、统计口径）
    留给子类。

    子类约定：
    - 类属性 ``_empty_icon``：空态图标（qfluentwidgets ``FluentIcon`` 成员），
      ``None`` 表示不显示图标。
    - 必须实现 ``_update_stats()``：把文案写进 ``self._statLabels``，
      并可用 :meth:`_set_stat_dots` 给统计圆点上语义色。
    - 自行实现 ``add_item(...)``（签名各页不同），内部调 ``_attach_row``。

    动效（v0.8.0 B3）：行的进出是本项目频次最高的视觉变化，也是唯一会让用户
    看到「列表突然抖一下」的地方，因此把动效接在这里 —— 三个队列一次性都有。
    进场淡入 + 轻微上移，退场淡出后再真正销毁；受批量预算与可见性双重约束，
    具体见 :meth:`_anim_budget_allows` 与 :func:`gui.animations.should_animate`。

    Attributes:
        items: ``{行 key: 行控件}``，key 是 task.id 或压缩/放大的 item_id。
        statsBar / _statLabels: 顶部统计栏容器与其中的标签。
        _statDots: 统计栏的语义色圆点（与 ``_statLabels`` 一一对应）。
        listWidget / listLayout: 行容器及其布局（末尾恒有一个弹簧）。
        emptyHint: 空态组件（:class:`EmptyState`，随列表是否为空显隐）。
    """

    _empty_icon = None
    _stat_count: int = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: dict = {}
        # 行动效的批量预算（见 _anim_budget_allows）
        self._anim_budget: int = 0
        self._anim_last_ms: float = 0.0

        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(8)

        self.statsBar, self._statLabels = build_stats_bar(self._stat_count)
        self._statDots: list[QLabel] = list(getattr(self.statsBar, "dots", []))
        vb.addWidget(self.statsBar)

        self.listWidget, self.listLayout, self.emptyHint = build_list_body(
            tr("queue.empty.title")
        )
        if self._empty_icon is not None:
            self.emptyHint.setIcon(self._empty_icon)
        vb.addWidget(self.listWidget, 1)
        self._refresh_empty()

    def _set_stat_dots(self, colors: list[str]) -> None:
        """给统计栏圆点上语义色，与 ``_statLabels`` 一一对应。

        Args:
            colors: 颜色令牌列表；超出的圆点保持原样。
        """
        for dot, color in zip(self._statDots, colors):
            dot.setStyleSheet(tokens.dot_qss(color, radius=3))

    # -- 行动效预算 -----------------------------------------------------
    def _anim_budget_allows(self) -> bool:
        """本次行增删是否还分得到动效预算。

        Returns:
            当前这一批里已播动效的行数未超过
            :data:`gui.animations.QUEUE_ANIM_BATCH_LIMIT` 时为 ``True``。

        Notes:
            「一批」按静默窗口界定：与上一次增删的间隔小于
            :data:`gui.animations.QUEUE_BURST_WINDOW_MS` 就算同一批，预算继续累计；
            超过则说明用户停手了，预算重置。

            为什么必须有这道闸：用户可以一次拖入几百个文件，那是一个几乎同步的
            循环，逐条起动画会把主线程压满 —— 动效反而成了卡顿源。加闸后
            「肉眼能同时看到的那些行」照常有动效，后面的直接落位，
            用户既看不出差别，也不会卡。
        """
        now = time.monotonic() * 1000.0
        if now - self._anim_last_ms > animations.QUEUE_BURST_WINDOW_MS:
            self._anim_budget = 0
        self._anim_last_ms = now
        self._anim_budget += 1
        return self._anim_budget <= animations.QUEUE_ANIM_BATCH_LIMIT

    # -- 行管理 ---------------------------------------------------------
    def _attach_row(self, key: str, widget) -> None:
        """把已建好的行控件挂进列表。

        Args:
            key: 行的唯一标识（task.id / item_id）。
            widget: 行控件。

        Notes:
            **刻意不在这里刷统计**：转换队列历史上入队时不刷（靠随后的
            ``sync()`` 兜底），压缩/放大则会刷。把它留给调用方决定，才能保证
            B4 的结构重构对三个队列都是零行为变化。

            动效（B3）：淡入 + 轻微上移。行卡片是 ``ThemedCard``，圆角由
            ``paintEvent`` 里的 ``drawRoundedRect`` 画出、全程没有 setMask，
            所以用 ``QGraphicsOpacityEffect`` 淡入是安全的（见 animations 铁律二）。
        """
        self.items[key] = widget
        # 插到末尾的 stretch 之前，否则新行会被顶到弹簧下方
        self.listLayout.insertWidget(self.listLayout.count() - 1, widget)
        self._refresh_empty()

        if not animations.should_animate(self) or not self._anim_budget_allows():
            return
        animations.slide_in(
            widget, duration=animations.DURATION_SLOW, curve=animations.CURVE_IN, animate=True
        )
        animations.fade(
            widget,
            0.0,
            1.0,
            duration=animations.DURATION_SLOW,
            curve=animations.CURVE_IN,
            animate=True,
        )

    def _refresh_empty(self) -> None:
        """空态↔内容切换：列表为空时展示引导组件，有任务时隐藏。

        Notes:
            v0.9 UI 重构恢复空态（此前产品上刻意隐藏）。首次使用者面对
            一片空白得不到任何指引；现在空态给出图标 + 「拖入文件」提示，
            与输入卡的拖拽区形成动作呼应。
        """
        self.emptyHint.setVisible(not self.items)

    def remove_item(self, key: str) -> None:
        """移除一行；key 不存在时静默忽略。

        Args:
            key: 行的唯一标识。

        Notes:
            **二次移除安全**：先从 ``items`` 里 pop 再处置控件，所以同一个 key
            无论被点几次删除、或删除动画播到一半又被「清空队列」扫一遍，
            第二次都拿不到控件，直接静默返回。淡出期间的行还挂在布局上，
            但已经不在 ``items`` 里，:meth:`clear` 也不会重复销毁它。

            淡出期间额外 ``blockSignals(True)``：控件此刻仍在界面上、按钮还能点，
            不掐掉信号的话用户可以对一个正在消失的行再发一次 removeRequested，
            上游会拿着已经不存在的 key 去查表。
        """
        w = self.items.pop(key, None)
        if w is not None:
            self._dispose_row(w)
        self._refresh_empty()
        self._update_stats()

    def _dispose_row(self, widget) -> None:
        """销毁一行：能播动效就淡出后再删，否则立即删。

        Args:
            widget: 已从 ``items`` 摘除的行控件。

        Notes:
            ``deleteLater`` 必须等淡出播完才调用，否则控件先没了、动画自然也没了，
            用户看到的还是「啪一下消失」。动画 parent 是控件本身，控件若被别的
            路径提前销毁，动画随之销毁且不会再发 ``finished``，回调不会打在
            野指针上。
        """
        if not animations.should_animate(self) or not self._anim_budget_allows():
            widget.deleteLater()
            return
        widget.blockSignals(True)
        # 淡出与合拢同时进行：只淡出不收高度，列表会留着一段透明的幽灵空隙，
        # 播完才「啪」地合上；两者同步走完，看到的才是「这一行被收走了」。
        animations.collapse_height(
            widget, duration=animations.DURATION_SLOW, curve=animations.CURVE_OUT, animate=True
        )
        animations.fade(
            widget,
            animations.current_opacity(widget),
            0.0,
            duration=animations.DURATION_SLOW,
            curve=animations.CURVE_OUT,
            on_finished=widget.deleteLater,
            animate=True,
        )

    def clear(self) -> None:
        """清空所有行。

        Notes:
            刻意**不**给清空加动效：它是一次性批量操作，几十上百行同时淡出既超出
            动效预算，也让「我点了清空但列表还在那儿」持续小半秒，反而像卡住。
            批量操作要的是干脆。
        """
        for w in self.items.values():
            w.deleteLater()
        self.items.clear()
        self._refresh_empty()
        self._update_stats()

    def set_progress(self, key: str, pct: int) -> None:
        """更新某一行的进度；key 不存在时静默忽略。"""
        w = self.items.get(key)
        if w:
            w.set_progress(pct)

    def set_stats(self, key: str, snap) -> None:
        """把 ffmpeg 实时统计（速度 / 剩余时间）转交给对应行（v0.8.21 E1）。

        Args:
            key: 行的唯一标识。
            snap: :class:`~core.ffmpeg_progress.ProgressSnapshot`。

        Notes:
            行控件没实现 ``set_stats`` 就静默跳过 —— 压缩 / 放大队列的行还没
            接真实统计，放在基类里是为了它们接上时不用再改一遍列表层。
        """
        w = self.items.get(key)
        setter = getattr(w, "set_stats", None)
        if setter is not None:
            setter(snap)

    def retranslate(self) -> None:
        """语言切换后回填所有行、空态文案与统计文案。"""
        for w in self.items.values():
            w.retranslate()
        self.emptyHint.set_text(tr("queue.empty.title"), tr("queue.empty.hint"))
        self._update_stats()

    def _update_stats(self) -> None:
        """把统计文案写进 ``self._statLabels``，由子类实现。"""
        raise NotImplementedError


class InterfaceBase(ScrollArea):
    def __init__(self, object_name: str, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setWidgetResizable(True)

        self.view = QWidget()
        self.view.setObjectName(object_name + "View")
        self.setWidget(self.view)

        self.vbox = QVBoxLayout(self.view)
        # v0.9.1 紧凑化：820x760 默认窗口下主页面免滚动，页边距/卡片间距收一档
        self.vbox.setContentsMargins(14, 10, 14, 10)
        self.vbox.setSpacing(10)

        # 标题头：大标题（20px）+ 副标题。旧版的绿色短下划线在视觉上像误置的
        # 链接下划线，UI 重构（2026-09）移除；层级改由字号/字重/留白承担。
        self.header = QWidget()
        # 标题行：标题 + 可选右侧状态
        self._header_row = QHBoxLayout()
        self._header_row.setContentsMargins(0, 0, 0, 0)
        self._header_row.setSpacing(10)
        hb = QVBoxLayout(self.header)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(2)
        self.titleLabel = TitleLabel(title)
        title_font = self.titleLabel.font()
        title_font.setPixelSize(tokens.FONT_TITLE_LG)
        title_font.setWeight(QFont.Weight.DemiBold)
        self.titleLabel.setFont(title_font)
        self._header_row.addWidget(self.titleLabel, 1)
        self._header_row.addStretch()
        hb.addLayout(self._header_row)
        if subtitle:
            self.subLabel = CaptionLabel(subtitle)
            hb.addWidget(self.subLabel)
        self.vbox.addWidget(self.header)

        InterfaceBase.retheme(self)

        self._collapsibles: list = []
        self._collapse_ready = False

    def register_collapsible(self, card) -> None:
        if card not in self._collapsibles:
            self._collapsibles.append(card)
            card.set_toggle_guard(self._can_collapse)

    def _can_collapse(self, card, want_collapse: bool) -> bool:
        if not want_collapse or not self._collapse_ready:
            return True
        expanded = [c for c in self._collapsibles if c.isVisible() and not c.isCollapsed()]
        return len(expanded) > 1

    # 共享 UI 组件构建器
    def _make_card(self, title_key, subtitle_key=None, collapsed=False):
        from ..i18n.translator import tr

        title_text = tr(title_key)
        sub_text = tr(subtitle_key) if subtitle_key else ""
        card = CollapsibleCard(title_text, sub_text, self, collapsed=collapsed)
        self.register_collapsible(card)
        return card, card.body, card.titleLabel

    def _make_scroll(self, min_height: int = 170) -> QScrollArea:
        s = QScrollArea()
        s.setWidgetResizable(True)
        s.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        s.setStyleSheet(f"QScrollArea{{border:none; background:transparent;}} {scrollbar_qss()}")
        s.viewport().setStyleSheet("background:transparent;")
        s.setMinimumHeight(min_height)
        return s

    def _make_combo(self, mapping, current, on_change) -> ComboBox:
        """新建一个「显示文案 -> 逻辑值」映射下拉框，变更时回调逻辑值。"""
        combo = ComboBox()
        for disp, _val in mapping:
            combo.addItem(disp)
        bind_combo_mapping(combo, mapping)
        select_combo_value(combo, current)
        combo.currentTextChanged.connect(lambda _t: on_change(combo_value(combo)))
        return combo

    def _repopulate_combo(self, combo: ComboBox, mapping):
        """换掉下拉框的候选项，尽量保住原来选中的**逻辑值**。"""
        current_val = combo_value(combo)
        combo.blockSignals(True)
        combo.clear()
        bind_combo_mapping(combo, mapping)
        for disp, _val in mapping:
            combo.addItem(disp)
        select_combo_value(combo, current_val)
        combo.blockSignals(False)

    # -------------------------------------------------------------------
    # 文件对话框（ Bug1：资源管理器卡死 / 无法添加文件）
    # -------------------------------------------------------------------
    def _dialog_parent(self):
        """原生文件对话框必须挂到应用主窗口上。

        若传 None，Windows 会把「当前前台窗口」当作 owner —— 用户往往刚从
        资源管理器点过来，于是那个 Explorer 窗口被 EnableWindow(FALSE) 禁用；
        对话框关闭时 Qt 只恢复自己的窗口，Explorer 便永久失去交互。
        同理 Qt 自绘对话框无 parent 时会弹到主窗口背后，表现为「点了没反应」。
        """
        return self.window()

    def _ask_open_files(self, title: str, exts, label: str = "Media") -> list:
        """弹出原生多选文件对话框，返回路径列表（取消则为空）。"""
        flt = f"{label} (" + " ".join(f"*{e}" for e in sorted(exts)) + ")"
        files, _ = QFileDialog.getOpenFileNames(self._dialog_parent(), title, "", flt)
        return files or []

    def _ask_directory(self, title: str, start: str = "") -> str:
        """弹出原生目录选择对话框，返回路径（取消则为空串）。"""
        return QFileDialog.getExistingDirectory(self._dialog_parent(), title, start or "") or ""

    def _expand_paths(self, paths, valid_exts):
        out = []
        for p in paths:
            if os.path.isdir(p):
                for root, _, files in os.walk(p):
                    for f in files:
                        fp = os.path.join(root, f)
                        if Path(fp).suffix.lower() in valid_exts:
                            out.append(fp)
            elif os.path.isfile(p) and Path(p).suffix.lower() in valid_exts:
                out.append(p)
        seen, uniq = set(), []
        for p in out:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    def retheme(self):
        bg = WINDOW_BG
        oid = self.view.objectName() or "view"
        css = (
            f"#{oid} {{ background-color: {bg.name()}; border: none; }}"
            "QLabel, FluentLabelBase, BodyLabel, CaptionLabel, StrongBodyLabel,"
            " TitleLabel, SubtitleLabel { background-color: transparent; }"
        )
        self.view.setStyleSheet(css)
        if self.viewport():
            self.viewport().setStyleSheet(f"background-color: {bg.name()}; border: none;")
        for card in self.findChildren(ThemedCard):
            card.retheme()

    def retranslate(self, title=None, subtitle=None):
        if title is not None:
            self.titleLabel.setText(title)
        if subtitle is not None and hasattr(self, "subLabel"):
            self.subLabel.setText(subtitle)
