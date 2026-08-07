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

from PyQt6.QtCore import (
    QParallelAnimationGroup,
    QPointF,
    QPropertyAnimation,
    QSize,
    Qt,
    pyqtProperty,
    pyqtSignal,
)
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
class _ArrowToggle(QWidget):
    """自绘折叠箭头按钮（V0.8.20 动画优化）。

    为什么自绘而不是 SVG 图标按钮：widget 无法直接旋转
    （``QGraphicsRotation`` 是 ``QGraphicsTransform``，``setGraphicsEffect``
    不接受），瞬时换图又不够精致。改为自绘 chevron + ``angle`` 属性
    （``pyqtProperty``），由 :func:`animations.animate_value` 平滑旋转。

    V0.8.20 Bug1 修复：**不能继承 QPushButton**。QPushButton 一旦设置了
    样式表就由 ``QStyleSheetStyle`` 接管绘制，自绘 ``paintEvent`` 的内容
    会被吞掉（Qt 官方文档原话："paintEvent() 中的 QPainter 绘图代码不再
    显示"）——表现就是折叠箭头完全消失。改继承 ``QWidget`` 全自绘
    （箭头 + 悬停/按下背景都在 ``paintEvent`` 里画），与 FormatCard 同一
    已被验证可靠的模式；点击信号由鼠标事件自行发出。

    ``angle`` 语义：0° = 箭头向下（卡片收起，提示可展开）；180° = 箭头向上
    （卡片展开，提示可收起）。收起↔展开互为 180° 旋转。
    """

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(30, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._angle = 0.0
        self._hover = False
        self._pressed = False

    def _get_angle(self) -> float:
        return self._angle

    def _set_angle(self, value: float) -> None:
        self._angle = float(value)
        self.update()

    angle = pyqtProperty(float, fget=_get_angle, fset=_set_angle)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        was_pressed = self._pressed
        self._pressed = False
        self.update()
        if (
            was_pressed
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self._pressed = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        # 悬停 / 按下反馈：浅黑底圆角（QSS 与自绘冲突，故在 paintEvent 里画）
        if self._pressed:
            painter.setBrush(QColor(0, 0, 0, 22))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(1, 1, w - 2, h - 2, 6, 6)
        elif self._hover:
            painter.setBrush(QColor(0, 0, 0, 12))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(1, 1, w - 2, h - 2, 6, 6)
        # 箭头（chevron：两条线段汇聚成 V 形）
        painter.setPen(
            QPen(
                QColor(tokens.TEXT_STRONG),
                2,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.translate(w / 2.0, h / 2.0)
        painter.rotate(self._angle)
        # V0.8.20 Bug1 修复：PyQt6 的 QPainter.drawLine 四参重载只接受 int，
        # 传 float 会抛 TypeError（异常被 Qt 吞掉 → 箭头消失，只剩 hover 方块）。
        # 改用 QPointF 两点重载。
        painter.drawLine(QPointF(-5.0, -2.0), QPointF(0.0, 3.0))
        painter.drawLine(QPointF(5.0, -2.0), QPointF(0.0, 3.0))


# 公开别名：折叠箭头的对外入口（v0.8.23 引擎卡也用它替换旧 PushButton 样式）。
ArrowToggle = _ArrowToggle


class CollapsibleCard(ThemedCard):
    # v0.8.0 B3 接入点 6：时长/曲线迁到 gui/animations 收口，**数值不变**
    # （250ms + OutCubic，与改造前逐帧一致）。保留这个类属性名是因为它是既有
    # 对外可读的事实，只是不再在这里写死魔法数字。
    _ANIM_DURATION = animations.DURATION_CARD
    def __init__(self, title: str = "", subtitle: str = "", parent=None, collapsed: bool = False):
        super().__init__(parent)
        self._collapsed = collapsed
        self._anim = None
        self._content_height = 0
        self._bar_h_fixed = None  # V0.8.20 Bug2：收起动画期间钉死的标题栏高度

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

        self._toggleBtn = _ArrowToggle(self)
        self._toggleBtn.setFixedSize(30, 30)
        self._toggleBtn.clicked.connect(self.toggle)
        # 初始角度按折叠状态：收起=下拉(0°)，展开=上收(180°)
        self._toggleBtn.angle = 0.0 if collapsed else 180.0
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

        走 ``_apply_collapsed`` 会启动一段 250ms 的高度动画，起点是控件默认
        的 16777215 —— 于是卡片首次显示时会先整个铺开再收拢，表现为
        「展开 → 收起」的闪烁。构造期直接置位，不跑动画。
        """
        self._body.setMinimumHeight(0)
        self._body.setMaximumHeight(0)
        self._body.setVisible(False)

    def _spin_toggle_icon(self) -> None:
        """折叠箭头 180° 平滑旋转切换（V0.8.20 动画优化）。

        收起↔展开互为 180° 旋转：把箭头平滑旋转到目标角度（0° 下拉 /
        180° 上收），比瞬时换图更有物理感。走 :func:`animations.animate_value`
        —— 无动画路径（全局关闭 / 控件不可见，如离屏门禁）直接写终值，
        终态与动画播完后完全一致，不破坏 B3x 稳态指纹。
        """
        animations.animate_value(
            self._toggleBtn,
            b"angle",
            0.0 if self._collapsed else 180.0,
            duration=animations.DURATION_CARD,
            curve=animations.CURVE_SMOOTH,
            animate=animations.should_animate(self._toggleBtn),
        )

    def _anim_target(self, target_h: int):
        if self._anim is not None:
            # 中途停掉上一段动画，避免它的 finished 回调污染新状态
            self._anim.stop()
            self._anim.deleteLater()
            self._anim = None

        # V0.8.26 Bug#1（三修）：折叠动画改走 **minimumHeight + maximumHeight
        # 双属性并行**（QParallelAnimationGroup）。
        #
        # 为什么不用 fixedHeight（V0.8.25）：``setFixedHeight`` 只是便捷方法，
        # **不是 Q_PROPERTY** —— ``QPropertyAnimation(b"fixedHeight")`` 找不到
        # 可写属性，动画直接失效，表现为「顿一下然后瞬间收起/展开」。
        #
        # 为什么 maximumHeight 单属性会抖：它只是「上限」，布局仍按 body 的
        # sizeHint 算实际高度。动画期间若 body 内换行文本/控件随视口宽度变化
        # 重排，sizeHint 每帧都在变，父布局也跟着每帧重排——表现为整卡上下抖。
        #
        # 双属性并行的语义：**每帧把 minimumHeight 与 maximumHeight 都钉到
        # 同一个动画值**，等价于 fixedHeight 的「实打实高度」——body 被强制
        # 钉死，内部内容被**裁剪**而非重排，父布局每帧只重排一次；且两条
        # 都是有效 Q_PROPERTY，动画能正常驱动。这是 Qt 社区（djc_helper /
        # superqt 等）做平滑折叠的标准做法。
        if target_h <= 0:
            real_target = 0
            h = self._body.height()
            # 收起起点 = 当前实际高度（maximumHeight 可能是 16777215，
            # 直接用它会先停在高位区间造成「顿一下」；用实际高度起播才顺）
            cur = h if h > 0 else 0
        else:
            if self._content_height > 0:
                real_target = self._content_height
            else:
                real_target = self._body.sizeHint().height()
                if real_target <= 0:
                    real_target = 200
            cur = 0  # 展开起点恒为 0，直接长开
        self._content_height = real_target if real_target > 0 else self._content_height
        # 钉住当前值作为动画起点（收起=当前高，展开=0）
        self._body.setMinimumHeight(int(cur))
        self._body.setMaximumHeight(int(cur))
        self._body.show()
        # 动画期间抑制父级滚动区滚动条，切断「高度→滚动条→宽度→换行→
        # 高度」的反馈环（双属性钉死下宽度不再影响 body 高度，但仍防滚动条
        # 出现/消失带来的视口宽度抖动）。
        self._suppress_scrollbars(True)

        # 并行驱动 min/max 高度：两条曲线一致，body 全程被钉在动画值上。
        curve = animations.CURVE_OUT if target_h <= 0 else animations.CURVE_IN
        group = QParallelAnimationGroup(self)
        for prop in (b"minimumHeight", b"maximumHeight"):
            a = QPropertyAnimation(self._body, prop, group)
            a.setDuration(self._ANIM_DURATION)
            a.setStartValue(int(cur))
            a.setEndValue(int(real_target))
            a.setEasingCurve(curve)
            group.addAnimation(a)
        group.finished.connect(self._on_anim_finished)
        self._anim = group
        self._anim.start()

    def _suppress_scrollbars(self, suppress: bool) -> None:
        """动画期间钉住最近父级滚动区的滚动条策略（v0.8.24 Bug#2）。

        只处理**最近一个** QScrollArea：卡片往往直接躺在滚动容器里，这就是
        每次逐帧改高度时被牵连重排的那个。``suppress=False`` 时恢复动画前
        记录的原始策略；找不到滚动区则什么都不做。
        """
        if suppress:
            # 若上一次动画被 stop() 打断（finished 未触发），先归还旧策略，
            # 避免「记录的是 AlwaysOff」把原始策略覆盖丢。
            self._restore_scrollbars()
            area = self._find_scroll_area()
            if area is None:
                self._scroll_area = None
                self._scroll_policies = None
                return
            self._scroll_area = area
            self._scroll_policies = (
                area.verticalScrollBarPolicy(),
                area.horizontalScrollBarPolicy(),
            )
            area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        else:
            self._restore_scrollbars()

    def _find_scroll_area(self):
        from PyQt6.QtWidgets import QScrollArea  # noqa: PLC0415 - 局部导入避免顶层拖重

        p = self.parentWidget()
        while p is not None:
            if isinstance(p, QScrollArea):
                return p
            p = p.parentWidget()
        return None

    def _restore_scrollbars(self) -> None:
        area = getattr(self, "_scroll_area", None)
        policies = getattr(self, "_scroll_policies", None)
        self._scroll_area = None
        self._scroll_policies = None
        if area is not None and policies is not None:
            try:
                area.setVerticalScrollBarPolicy(policies[0])
                area.setHorizontalScrollBarPolicy(policies[1])
            except RuntimeError:
                pass  # 静默原因：滚动区可能已随界面销毁

    def _on_anim_finished(self):
        """按结束时的实际状态收尾，避免快速连点造成状态错位。"""
        # V0.8.20 Bug2：解除收起动画期间对标题栏高度的钉死，恢复自由布局。
        # （展开分支同样会经过这里，两种状态下解除都安全。）
        if self._bar_h_fixed is not None:
            self._bar.setMinimumHeight(0)
            self._bar.setMaximumHeight(16777215)
            self._bar_h_fixed = None
        # V0.8.24 Bug#2：动画结束恢复父级滚动条策略。
        self._suppress_scrollbars(False)
        if self._collapsed:
            self._body.setMinimumHeight(0)
            self._body.setMaximumHeight(0)
            self._body.setVisible(False)
        else:
            # V0.8.26：双属性动画结束后解除 min 钉死、max 恢复自由，
            # 布局立刻按 sizeHint 落到正确值（同 maximumHeight 习惯）。
            self._body.setMinimumHeight(0)
            self._body.setMaximumHeight(16777215)

    def _apply_collapsed(self):
        # Bug：必须同步 _collapsed 标志，否则 _on_anim_finished 在展开
        # 动画结束后会读到残留的 True 而把卡片重新收起（"展开→收起"闪烁）。
        self._collapsed = True
        h = self._body.height()
        if h > 0:
            self._content_height = h
        # V0.8.20 Bug2：收起动画期间把标题栏高度钉死。收起时 body 高度逐帧
        # 收缩，若父级布局/滚动区随内容高度重排，或 Windows DPI 缩放下标题
        # 垂直居中产生亚像素波动，标题文字会上下抖动。钉死后标题栏几何在
        # 动画期间绝对不变，动画结束由 _on_anim_finished 恢复。
        self._bar_h_fixed = self._bar.height()
        self._bar.setFixedHeight(self._bar_h_fixed)
        self._anim_target(0)
        self._spin_toggle_icon()

    def _apply_expanded(self):
        self._collapsed = False
        self._body.setVisible(True)
        self._anim_target(16777215)
        self._spin_toggle_icon()

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
            # V0.8.26：双属性语义下解除固定，恢复由内容决定高度。
            self._body.setMinimumHeight(0)
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
