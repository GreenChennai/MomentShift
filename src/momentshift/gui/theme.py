"""MomentShift 设计系统。

职责边界：
- 做：集中定义主题色板与圆角等设计 token，按明暗主题返回具体颜色。
- 不做：不持有任何控件引用；不负责触发重绘（各控件自行实现 retheme）。

依赖：无内部依赖；被依赖：几乎全部 GUI 模块。

为什么全走函数而不是常量：主题可在运行时切换，常量会被固化成启动时的那一套配色。

提供统一的视觉语言：
- 色彩 tokens（窗口背景、组件表面、hover/press、accent、文字）
- ThemedCard — 绘制实心主题感知表面的 CardWidget
- CollapsibleCard — 带动效的折叠卡片
- 共享 UI 构建器（panel/field_row/按钮等）
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QPropertyAnimation, QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    PrimaryPushButton,
    StrongBodyLabel,
    TransparentPushButton,
    TransparentToolButton,
)

from . import animations, tokens

# =============================================================================
# 设计 tokens（仅浅色）—— 全部转发自 gui/tokens.py
#
# 这里只做「语义别名 + QColor 包装」，色值本身一律以 tokens 为准。
# 保留这批旧名字是为了不改动 20 余个调用方；新代码请直接 import tokens。
# =============================================================================

# 窗口 + 内容背景
WINDOW_BG = QColor(tokens.WHITE)
SURFACE = QColor(tokens.SURFACE)  # 卡片/组件表面
SURFACE_HOVER = QColor(tokens.SURFACE_HOVER)  # hover 态
SURFACE_PRESS = QColor(tokens.SURFACE_PRESS)  # press 态

# 文字
TEXT_STRONG = tokens.TEXT_STRONG  # 主文字
TEXT_SECONDARY = tokens.TEXT_SECONDARY  # 次要文字
TEXT_PLACEHOLDER = tokens.TEXT_PLACEHOLDER  # 占位符
TEXT_MUTED = tokens.TEXT_MUTED  # 禁用/弱化（由过灰的 BORDER_HOVER 调深）
TEXT_LINK = tokens.TEXT_LINK  # 链接蓝

# 边框
BORDER_COLOR = tokens.BORDER
BORDER_HOVER = tokens.BORDER_HOVER

# 品牌色
ACCENT = QColor(tokens.ACCENT)  # GitHub 绿
ACCENT_HEX = tokens.ACCENT

# 状态色
COLOR_DANGER = tokens.DANGER
COLOR_SUCCESS = tokens.SUCCESS
DANGER_TEXT = tokens.DANGER_TEXT
SUCCESS_TEXT = tokens.SUCCESS

# 几何
RADIUS = tokens.RADIUS
SPACING = tokens.SPACING
CARD_MARGIN = tokens.CARD_MARGIN

# =============================================================================
# SVG 图标路径
# =============================================================================
_RESOURCES = Path(__file__).parent.parent / "resources" / "icons"


def _icon_path(name: str) -> str:
    return os.fspath(_RESOURCES / name)


ICON_EXPAND = _icon_path("\u4e0b\u62c9.svg")
ICON_COLLAPSE = _icon_path("\u6536\u8d77.svg")

# v0.8.14 品牌图标：多分辨率 ico 供窗口/托盘/任务栏；512 png 供启动屏与关于页缩放。
ICON_APP_ICO = _icon_path("app_logo.ico")
ICON_APP_PNG = _icon_path("app_logo.png")

# QIcon 必须在 QGuiApplication 就绪后才能构造，因此惰性缓存而不是模块级常量。
_APP_ICON_CACHE: QIcon | None = None


def app_icon() -> QIcon:
    """应用主图标（窗口 / 托盘 / 启动屏 / 任务栏统一入口）。"""
    global _APP_ICON_CACHE
    if _APP_ICON_CACHE is None:
        icon = QIcon(ICON_APP_ICO)
        if icon.isNull():  # ico 缺失时退回 png，避免整窗无图标
            icon = QIcon(ICON_APP_PNG)
        _APP_ICON_CACHE = icon
    return _APP_ICON_CACHE


def app_logo_pixmap(size: int) -> QPixmap:
    """按指定边长返回平滑缩放的 Logo 位图（启动屏 / 关于页用）。"""
    pm = QPixmap(ICON_APP_PNG)
    if pm.isNull():
        return pm
    return pm.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )

# =============================================================================
# 颜色访问器
# =============================================================================


def content_bg() -> QColor:
    return WINDOW_BG


def component_bg() -> QColor:
    return SURFACE


def surface() -> QColor:
    return SURFACE


def surface_hover() -> QColor:
    return SURFACE_HOVER


def surface_pressed() -> QColor:
    return SURFACE_PRESS


def accent_color() -> QColor:
    return ACCENT


def accent_name() -> str:
    return ACCENT_HEX


def text_strong() -> str:
    return TEXT_STRONG


def text_secondary() -> str:
    return TEXT_SECONDARY


def placeholder_text() -> str:
    return TEXT_PLACEHOLDER


def text_disabled() -> str:
    return TEXT_MUTED


def muted_text() -> str:
    return TEXT_MUTED


def sub_text() -> str:
    return TEXT_SECONDARY


def hint_text() -> str:
    return TEXT_PLACEHOLDER


def link_color() -> QColor:
    return QColor(TEXT_LINK)


def border_color() -> str:
    return BORDER_COLOR


def border_hover() -> str:
    return BORDER_HOVER


def danger_color() -> QColor:
    return QColor(COLOR_DANGER)


def danger_text() -> str:
    return DANGER_TEXT


def success_color() -> QColor:
    return QColor(COLOR_SUCCESS)


def success_text() -> str:
    return SUCCESS_TEXT


# =============================================================================
# 样式应用器 —— 把「取令牌 → 拼 QSS → setStyleSheet」三步收成一次调用
#
# 改造前全项目散落 131 处 setStyleSheet，其中绝大多数是三种重复到极致的写法：
# 「置透明底」「给标签上色」「把滚动区弄干净」。逐处内联的代价是：
# 改一个语义色要全局搜、漏一处就出现视觉不一致，而且 QLabel 忘了配透明底
# 就会冒出「文字下面一块灰」——项目历史上反复踩过。
# 收成下面三个应用器后，调用方只表达意图，拼串与铁律由这里统一兜住。
# =============================================================================


def apply_transparent(*widgets: QWidget) -> None:
    """把若干控件的自身背景置为透明。

    Args:
        *widgets: 目标控件；``None`` 会被跳过，方便直接喂 ``getattr(...)`` 的结果。
    """
    for w in widgets:
        if w is not None:
            w.setStyleSheet("background: transparent;")


def apply_text(
    widget,
    color: str,
    *,
    size: int | None = None,
    weight: int | None = None,
    transparent: bool = False,
    extra: str = "",
) -> None:
    """给单个文字控件应用配色样式。

    Args:
        widget: 目标控件（通常是 ``QLabel`` / ``CaptionLabel``）。
        color: 文字颜色，直接传令牌或 ``muted_text()`` 这类访问器的返回值。
        size: 字号（px）。
        weight: 字重。
        transparent: 是否追加透明背景。
        extra: 追加在末尾的原样 QSS 声明（如 ``"padding: 24px 0;"``）。

    Notes:
        ``transparent`` 默认 **False**，与改造前逐处内联的写法保持一致。
        B1 的铁律是「只换写法不换颜色」，此处若擅自默认补上透明底，
        会静默改变一批标签的实际渲染，因此哪些标签缺透明底只做记录、不顺手改。
    """
    widget.setStyleSheet(
        tokens.text_qss(color, size=size, weight=weight, transparent=transparent, extra=extra)
    )


def apply_plain_scroll(*areas, radius: int | None = None) -> None:
    """把若干 QScrollArea 置为无边框 + 透明底 + 全局细滚动条。

    Args:
        *areas: 目标滚动区；``None`` 会被跳过。
        radius: 滚动区圆角半径；``None`` 表示不设圆角。
    """
    qss = tokens.scrollarea_qss(radius)
    for area in areas:
        if area is not None:
            area.setStyleSheet(qss)


# =============================================================================
# ThemedCard — 实色表面 + 1px 主题边框
# =============================================================================
class ThemedCard(CardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBorderRadius(RADIUS)
        # v0.8.11 Bug4：选择器增加后代匹配（"ThemedCard QWidget"）—— Qt 的后代
        # combinator 匹配所有层级。直接子选择器 ">" 漏掉孙子（中间隔 QVBoxLayout
        # 的 statsBar/listWidget 等），默认白底穿透到屏幕。
        self.setStyleSheet(
            tokens.transparent_children_qss(
                "ThemedCard > QWidget",
                "ThemedCard QWidget",
                "FluentLabelBase, QLabel",
            )
        )

    def _normalBackgroundColor(self):
        return component_bg()

    def _hoverBackgroundColor(self):
        return SURFACE_HOVER

    def _pressedBackgroundColor(self):
        return SURFACE_PRESS

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(border_color()))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        r = self.borderRadius
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.drawRoundedRect(rect, r, r)

    def retheme(self):
        self.update()


# =============================================================================
# CollapsibleCard — 折叠卡片（带动效）
# =============================================================================
class CollapsibleCard(ThemedCard):
    _ICON_W = _ICON_H = 20
    # v0.8.0 B3 接入点 6：时长/曲线迁到 gui/animations 收口，**数值不变**
    # （250ms + OutCubic，与改造前逐帧一致）。保留这个类属性名是因为它是既有
    # 对外可读的事实，只是不再在这里写死魔法数字。
    _ANIM_DURATION = animations.DURATION_CARD

    def __init__(self, title: str = "", subtitle: str = "", parent=None, collapsed: bool = False):
        super().__init__(parent)
        self._collapsed = collapsed
        self._anim = None
        self._content_height = 0

        self.setStyleSheet(
            tokens.transparent_children_qss(
                # v0.8.11 Bug4：增加后代选择器覆盖孙子（直接子 ">" 漏孙子层白底）
                "CollapsibleCard > QWidget",
                "CollapsibleCard QWidget",
                "QLabel, FluentLabelBase, BodyLabel, CaptionLabel, StrongBodyLabel,"
                " TitleLabel, SubtitleLabel",
            )
        )

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)

        self._bar = QWidget()
        hb = QHBoxLayout(self._bar)
        hb.setContentsMargins(CARD_MARGIN, 10, 6, 10)
        hb.setSpacing(8)

        self.titleLabel = StrongBodyLabel(title)
        hb.addWidget(self.titleLabel, 1)
        hb.addStretch()

        self._toggleBtn = TransparentToolButton(self._toggle_icon(), self)
        self._toggleBtn.setIconSize(QSize(self._ICON_W, self._ICON_H))
        self._toggleBtn.setFixedSize(30, 30)
        self._toggleBtn.clicked.connect(self.toggle)
        hb.addWidget(self._toggleBtn)

        self._outer.addWidget(self._bar)

        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(CARD_MARGIN, 0, CARD_MARGIN, 14)
        self._body_layout.setSpacing(10)

        self._body.setStyleSheet(tokens.input_qss("QLineEdit", tokens.RADIUS_SM))

        self.subtitleLabel = None
        if subtitle:
            self.subtitleLabel = CaptionLabel(subtitle)
            self._body_layout.insertWidget(0, self.subtitleLabel)

        self._outer.addWidget(self._body)

        self._toggle_guard = None
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        if collapsed:
            self._collapse_instant()

    # -- 标题栏公开 API（v0.8.0 ODD-07）---------------------------------
    # 「转换设置」弹窗要把折叠箭头换成一个总开关，改造前的写法是直接摸本类的
    # 两个私有件：``adv_card._toggleBtn.hide()`` 和
    # ``adv_card._bar.layout().insertWidget(2, sw)``。这不但把「标题栏内部长什么样、
    # 开关该插在第几个位置」这种实现细节泄漏到了别的模块，而且那个魔数 2 一旦
    # 标题栏加个控件就会插错位置，且不会报错。下面两个方法把它收成本类的对外契约。

    def hide_toggle(self) -> None:
        """隐藏默认的折叠箭头（调用方需自备其它展开/收起入口）。"""
        self._toggleBtn.hide()

    def add_header_widget(self, widget, *, before_toggle: bool = True) -> None:
        """往标题栏右侧追加一个挂件。

        Args:
            widget: 要放进标题栏的控件。
            before_toggle: True 表示放在折叠箭头**之前**（视觉上更靠左），
                False 表示放到最右侧。

        Notes:
            位置按「折叠箭头当前所在下标」实时算出，不再依赖调用方硬编码下标，
            因此将来标题栏增删控件也不会插错位置。
        """
        layout = self._bar.layout()
        if before_toggle:
            idx = layout.indexOf(self._toggleBtn)
            layout.insertWidget(idx if idx >= 0 else layout.count(), widget)
        else:
            layout.addWidget(widget)

    def _collapse_instant(self):
        """初始化时的即时折叠（v0.7.3 Bug2）。

        走 ``_apply_collapsed`` 会启动一段 250ms 的 maximumHeight 动画，
        起点是控件默认的 16777215 —— 于是卡片首次显示时会先整个铺开再收拢，
        表现为「展开 → 收起」的闪烁。构造期直接置位，不跑动画。
        """
        self._body.setMaximumHeight(0)
        self._body.setVisible(False)
        self._toggleBtn.setIcon(self._toggle_icon())

    def _toggle_icon(self) -> QIcon:
        path = ICON_EXPAND if self._collapsed else ICON_COLLAPSE
        return QIcon(path) if os.path.exists(path) else QIcon()

    def _anim_target(self, target_h: int):
        if self._anim is not None:
            # 中途停掉上一段动画，避免它的 finished 回调污染新状态
            self._anim.stop()
            self._anim.deleteLater()
            self._anim = None
        cur = self._body.maximumHeight()
        real_target = target_h
        if target_h <= 0:
            real_target = 0
        elif target_h == 16777215:
            if self._content_height > 0:
                real_target = self._content_height
            else:
                real_target = self._body.sizeHint().height()
                if real_target <= 0:
                    real_target = 200
        self._content_height = real_target if real_target > 0 else self._content_height
        self._body.show()
        self._anim = QPropertyAnimation(self._body, b"maximumHeight", self)
        self._anim.setDuration(self._ANIM_DURATION)
        self._anim.setStartValue(cur)
        self._anim.setEndValue(real_target)
        self._anim.setEasingCurve(animations.CURVE_IN)
        self._anim.finished.connect(self._on_anim_finished)
        self._anim.start()

    def _on_anim_finished(self):
        """按结束时的实际状态收尾，避免快速连点造成状态错位。"""
        if self._collapsed:
            self._body.setVisible(False)
        else:
            # 解除高度上限，内容后续变化（如切换格式）不会被裁剪
            self._body.setMaximumHeight(16777215)

    def _apply_collapsed(self):
        # Bug：必须同步 _collapsed 标志，否则 _on_anim_finished 在展开
        # 动画结束后会读到残留的 True 而把卡片重新收起（"展开→收起"闪烁）。
        self._collapsed = True
        h = self._body.height()
        if h > 0:
            self._content_height = h
        self._anim_target(0)
        self._toggleBtn.setIcon(self._toggle_icon())

    def _apply_expanded(self):
        self._collapsed = False
        self._body.setVisible(True)
        self._anim_target(16777215)
        self._toggleBtn.setIcon(self._toggle_icon())

    @property
    def body(self) -> QVBoxLayout:
        return self._body_layout

    def set_toggle_guard(self, fn) -> None:
        self._toggle_guard = fn

    def toggle(self):
        self.setCollapsed(not self._collapsed)

    def setCollapsed(self, collapsed: bool):
        if self._collapsed == collapsed:
            return
        if collapsed and self._toggle_guard is not None and not self._toggle_guard(self, True):
            return
        self._collapsed = collapsed
        if collapsed:
            self._apply_collapsed()
        else:
            self._apply_expanded()

    def isCollapsed(self) -> bool:
        return self._collapsed

    def refresh_content_height(self):
        """内容动态变化后调用，展开态下解除 maximumHeight 上限。

        v0.7.3 Bug3：展开动画结束时 maximumHeight 停在当时的内容高度；
        之后若再显示更多控件（例如压缩后端切到「自动选择」，三组参数同时出现），
        布局会被这个陈旧上限压扁 —— 表现为所有条目挤成一团。
        """
        self._content_height = 0
        if not self._collapsed:
            self._body.setMaximumHeight(16777215)


# =========================================================================
# 共享 UI 构建器
# =========================================================================


def section_label(text: str, parent=None):
    lbl = CaptionLabel(text, parent)
    lbl.setObjectName("sectionLabel")
    return lbl


def panel(
    title: str | None = None, subtitle: str | None = None, parent=None, radius: int = RADIUS
) -> tuple[ThemedCard, QVBoxLayout]:
    card = ThemedCard(parent)
    card.setBorderRadius(radius)
    vb = QVBoxLayout(card)
    vb.setContentsMargins(CARD_MARGIN, 14, CARD_MARGIN, 14)
    vb.setSpacing(10)
    if title:
        t = StrongBodyLabel(title)
        t.setObjectName("panelTitle")
        vb.addWidget(t)
    if subtitle:
        s = CaptionLabel(subtitle)
        s.setObjectName("panelSub")
        vb.addWidget(s)
    return card, vb


def field_row(
    label_text: str,
    control,
    parent=None,
    label_width: int = 96,
    label_wrap: bool = False,
) -> QWidget:
    """构造「左标签 + 右控件」的一行。

    Args:
        label_text: 行标签文案。
        control: 右侧控件；传入 ``QLayout`` 时按布局加入（用于多控件组合行）。
        parent: 行控件父对象。
        label_width: 标签固定宽度（px，label_wrap=False 时生效）。
        label_wrap: 标签允许换行完整显示（v0.8.8 Bug4：固定宽度下长文案
            （如「结构化输出（时间戳 + 说话人）」）会被单行截断——开启后
            标签按内容自动换行、高度自适应，右侧控件垂直居中。
    Returns:
        行控件 ``QWidget``；标签引用挂在 ``row.fieldLabel`` 上，
        调用方的 ``retranslateUi`` 可以通过它更新文案（v0.8.1 Bug4-②）。
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QLayout, QSizePolicy

    row = QWidget(parent)
    apply_transparent(row)
    hb = QHBoxLayout(row)
    hb.setContentsMargins(0, 0, 0, 0)
    hb.setSpacing(12)
    lbl = BodyLabel(label_text)
    lbl.setObjectName("fieldLabel")
    if label_wrap:
        # 允许换行完整显示：宽度自适应内容、高度随换行增长
        lbl.setWordWrap(True)
        lbl.setMinimumWidth(0)
        lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
    else:
        lbl.setFixedWidth(label_width)
    hb.addWidget(lbl)
    if isinstance(control, QLayout):
        hb.addLayout(control, 1)
    else:
        hb.addWidget(control, 1)
    row.fieldLabel = lbl
    return row


def primary_btn(text: str, icon=None, parent=None) -> PrimaryPushButton:
    if icon is not None:
        return PrimaryPushButton(text, icon=icon, parent=parent)
    return PrimaryPushButton(text, parent=parent)


def ghost_btn(text: str, icon=None, parent=None) -> TransparentPushButton:
    if icon is not None:
        return TransparentPushButton(text, icon=icon, parent=parent)
    return TransparentPushButton(text, parent=parent)


def icon_btn(icon, parent=None) -> TransparentToolButton:
    """图标按钮。v0.7.3 调整2：全局取消鼠标悬停提示，不再接受 tooltip 参数。"""
    return TransparentToolButton(icon, parent)


def ext_badge(ext: str, parent=None) -> QLabel:
    """文件后缀矩形徽标（v0.7.4 Adj1）。

    用于转换/压缩/放大三个队列的任务卡片左侧，取代原先按文件类别绘制的
    视频/音频/图片图标。样式与「转换设置」弹窗「待处理文件」完全一致：
    品牌绿淡底 + 圆角矩形 + 居中后缀文字。
    """
    ext = (ext or "").upper().lstrip(".")
    if not ext:
        ext = "?"
    lbl = QLabel(ext, parent)
    lbl.setFixedWidth(42)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet(tokens.ext_badge_qss())
    return lbl


def scrollbar_qss() -> str:
    """全局细滚动条样式。实现已迁至 tokens，此处保留旧入口不改调用方。"""
    return tokens.scrollbar_qss()


# =============================================================================
# Monkey-patch: 强制 FluentLabelBase + SwitchButton 标签背景透明
# =============================================================================
def _patch_fluent_label_background():
    from qfluentwidgets.common.style_sheet import setCustomStyleSheet
    from qfluentwidgets.components.widgets.label import FluentLabelBase

    _orig = FluentLabelBase.setTextColor

    def _set_text_color(self, light=QColor(0, 0, 0), dark=QColor(255, 255, 255)):
        _orig(self, light, dark)
        light_qss = (
            f"FluentLabelBase{{"
            f"color:{self.lightColor.name(QColor.NameFormat.HexArgb)};"
            f"background-color:transparent}}"
        )
        dark_qss = (
            f"FluentLabelBase{{"
            f"color:{self.darkColor.name(QColor.NameFormat.HexArgb)};"
            f"background-color:transparent}}"
        )
        setCustomStyleSheet(self, light_qss, dark_qss)

    FluentLabelBase.setTextColor = _set_text_color


def _patch_switch_button_label_background():
    from qfluentwidgets.common.style_sheet import setCustomStyleSheet
    from qfluentwidgets.components.widgets.switch_button import SwitchButton

    _orig = SwitchButton.setTextColor

    def _set_text_color(self, light, dark):
        _orig(self, light, dark)
        light_qss = (
            f"SwitchButton>QLabel{{"
            f"color:{self.lightTextColor.name(QColor.NameFormat.HexArgb)};"
            f"background-color:transparent}}"
        )
        dark_qss = (
            f"SwitchButton>QLabel{{"
            f"color:{self.darkTextColor.name(QColor.NameFormat.HexArgb)};"
            f"background-color:transparent}}"
        )
        setCustomStyleSheet(self.label, light_qss, dark_qss)

    SwitchButton.setTextColor = _set_text_color


def apply_fluent_patches() -> None:
    """安装 qfluentwidgets 兼容补丁。幂等，可安全重复调用。

    Notes:
        改为显式调用（由 ``app_bootstrap.install_fluent_patches`` 触发），
        原先在 import 期自动执行，导致 import 顺序敏感、无法关闭、无法单测，
        且升级 qfluentwidgets 时是首要爆炸点。
    """
    global _patches_applied
    if _patches_applied:
        return
    _patch_fluent_label_background()
    _patch_switch_button_label_background()
    _patches_applied = True


_patches_applied = False
