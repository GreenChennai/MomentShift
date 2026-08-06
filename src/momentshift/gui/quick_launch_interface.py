"""快速调用界面 —— 管理 Windows 右键菜单集成设置。

职责边界：
- 做：展示右键菜单各开关、把开关变更同步到注册表与配置。
- 不做：不直接操作注册表（交给 core/quick_launch）。

依赖：core/config、core/logger、core/quick_launch、gui/base、gui/theme、i18n/translator；被依赖：主窗口按导航页装载。

通过 SettingCard 结构展示：
- 总开关（启用/禁用所有快速调用）
- 绑定右键菜单开关
- 各功能独立开关（转换 / 压缩 / 放大）
- 注册状态指示

所有快捷调用默认关闭，用户手动开启。
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QTimer
from PyQt6.QtGui import QTextDocument
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    SettingCardGroup,
    StrongBodyLabel,
    SwitchSettingCard,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from ..core import quick_launch
from ..core.config import cfg
from ..core.logger import get_logger
from ..i18n.translator import tr
from . import tokens
from .base import InterfaceBase
from .theme import (
    CARD_MARGIN,
    ThemedCard,
    danger_color,
    success_color,
    surface,
)

log = get_logger("quick_launch_iface")

# 通知卡片简介标签左右两侧固定占用的宽度（px）：图标 16 + 三处间距 16×3 +
# 开关按钮 98 + 左边距 16 ≈ 178。fit 时用它把标签宽度约束到卡片实际可用宽度。
_NOTIFY_CARD_FIXED_W = 178
# 简介标签最小宽度：窗口极窄时也不至于把文案挤成十几行。
_NOTIFY_CARD_MIN_LABEL_W = 200


class _NotifyCardFitFilter(QObject):
    """监听通知卡片尺寸变化，窗口宽度变化时重新适配简介高度。

    Args:
        card: 通知卡片（SwitchSettingCard）。
        owner: 拥有者（QuickLaunchInterface），回调其 ``_fit_notify_card``。

    Notes:
        SettingCardGroup 的 ``ExpandLayout`` 只负责把卡片按**当前高度**纵向排列，
        从不主动改卡片高度（v0.8.1 实测根因）。本过滤器只在卡片**宽度**变化时
        重跑适配，避免在高度变化（由 fit 自己引起）时产生回环。
    """

    def __init__(self, card, owner):
        super().__init__(card)
        self._card = card
        self._owner = owner

    def eventFilter(self, obj, event):
        if obj is self._card and event.type() == QEvent.Type.Resize:
            if event.size().width() != event.oldSize().width():
                self._owner._fit_notify_card(self._card)
        return super().eventFilter(obj, event)


class _StatusCard(ThemedCard):
    """注册状态指示卡片：显示当前各功能右键注册情况。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        vb = QVBoxLayout(self)
        vb.setContentsMargins(CARD_MARGIN, 14, CARD_MARGIN, 14)
        vb.setSpacing(8)
        self.titleLbl = StrongBodyLabel(tr("quicklaunch.status.title"))
        vb.addWidget(self.titleLbl)

        self._status_rows: dict[str, tuple] = {}
        tasks = quick_launch.available_tasks()
        task_labels = {
            "convert": tr("nav.convert"),
            "compress": tr("nav.compress"),
            "upscale": tr("nav.upscale"),
        }
        for t in tasks:
            row = QWidget()
            hb = QHBoxLayout(row)
            hb.setContentsMargins(0, 4, 0, 4)
            hb.setSpacing(8)
            # V0.8.19 优化7：与其他页（关于/引擎卡/ASR）一致，用 QLabel 圆点
            # 而非文本「●」字符（避免字体差异导致渲染大小不一）
            dot = QLabel()
            dot.setFixedSize(8, 8)
            label = BodyLabel(task_labels.get(t, t))
            hb.addWidget(dot)
            hb.addWidget(label, 1)
            vb.addWidget(row)
            self._status_rows[t] = (dot, label)

        self.refresh()

    def refresh(self):
        """刷新各功能注册状态指示。"""
        for t in quick_launch.available_tasks():
            registered = quick_launch.is_context_menu_registered(t)
            dot, _label = self._status_rows[t]
            color = success_color() if registered else danger_color()
            dot.setStyleSheet(tokens.dot_qss(color.name(), 4))

    def retranslate(self):
        self.titleLbl.setText(tr("quicklaunch.status.title"))


class QuickLaunchInterface(InterfaceBase):
    """快速调用设置标签页。

    用户在此管理 Windows 右键菜单的快捷调用功能。
    """

    def __init__(self, parent=None):
        super().__init__("QuickLaunch", tr("quicklaunch.title"), tr("quicklaunch.subtitle"), parent)

        # Linux / macOS 无右键菜单机制：只显示说明占位，不渲染注册表相关控件。
        if not quick_launch.supported():
            ph = ThemedCard(self)
            pvb = QVBoxLayout(ph)
            pvb.setContentsMargins(CARD_MARGIN, 16, CARD_MARGIN, 16)
            pvb.setSpacing(6)
            pvb.addWidget(StrongBodyLabel(tr("quicklaunch.unsupported.title")))
            psub = BodyLabel(tr("quicklaunch.unsupported.hint"))
            psub.setWordWrap(True)
            pvb.addWidget(psub)
            self.vbox.addWidget(ph)
            return

        # =====================================================================
        # 总开关组
        # =====================================================================
        self.g_main = SettingCardGroup(tr("quicklaunch.group.main"))
        self.masterCard = SwitchSettingCard(
            FIF.POWER_BUTTON,
            tr("quicklaunch.master"),
            tr("quicklaunch.master.hint"),
            cfg.quickLaunchEnabled,
        )
        self.masterCard.checkedChanged.connect(self._on_master)
        self.bindCard = SwitchSettingCard(
            FIF.LINK,
            tr("quicklaunch.bind"),
            tr("quicklaunch.bind.hint"),
            cfg.quickLaunchBindMenu,
        )
        self.bindCard.checkedChanged.connect(self._on_bind)
        self.g_main.addSettingCard(self.masterCard)
        self.g_main.addSettingCard(self.bindCard)
        self.vbox.addWidget(self.g_main)

        # =====================================================================
        # 各功能独立开关组
        # =====================================================================
        self.g_tasks = SettingCardGroup(tr("quicklaunch.group.tasks"))
        self.convertCard = SwitchSettingCard(
            FIF.HOME,
            tr("nav.convert"),
            tr("quicklaunch.convert.hint"),
            cfg.quickLaunchConvert,
        )
        self.convertCard.checkedChanged.connect(self._on_task)
        self.compressCard = SwitchSettingCard(
            FIF.PHOTO,
            tr("nav.compress"),
            tr("quicklaunch.compress.hint"),
            cfg.quickLaunchCompress,
        )
        self.compressCard.checkedChanged.connect(self._on_task)
        self.upscaleCard = SwitchSettingCard(
            FIF.ZOOM,
            tr("nav.upscale"),
            tr("quicklaunch.upscale.hint"),
            cfg.quickLaunchUpscale,
        )
        self.upscaleCard.checkedChanged.connect(self._on_task)
        self.g_tasks.addSettingCard(self.convertCard)
        self.g_tasks.addSettingCard(self.compressCard)
        self.g_tasks.addSettingCard(self.upscaleCard)
        # 通知开关拆成「开始任务通知」/「完成任务通知」（Windows 弹窗自带声音）
        self.notifyStartCard = SwitchSettingCard(
            FIF.PLAY,
            tr("quicklaunch.notify.start"),
            tr("quicklaunch.notify.start.hint"),
            cfg.quickNotifyStart,
        )
        self.g_tasks.addSettingCard(self.notifyStartCard)
        self.notifyDoneCard = SwitchSettingCard(
            FIF.CHECKBOX,
            tr("quicklaunch.notify.done"),
            tr("quicklaunch.notify.done.hint"),
            cfg.quickNotifyDone,
        )
        self.g_tasks.addSettingCard(self.notifyDoneCard)
        # 简介文本过长 → 自动换行（SettingCard 内容默认不换行）
        # SettingCard 固定高度 70px 会截断换行后的简介 → 解除固定高度自适应
        self._resize_notify_cards()
        self.vbox.addWidget(self.g_tasks)

        # =====================================================================
        # 注册状态显示
        # =====================================================================
        self.statusCard = _StatusCard(self)
        self.vbox.addWidget(self.statusCard)

        # 保存每个组的基础 qss（用于 retheme）
        self._group_qss = {
            self.g_main: self.g_main.styleSheet(),
            self.g_tasks: self.g_tasks.styleSheet(),
        }

        self.vbox.addStretch(1)
        self.retheme()

        # 打开设置页时自动按最新命令格式（%* 无引号）重写注册表，
        # 修复旧版 `"%*"`/`%1` 命令导致右键无文件参数的问题
        from PyQt6.QtCore import QTimer

        QTimer.singleShot(0, self._apply)

    # =========================================================================
    # 开关响应
    # =========================================================================

    def _resize_notify_cards(self) -> None:
        """让两张通知卡片的简介文字能完整换行显示。

        v0.8.0 首次修复（wordWrap + setFixedHeight(16777215) + adjustSize()）没有
        生效，v0.8.1 离屏实测的根因：
        - ``setFixedHeight(16777215)`` 在 Qt 里对最小值是 no-op
          （QWIDGETSIZE_MAX 表示「无约束」，最小值不落盘）；
        - ``SettingCardGroup`` 用的是 ``ExpandLayout``，它**从不改卡片高度**，
          只按当前高度纵向排列（``setGeometry(..., w.height())``），卡片高度
          停在构造时的 70px，简介换行后被上下挤压；
        - ``QLabel.heightForWidth``/``sizeHint`` 对已布局宽度有缓存，返回单行
          高度，任何「按 sizeHint 自适应」的尝试都会被它带偏。

        修复：换行高度用 ``QTextDocument`` 按**实际宽度**独立测量（不受 QLabel
        缓存影响）；把简介标签宽度显式约束到卡片可用宽度；卡片高度**显式**
        ``setFixedHeight``（ExpandLayout 不会自己长高）；宽度变化（窗口缩放）
        由事件过滤器重新适配。语言切换 setContent 后重跑本方法。
        """
        for _card in (self.notifyStartCard, self.notifyDoneCard):
            try:
                _card.contentLabel.setWordWrap(True)
                # 去掉 qfluentwidgets 构造时的固定高度上限（70px 会截断换行）
                _card.setFixedHeight(16777215)
                if not getattr(_card, "_notify_fit_filter", None):
                    _card._notify_fit_filter = _NotifyCardFitFilter(_card, self)
                    _card.installEventFilter(_card._notify_fit_filter)
                QTimer.singleShot(0, lambda c=_card: self._fit_notify_card(c))
            except Exception:
                log.debug("调整卡片高度失败，忽略")  # 静默原因：卡片可能已随界面销毁

    def _fit_notify_card(self, card) -> None:
        """按卡片当前实际宽度重排简介标签，保证换行文案不截断。

        Args:
            card: 通知卡片（SwitchSettingCard）。

        Notes:
            ``QTextDocument`` 按「卡片可用宽度 + 实际字体」测量换行高度，结果
            稳定；随后把高度写进 ``contentLabel.minimumHeight`` 并把卡片高度
            显式设为「标题 + 换行高 + 留白」，让 ``ExpandLayout`` 跟随长高。
        """
        try:
            cl = card.contentLabel
            avail = card.width() - _NOTIFY_CARD_FIXED_W
            width = max(avail, _NOTIFY_CARD_MIN_LABEL_W)
            if width <= 0:
                return
            cl.setFixedWidth(width)
            doc = QTextDocument(cl.text())
            doc.setDefaultFont(cl.font())
            doc.setTextWidth(width)
            wrapped = int(doc.size().height()) + 2
            cl.setMinimumHeight(wrapped)
            total = card.titleLabel.sizeHint().height() + wrapped + 16
            card.setFixedHeight(total)
            card.updateGeometry()
        except RuntimeError:
            pass  # 静默原因：卡片可能已随界面销毁

    def _on_master(self, checked: bool):
        """总开关变更：重新应用整个注册状态。"""
        self._apply()

    def _on_bind(self, checked: bool):
        """右键绑定开关变更。"""
        self._apply()

    def _on_task(self, checked: bool):
        """子功能开关变更。"""
        self._apply()

    def _apply(self):
        """根据当前所有开关状态，批量更新 Windows 注册表。"""
        enabled = cfg.quickLaunchEnabled.value
        bind = cfg.quickLaunchBindMenu.value
        tasks = {
            "convert": cfg.quickLaunchConvert.value,
            "compress": cfg.quickLaunchCompress.value,
            "upscale": cfg.quickLaunchUpscale.value,
        }
        if not bind:
            # 不绑定右键 → 注销全部
            for t in tasks:
                quick_launch.unregister_context_menu(t)
        else:
            quick_launch.apply_all(enabled, tasks)
        self.statusCard.refresh()

    # =========================================================================
    # 主题 / i18n
    # =========================================================================

    def retheme(self):
        super().retheme()
        bg = surface().name()
        for grp, base_qss in self._group_qss.items():
            grp.setStyleSheet(f"{base_qss}\nSettingCardGroup{{ background-color: {bg}; }}")
        self.statusCard.retheme()

    def retranslateUi(self):
        self.retranslate(tr("quicklaunch.title"), tr("quicklaunch.subtitle"))
        self.g_main.titleLabel.setText(tr("quicklaunch.group.main"))
        self.g_tasks.titleLabel.setText(tr("quicklaunch.group.tasks"))
        self.masterCard.setTitle(tr("quicklaunch.master"))
        self.masterCard.setContent(tr("quicklaunch.master.hint"))
        self.bindCard.setTitle(tr("quicklaunch.bind"))
        self.bindCard.setContent(tr("quicklaunch.bind.hint"))
        self.convertCard.setTitle(tr("nav.convert"))
        self.convertCard.setContent(tr("quicklaunch.convert.hint"))
        self.compressCard.setTitle(tr("nav.compress"))
        self.compressCard.setContent(tr("quicklaunch.compress.hint"))
        self.upscaleCard.setTitle(tr("nav.upscale"))
        self.upscaleCard.setContent(tr("quicklaunch.upscale.hint"))
        # v0.8.1 Bug4-③：通知卡片的标题与简介此前漏更新，切换语言后不刷新
        self.notifyStartCard.setTitle(tr("quicklaunch.notify.start"))
        self.notifyStartCard.setContent(tr("quicklaunch.notify.start.hint"))
        self.notifyDoneCard.setTitle(tr("quicklaunch.notify.done"))
        self.notifyDoneCard.setContent(tr("quicklaunch.notify.done.hint"))
        self._resize_notify_cards()
        self.statusCard.retranslate()
