"""快速调用界面 —— 管理 Windows 右键菜单集成设置（v0.2.9）。

通过 SettingCard 结构展示：
- 总开关（启用/禁用所有快速调用）
- 绑定右键菜单开关
- 各功能独立开关（转换 / 压缩 / 放大）
- 注册状态指示

所有快捷调用默认关闭，用户手动开启。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout
from PyQt6.QtCore import QProcess

from qfluentwidgets import (
    FluentIcon as FIF,
    SettingCard,
    SettingCardGroup,
    SwitchSettingCard,
    CaptionLabel, StrongBodyLabel, BodyLabel,
    CardWidget,

)

from ..core.config import cfg
from ..core import quick_launch
from ..i18n.translator import tr
from .base import InterfaceBase
from .theme import (
    ThemedCard, primary_btn, muted_text, success_color,
    danger_color, accent_name, CARD_MARGIN, surface, border_color,
)


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
            dot = CaptionLabel("●")
            dot.setFixedWidth(16)
            label = BodyLabel(task_labels.get(t, t))
            hb.addWidget(dot)
            hb.addWidget(label, 1)
            vb.addWidget(row)
            self._status_rows[t] = (dot, label)

        self.refresh()

    def refresh(self):
        """刷新各功能注册状态指示。"""
        tasks = quick_launch.available_tasks()
        all_ok = True
        for t in tasks:
            registered = quick_launch.is_context_menu_registered(t)
            dot, label = self._status_rows[t]
            if registered:
                dot.setStyleSheet(f"color: {success_color().name()}; font-size: 14px;")
            else:
                dot.setStyleSheet(f"color: {danger_color().name()}; font-size: 14px;")
                all_ok = False

    def retranslate(self):
        self.titleLbl.setText(tr("quicklaunch.status.title"))


class QuickLaunchInterface(InterfaceBase):
    """快速调用设置标签页。

    用户在此管理 Windows 右键菜单的快捷调用功能。
    """

    def __init__(self, parent=None):
        super().__init__("QuickLaunch", tr("quicklaunch.title"),
                         tr("quicklaunch.subtitle"), parent)

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
            FIF.HOME, tr("nav.convert"), tr("quicklaunch.convert.hint"),
            cfg.quickLaunchConvert,
        )
        self.convertCard.checkedChanged.connect(self._on_task)
        self.compressCard = SwitchSettingCard(
            FIF.PHOTO, tr("nav.compress"), tr("quicklaunch.compress.hint"),
            cfg.quickLaunchCompress,
        )
        self.compressCard.checkedChanged.connect(self._on_task)
        self.upscaleCard = SwitchSettingCard(
            FIF.ZOOM, tr("nav.upscale"), tr("quicklaunch.upscale.hint"),
            cfg.quickLaunchUpscale,
        )
        self.upscaleCard.checkedChanged.connect(self._on_task)
        self.g_tasks.addSettingCard(self.convertCard)
        self.g_tasks.addSettingCard(self.compressCard)
        self.g_tasks.addSettingCard(self.upscaleCard)
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

        # v0.7.21：打开设置页时自动按最新命令格式（%* 无引号）重写注册表，
        # 修复旧版 `"%*"`/`%1` 命令导致右键无文件参数的问题
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, self._apply)

    # =========================================================================
    # 开关响应
    # =========================================================================

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
            grp.setStyleSheet(
                f"{base_qss}\nSettingCardGroup{{ background-color: {bg}; }}")
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
        self.statusCard.retranslate()
