"""动效系统单点真源（v0.8.0 B2）。

职责边界：
- 做：定义时长/缓动令牌、批量阈值与全局开关；提供四个可复用动效原语
  （透明度渐变、位置进场、数值平滑、颜色过渡）；保证每个动画对象都有 parent。
- 不做：不认识任何具体控件，不写死任何业务语义（哪里该动、动多少由调用方决定）。

依赖：仅 PyQt6；被依赖：gui/base、gui/queue_widget、gui/drop_area、gui/theme。

与 ``gui/tokens.py`` 对等：tokens 收口「长什么样」，本模块收口「怎么动」。
两者都是单点真源，禁止在别处再写魔法数字（250ms、OutCubic 之类）。

铁律一 —— 动画必须有 parent
    v0.7.19 / v0.7.24 各崩过一次，根因都是 ``QPropertyAnimation`` 建成局部变量，
    函数返回即被 Python GC 回收，表现为「动画偶尔不播 / 播一半没了」这种最难复现
    的偶发 bug。本模块所有工厂函数一律 ``QPropertyAnimation(target, prop, parent)``
    显式挂 parent，并把对象登记到 target 自己的槽表上；调用方即使丢掉返回值，
    动画也不会消失。

铁律二 —— 透明度特效与 setMask 互斥
    ``QGraphicsOpacityEffect`` 会把控件转成离屏合成，与 ``setMask(QRegion(...))``
    的裁剪路径叠加后在部分平台出现黑边/缺角。因此 :func:`fade` 只能用在**没有
    setMask 路径**的控件上（本项目里圆角卡片走 ``paintEvent`` + ``drawRoundedRect``，
    无 setMask，安全；``drop_area.iconBadge`` 有 setMask，禁止使用）。
    需要给有 mask 的控件做过渡，改用 :func:`blend_color` 驱动 QSS 颜色。

全局开关 —— ``ANIMATIONS_ENABLED``
    关掉后所有原语**不是跳过调用**，而是退化成「同步设置终值」的无动画路径，
    终态与开着时动画播完后完全一致。可由环境变量
    ``MOMENTSHIFT_ANIMATIONS=0/false/off`` 覆盖（低配机器 / 门禁复跑用）。
    刻意**不**接入设置界面：它是验收与排障手段，不是产品功能。
"""

from __future__ import annotations

import os

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    pyqtProperty,
)
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QGraphicsOpacityEffect

# =============================================================================
# 时长档位（毫秒）
# =============================================================================
# 三档的划分依据是「用户在等谁」：
#   FAST   —— 用户的手正在动（悬停、拖拽经过），动效只是跟随，必须无感；
#             超过 ~150ms 就会被感知成「界面反应慢」。
#   MEDIUM —— 系统在回应一次状态变化（任务转成功/失败），要被看见但不能拖节奏。
#   SLOW   —— 有元素进出画面（队列行增删），需要足够时间让眼睛跟上位移，
#             否则只会看到「列表突然抖了一下」。
# CARD 档是既有事实的迁移位，不趁机改手感。
DURATION_FAST = 120  # 悬停高亮 / 拖拽区态切换 / 进度条追值
DURATION_MEDIUM = 200  # 状态胶囊颜色过渡
DURATION_SLOW = 320  # 队列行进场 / 退场
DURATION_CARD = 250  # 折叠卡片展开收起（迁移自 CollapsibleCard._ANIM_DURATION，值不变）

# =============================================================================
# 缓动曲线（按语义命名，不按数学名命名）
# =============================================================================
#
# 这里只放**当前有接入点**的曲线。曾经写过一条 CURVE_EMPHASIS（OutBack，
# 轻微过冲回弹）留作「以后可能用得上」，随后删掉了：没有消费方的令牌不会保持
# 正确，只会在下次改动时被顺手改坏，或者让人以为某处正在用它。
# 需要新曲线时，跟着它的第一个接入点一起加。
CURVE_IN = QEasingCurve.Type.OutCubic  # 进场：先快后慢，落位稳（同既有事实）
CURVE_OUT = QEasingCurve.Type.InCubic  # 退场：先慢后快，抽离干脆
CURVE_SMOOTH = QEasingCurve.Type.InOutQuad  # 通用平滑：颜色/数值过渡两端对称

# =============================================================================
# 几何常量
# =============================================================================
# 进场时内容向上位移的像素数。取 10 是因为队列行上下内边距是 12：
# 位移量必须小于内边距，否则「借」出来的空间不够，底部内边距会被压成负值。
SLIDE_OFFSET = 10

# =============================================================================
# 批量阈值
# =============================================================================
# 一次连续操作里最多允许多少条队列行播动效。超出的部分整批走无动画路径。
# 为什么需要：用户可以一次拖入几百个文件，逐条建 QGraphicsOpacityEffect 并起
# 动画会把主线程压满，动效本身反而变成卡顿源。
# 为什么取 24：单屏最多也就看得见十来行，24 已经覆盖「肉眼能同时看到的全部行」，
# 再多播了也没人看；同时把并发的离屏合成控制在廉价数量级。
QUEUE_ANIM_BATCH_LIMIT = 24

# 判定「同一次连续操作」的静默窗口（毫秒）。两次入队/移除间隔小于它就算同一批，
# 预算继续累计；超过它说明用户停手了，预算重置。
QUEUE_BURST_WINDOW_MS = 60

# 进度条补间的最小步长（百分点）。小于它的增量直接写值，不起动画。
# 为什么要这条：后端进度回调本身就密（约 10Hz），1~2 个百分点的小步进肉眼看已经
# 是连续的，再给每一步都建一个动画对象纯属浪费；补间真正的价值在于「憋了半天
# 一次跳 20%」那种粗颗粒回调。取 4 是因为 6px 高、约 300px 宽的进度条上，
# 4 个百分点约等于 12px —— 正好是「能看出是跳过去的」起点。
PROGRESS_SMOOTH_MIN_STEP = 4

# =============================================================================
# 全局开关
# =============================================================================
_ENV_RAW = os.environ.get("MOMENTSHIFT_ANIMATIONS", "1").strip().lower()
ANIMATIONS_ENABLED = _ENV_RAW not in ("0", "false", "off", "no")

# 动画对象登记表挂在 target 自己身上的属性名。
# 刻意**不**用模块级 ``{id(target): anim}`` 字典：控件销毁后 id 会被新对象复用，
# 拿着旧记录去 stop() 一个已析构的 C++ 对象会抛 RuntimeError。挂在 target 上
# 则天然与 target 同生共死。
_SLOT_ATTR = "_ms_anim_slots"
_SLIDE_ATTR = "_ms_slide_driver"


def set_animations_enabled(value: bool) -> None:
    """进程内切换全局动效开关。

    Args:
        value: ``True`` 开启，``False`` 关闭。

    Notes:
        环境变量只在 import 期读一次，本函数供测试与排障在运行时覆盖。
    """
    global ANIMATIONS_ENABLED
    ANIMATIONS_ENABLED = bool(value)


def animations_enabled() -> bool:
    """返回当前全局动效开关状态。"""
    return ANIMATIONS_ENABLED


def should_animate(widget=None) -> bool:
    """综合判定此刻是否值得播动效。

    Args:
        widget: 动效目标控件；``None`` 表示只看全局开关。

    Returns:
        全局开关打开**且**控件当前真的可见时为 ``True``。

    Notes:
        为什么把「可见」也算进来：看不见的动效只有开销没有价值 —— 用户切到别的
        标签页时队列仍在增删行，离屏构造的控件（快照/冒烟/单测）更是从头到尾
        没有 ``show()``。用这一条守住，所有离屏门禁自动走无动画路径，
        动效对门禁结果的影响恒为零。
    """
    if not ANIMATIONS_ENABLED:
        return False
    if widget is None:
        return True
    try:
        return bool(widget.isVisible())
    except RuntimeError:
        # 控件的 C++ 侧已析构（Python 壳还在），当然不该再播
        return False


# =============================================================================
# 动画对象登记（打断上一段同键动画）
# =============================================================================
def _slots(target) -> dict:
    """取（必要时创建）挂在 target 上的动画槽表。

    Args:
        target: 任意 Python 侧可写属性的 QObject。

    Returns:
        ``{属性名: 动画对象}``；target 不允许写属性时返回一次性空表
        （退化为「不打断旧动画」，不影响正确性）。
    """
    slots = getattr(target, _SLOT_ATTR, None)
    if isinstance(slots, dict):
        return slots
    slots = {}
    try:
        setattr(target, _SLOT_ATTR, slots)
    except AttributeError:
        return {}
    return slots


def _occupy(target, prop: bytes, anim) -> None:
    """占用某个属性槽：先停掉该槽上的旧动画，再登记新的。

    Args:
        target: 动画目标。
        prop: 属性名。
        anim: 新动画；``None`` 表示只清空该槽。

    Notes:
        Qt 只在动画**跑到终点**时才发 ``finished``，中途 ``stop()`` 不会发；
        因此被打断的那段动画的收尾回调不会误触发。

        ``stop()`` 同时会触发 ``DeleteWhenStopped``（见 :func:`_start`）把旧动画
        对象销毁；此后槽表里那条记录就是个悬空的 Python 壳，再碰它会抛
        ``RuntimeError``，故这里统一吞掉。
    """
    slots = _slots(target)
    prev = slots.get(prop)
    if prev is not None:
        try:
            prev.stop()
        except RuntimeError:
            pass
    if anim is None:
        slots.pop(prop, None)
    else:
        slots[prop] = anim


def _start(anim: QPropertyAnimation) -> QPropertyAnimation:
    """启动动画，并要求 Qt 在它停下时自行销毁。

    Args:
        anim: 已配置好起止值的动画对象。

    Returns:
        传入的同一个对象（方便链式返回）。

    Notes:
        为什么必须 ``DeleteWhenStopped``：铁律一要求把 parent 设成 target，于是
        动画成了 target 的 QObject 子对象，**会一直活到 target 析构**。进度条
        这种一秒能触发十几次的高频点，一次长任务下来能在单行上堆出上万个动画
        子对象 —— 不崩，但内存和 ``QObject::children()`` 遍历都会慢慢烂掉。
        加上这条策略后：parent 保证「播放期间不会被 Python GC 提前回收」，
        DeleteWhenStopped 保证「播完/被打断就即刻释放」，两者互补不冲突。
    """
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


def stop(target, prop: bytes) -> None:
    """立即停掉 target 上某个属性的动画，当前值原地冻结。

    Args:
        target: 动画目标。
        prop: 属性名（如 ``b"barValue"``）。

    Notes:
        用于「任务已经结束了，进度条不许再往上爬」这类必须硬切的场合。
    """
    _occupy(target, prop, None)


# =============================================================================
# 原语一：数值平滑
# =============================================================================
def animate_value(
    target,
    prop: bytes,
    end,
    *,
    duration: int = DURATION_MEDIUM,
    curve: QEasingCurve.Type = CURVE_SMOOTH,
    animate: bool | None = None,
) -> QPropertyAnimation | None:
    """把一个数值型 Qt 属性平滑过渡到终值。

    Args:
        target: 持有该属性的 QObject（属性须是 ``pyqtProperty``）。
        prop: 属性名，字节串，如 ``b"hoverT"``。
        end: 终值。
        duration: 时长（毫秒），取自本模块的 ``DURATION_*``。
        curve: 缓动曲线，取自本模块的 ``CURVE_*``。
        animate: 是否播动画；``None`` 表示跟随全局开关。

    Returns:
        已启动的动画对象；走无动画路径时返回 ``None``。

    Notes:
        无动画路径**直接把终值写进属性**（而非跳过调用），因此调用方无需再为
        「关掉动效」写第二套赋值代码，终态天然一致。
        同一 ``(target, prop)`` 上的旧动画会先被打断，快速连点不会两段动画打架。

        **限制**：``prop`` 必须是 Python 侧用 ``pyqtProperty`` 定义的属性。
        Qt 的 C++ 属性（``maximumHeight`` 之类）同名的是**方法**不是值，
        ``getattr`` 拿到的会是绑定方法、``setattr`` 则会在实例上盖出一个同名
        属性把方法遮掉。要动那类属性请用本模块的专用封装
        （如 :func:`collapse_height`）。
    """
    if animate is None:
        animate = ANIMATIONS_ENABLED
    name = prop.decode()
    _occupy(target, prop, None)
    if not animate:
        setattr(target, name, end)
        return None

    anim = QPropertyAnimation(target, prop, target)  # 铁律一：parent=target
    anim.setDuration(duration)
    anim.setStartValue(getattr(target, name))
    anim.setEndValue(end)
    anim.setEasingCurve(curve)
    anim.finished.connect(lambda: _occupy(target, prop, None))
    _occupy(target, prop, anim)
    return _start(anim)


# =============================================================================
# 原语二：透明度渐变
# =============================================================================
def current_opacity(target) -> float:
    """读出控件当前的特效不透明度。

    Args:
        target: 目标控件。

    Returns:
        已挂 ``QGraphicsOpacityEffect`` 时返回其 opacity，否则返回 ``1.0``。

    Notes:
        淡出要从「此刻实际的透明度」起步 —— 若上一段淡入被打断在 0.4，
        硬从 1.0 起步会先跳亮一下再淡出，非常刺眼。
    """
    eff = target.graphicsEffect()
    return float(eff.opacity()) if isinstance(eff, QGraphicsOpacityEffect) else 1.0


def _clear_opacity(target) -> None:
    """摘掉透明度特效，恢复控件的原生渲染路径。

    Args:
        target: 目标控件。

    Notes:
        ``QGraphicsOpacityEffect`` 常驻会让控件每次重绘都多走一遍离屏合成。
        淡入结束后终值就是「完全不透明」，等价于「没有特效」，因此直接摘掉，
        避免几百行队列长期背着这份开销。
    """
    if isinstance(target.graphicsEffect(), QGraphicsOpacityEffect):
        target.setGraphicsEffect(None)


def fade(
    target,
    start: float,
    end: float,
    *,
    duration: int = DURATION_SLOW,
    curve: QEasingCurve.Type = CURVE_SMOOTH,
    on_finished=None,
    animate: bool | None = None,
) -> QPropertyAnimation | None:
    """淡入 / 淡出。

    Args:
        target: 目标控件。**必须**是没有 setMask 路径的控件（见模块铁律二）。
        start: 起始不透明度（0~1）。
        end: 结束不透明度（0~1）。
        duration: 时长（毫秒）。
        curve: 缓动曲线。
        on_finished: 动画正常结束后的回调；无动画路径下同步调用。
            典型用途是「淡出播完再 deleteLater」。
        animate: 是否播动画；``None`` 表示跟随全局开关。

    Returns:
        已启动的动画对象；走无动画路径时返回 ``None``。

    Notes:
        终值为完全不透明时会顺手摘掉特效（见 :func:`_clear_opacity`）。
    """
    if animate is None:
        animate = ANIMATIONS_ENABLED

    if not animate:
        if end >= 1.0:
            _clear_opacity(target)
        else:
            eff = target.graphicsEffect()
            if not isinstance(eff, QGraphicsOpacityEffect):
                eff = QGraphicsOpacityEffect(target)
                target.setGraphicsEffect(eff)
            eff.setOpacity(float(end))
        if on_finished is not None:
            on_finished()
        return None

    eff = target.graphicsEffect()
    if not isinstance(eff, QGraphicsOpacityEffect):
        eff = QGraphicsOpacityEffect(target)
        target.setGraphicsEffect(eff)
    eff.setOpacity(float(start))

    anim = QPropertyAnimation(eff, b"opacity", target)  # 铁律一：parent=target
    anim.setDuration(duration)
    anim.setStartValue(float(start))
    anim.setEndValue(float(end))
    anim.setEasingCurve(curve)

    def _done() -> None:
        _occupy(target, b"opacity", None)
        if end >= 1.0:
            _clear_opacity(target)
        if on_finished is not None:
            on_finished()

    anim.finished.connect(_done)
    _occupy(target, b"opacity", anim)
    return _start(anim)


# =============================================================================
# 原语三：位置进场
# =============================================================================
class _MarginSlide(QObject):
    """把 0~1 的进度翻译成「内容整体上移」的布局边距位移。

    为什么不用 ``QPropertyAnimation(widget, b"pos")``：目标控件都躺在
    ``QVBoxLayout`` 里，布局会在下一次 relayout 把 pos 改回去，动画等于白播
    （表现为「动一下又弹回原位」）。改为动**自己的布局边距**：上边距加多少、
    下边距就减多少，控件总高不变 → 不触发父布局重排 → 兄弟行不会跟着抖，
    而内容看上去是从下往上滑到位的。

    Attributes:
        slideT: 0 = 位移最大（起点），1 = 完全归位（终点）。
    """

    def __init__(self, layout, offset: int, parent) -> None:
        """记录布局的原始边距作为归位基准。

        Args:
            layout: 目标控件自己的布局。
            offset: 起点相对终点向下偏移的像素数。
            parent: 驱动对象的 parent（传目标控件，保证同生共死）。
        """
        super().__init__(parent)
        self._layout = layout
        margins = layout.contentsMargins()
        self._base = (margins.left(), margins.top(), margins.right(), margins.bottom())
        # 位移量不能超过下边距，否则「借」不出空间，控件总高会变、引发父布局重排
        self._offset = max(0, min(int(offset), self._base[3]))
        self._t = 1.0

    def _get_t(self) -> float:
        return self._t

    def _set_t(self, value: float) -> None:
        self._t = float(value)
        delta = int(round(self._offset * (1.0 - self._t)))
        left, top, right, bottom = self._base
        self._layout.setContentsMargins(left, top + delta, right, bottom - delta)

    slideT = pyqtProperty(float, fget=_get_t, fset=_set_t)


def slide_in(
    widget,
    *,
    offset: int = SLIDE_OFFSET,
    duration: int = DURATION_SLOW,
    curve: QEasingCurve.Type = CURVE_IN,
    animate: bool | None = None,
) -> QPropertyAnimation | None:
    """让控件内容从下方轻微上移到位（布局安全的进场位移）。

    Args:
        widget: 目标控件；必须自带布局，否则本函数直接返回 ``None``。
        offset: 起点向下偏移的像素数。
        duration: 时长（毫秒）。
        curve: 缓动曲线。
        animate: 是否播动画；``None`` 表示跟随全局开关。

    Returns:
        已启动的动画对象；无布局或走无动画路径时返回 ``None``。

    Notes:
        驱动对象复用并挂在 widget 上：重复调用不会把「已经被改过的边距」
        当成原始基准，避免多次进场把内容越推越下。
    """
    layout = widget.layout()
    if layout is None:
        return None

    driver = getattr(widget, _SLIDE_ATTR, None)
    if not isinstance(driver, _MarginSlide):
        driver = _MarginSlide(layout, offset, widget)
        try:
            setattr(widget, _SLIDE_ATTR, driver)
        except AttributeError:
            pass

    if animate is None:
        animate = ANIMATIONS_ENABLED
    if not animate:
        driver.slideT = 1.0  # 无动画路径：直接归位
        return None

    driver.slideT = 0.0
    return animate_value(driver, b"slideT", 1.0, duration=duration, curve=curve, animate=True)


def collapse_height(
    widget,
    *,
    duration: int = DURATION_SLOW,
    curve: QEasingCurve.Type = CURVE_OUT,
    on_finished=None,
    animate: bool | None = None,
) -> QPropertyAnimation | None:
    """把控件高度平滑收到 0（退场时让上下邻居合拢）。

    Args:
        widget: 目标控件；调用后它的 ``maximumHeight`` 会被钉死，仅用于**即将被
            销毁**的控件。
        duration: 时长（毫秒）。
        curve: 缓动曲线。
        on_finished: 收完后的回调；无动画路径下同步调用。
        animate: 是否播动画；``None`` 表示跟随全局开关。

    Returns:
        已启动的动画对象；走无动画路径时返回 ``None``。

    Notes:
        为什么单独开一个函数而不复用 :func:`animate_value`：``maximumHeight``
        是 Qt 的 C++ 属性，Python 侧同名的是 getter **方法**，走 getattr/setattr
        那条路会把方法遮掉（见 :func:`animate_value` 的限制说明）。

        为什么起点要先钉成 ``widget.height()``：``maximumHeight`` 的默认值是
        16777215，直接从它插到 0，动画的前 98% 都在做肉眼无形的下降，
        用户看到的是「愣了 300ms 然后瞬间没了」。

        为什么退场必须收高度：只淡出不收高度的话，列表会留着一段透明的幽灵
        空隙，等动画播完才「啪」地合上 —— 比干脆不做动效还难看。
    """
    if animate is None:
        animate = ANIMATIONS_ENABLED

    prop = b"maximumHeight"
    _occupy(widget, prop, None)
    if not animate:
        widget.setMaximumHeight(0)
        if on_finished is not None:
            on_finished()
        return None

    widget.setMaximumHeight(max(0, widget.height()))
    anim = QPropertyAnimation(widget, prop, widget)  # 铁律一：parent=target
    anim.setDuration(duration)
    anim.setStartValue(widget.maximumHeight())
    anim.setEndValue(0)
    anim.setEasingCurve(curve)

    def _done() -> None:
        _occupy(widget, prop, None)
        if on_finished is not None:
            on_finished()

    anim.finished.connect(_done)
    _occupy(widget, prop, anim)
    return _start(anim)


# =============================================================================
# 原语四：颜色过渡
# =============================================================================
def blend_color(start: str, end: str, t: float) -> str:
    """在两个颜色之间线性插值，返回可直接写进 QSS 的十六进制串。

    Args:
        start: 起始色（任何 ``QColor`` 认得的写法，通常是 ``tokens`` 里的令牌）。
        end: 结束色。
        t: 插值因子，0 取 start，1 取 end；超出区间会被夹回。

    Returns:
        形如 ``#3eb68f`` 的小写十六进制串。

    Notes:
        为什么颜色过渡不用 ``QPropertyAnimation`` 直接动 QColor：过渡的落点是
        一段 QSS 字符串，必须由调用方决定拼进哪个选择器。这里只提供纯函数，
        由调用方用 :func:`animate_value` 驱动一个 0~1 的因子再来取色。

        **快照安全铁律**：本函数只允许用在过渡途中。稳态（t=0 / t=1）必须写回
        ``tokens`` 里的令牌**原文**，绝不能用这里返回的计算色 —— 令牌是大写
        （``#3EB68F``），``QColor.name()`` 一律小写，直接写回去会让 QSS 快照
        逐条报「实质差异」。
    """
    factor = max(0.0, min(1.0, float(t)))
    c0, c1 = QColor(start), QColor(end)
    r = round(c0.red() + (c1.red() - c0.red()) * factor)
    g = round(c0.green() + (c1.green() - c0.green()) * factor)
    b = round(c0.blue() + (c1.blue() - c0.blue()) * factor)
    return f"#{r:02x}{g:02x}{b:02x}"
