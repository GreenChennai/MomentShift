"""设置页：语言、托盘、硬件加速、并发线程、ffmpeg 来源、配置文件、恢复默认。

职责边界：
- 做：渲染设置卡片、把控件取值写回 qconfig、提供打开配置文件与重置入口。
- 不做：不定义配置项本身（在 core/config）；不直接改注册表（交给 core/quick_launch）。

依赖：core/config、core/logger、core/platform、core/qt_compat、core/quick_launch、gui/base、gui/theme、i18n/translator；被依赖：主窗口按导航页装载。

全部用 qfluentwidgets 的 ``SettingCard`` 积木搭建（不自造 UI），按
常规 / 转换 / 数据 三组排列。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QSpinBox
from qfluentwidgets import (
    ComboBox,
    ConfigItem,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    PushSettingCard,
    SettingCard,
    SettingCardGroup,
    SwitchSettingCard,
    qconfig,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from ..core import quick_launch
from ..core.config import cfg
from ..core.logger import get_logger
from ..core.platform import config_file, popen_silent
from ..core.qt_compat import QDesktopServices, QUrl
from ..i18n.translator import available_languages, tr
from . import tokens
from .base import InterfaceBase
from .theme import surface

log = get_logger("settings")


class ComboSettingCard(SettingCard):
    """带下拉框的设置卡片，选中项的真实值通过 ``userData`` 存取。

    典型用法::

        card = ComboSettingCard(cfg.language, FIF.LANGUAGE, "语言", "",
                                [(name, key) for key, name in langs], self)

    为什么用 userData 而不是下拉文本：文本要跟随 i18n 变化，而配置里存的是
    稳定的枚举值。分开之后，普通配置项和 OptionsConfigItem 都能复用这张卡片。
    """

    def __init__(self, configItem, icon, title, content, options, parent=None):
        super().__init__(icon, title, content, parent)
        self.configItem = configItem
        self.options = options
        self.combo = ComboBox(self)
        for text, value in options:
            self.combo.addItem(text, userData=value)
        self.hBoxLayout.addStretch(1)
        self.hBoxLayout.addWidget(self.combo)
        self.hBoxLayout.addSpacing(8)

        for i, (text, value) in enumerate(options):
            if value == configItem.value:
                self.combo.blockSignals(True)
                self.combo.setCurrentIndex(i)
                self.combo.blockSignals(False)
                break
        self.combo.currentIndexChanged.connect(self._on_changed)
        configItem.valueChanged.connect(self._on_external)

    def _on_changed(self, _index):
        """用户切换下拉项时写回配置。

        Notes:
            currentData() 为 None 说明是构造期的空选中，此时不写配置，
            否则会把配置刷成无效值。
        """
        val = self.combo.currentData()
        if val is not None:
            self.configItem.value = val

    def _on_external(self, value):
        """配置项被别处改动时同步下拉选中项。

        v0.8.0 ODD-03：原实现把 ``setCurrentIndex`` 当查找用——挨个把下拉真的
        切过去再看 ``currentData()`` 对不对。副作用有两个：一是没命中时会停在
        最后一项（静默选错），二是每切一次都在跑 combo 的重绘。
        改用 ``findData`` 一次定位。
        """
        index = self.combo.findData(value)
        if index < 0:
            # 值不在候选里（配置文件被手改过）：保持当前选中，不静默跳到末项。
            return
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(index)
        self.combo.blockSignals(False)


class IntInputSettingCard(SettingCard):
    """带数字输入框的设置卡片，用于「并发线程数」这类有界整数。

    典型用法::

        card = IntInputSettingCard(cfg.maxThreads, FIF.SPEED, "并发线程数", "",
                                   self, minimum=1, maximum=16)

    为什么用输入框而不是滑块：线程数用户往往有明确目标值（例如 4、8），
    滑块拖到精确值反而费劲，直接输入更快。
    """

    def __init__(
        self, configItem, icon, title, content, parent=None, minimum: int = 1, maximum: int = 16
    ):
        super().__init__(icon, title, content, parent)
        self.configItem = configItem
        self.spin = QSpinBox(self)
        self.spin.setRange(minimum, maximum)
        self.spin.setValue(int(configItem.value))
        self.spin.setFixedWidth(76)
        self.spin.setObjectName("threadsSpin")
        # 去掉 +/- 微调按钮，退化为纯数字输入框：
        # 线程数区间只有 1~16，点按钮反而比直接键入慢
        self.spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.spin.setStyleSheet(tokens.input_qss("QSpinBox", 6))
        self.hBoxLayout.addStretch(1)
        self.hBoxLayout.addWidget(self.spin)
        self.hBoxLayout.addSpacing(8)
        self.spin.valueChanged.connect(self._on_changed)
        configItem.valueChanged.connect(self._on_external)

    def _on_changed(self, value: int):
        """用户修改数值时写回配置。"""
        self.configItem.value = value

    def _on_external(self, value):
        """配置项被别处改动时同步输入框。

        Notes:
            必须 blockSignals，否则回填会再次触发 valueChanged，
            和 _on_changed 形成信号回环。
        """
        self.spin.blockSignals(True)
        self.spin.setValue(int(value))
        self.spin.blockSignals(False)


class SettingInterface(InterfaceBase):
    """设置页：语言、主题、转换参数与数据管理入口。

    典型用法：由主窗口作为导航子页实例化一次，语言/主题切换后
    调用 retranslate() / retheme() 局部刷新，不重建页面。

    线程约定：仅在 GUI 主线程使用。
    """

    def __init__(self, parent=None):
        super().__init__("Settings", tr("settings.title"), "", parent)

        # --- 通用：语言 + 托盘 ---
        self.g_general = SettingCardGroup(tr("settings.group.general"))

        lang_options = [(name, key.value) for key, name in available_languages()]
        self.langCard = ComboSettingCard(
            cfg.language, FIF.LANGUAGE, tr("settings.language"), "", lang_options
        )
        self.trayCard = SwitchSettingCard(
            FIF.MINIMIZE,
            tr("settings.close_to_tray"),
            tr("settings.close_to_tray.hint"),
            cfg.closeToTray,
        )
        for c in (self.langCard, self.trayCard):
            self.g_general.addSettingCard(c)
        self.vbox.addWidget(self.g_general)

        # --- 转换：硬件加速 + 并发线程数 + ffmpeg 来源 ---
        self.g_convert = SettingCardGroup(tr("settings.group.conversion"))
        self.hwCard = ComboSettingCard(
            cfg.hardware,
            FIF.ROBOT,
            tr("settings.hardware"),
            tr("settings.hardware.hint"),
            [
                (tr("settings.hardware.auto"), "auto"),
                (tr("settings.hardware.cpu"), "cpu"),
                (tr("settings.hardware.gpu"), "gpu"),
            ],
        )
        # 并发线程数用数字输入框而非滑块，默认 3
        self.threadsCard = IntInputSettingCard(
            cfg.maxThreads,
            FIF.SYNC,
            tr("settings.threads"),
            tr("settings.threads.hint"),
            minimum=1,
            maximum=16,
        )
        self.ffCard = ComboSettingCard(
            cfg.ffmpegSource,
            FIF.CLOUD,
            tr("settings.ffmpeg"),
            "",
            [(tr("settings.ffmpeg.auto"), "auto"), (tr("settings.ffmpeg.path"), "path")],
        )
        for c in (self.hwCard, self.threadsCard, self.ffCard):
            self.g_convert.addSettingCard(c)
        self.vbox.addWidget(self.g_convert)

        # --- 数据：打开配置文件 + 重置 ---
        self.g_data = SettingCardGroup(tr("settings.group.data"))
        self.openCfgCard = PushSettingCard(
            tr("settings.open_config_btn"), FIF.FOLDER_ADD, tr("settings.open_config"), ""
        )
        self.openCfgCard.button.clicked.connect(self._open_config)
        self.resetCard = PushSettingCard(
            tr("settings.reset_btn"), FIF.DELETE, tr("settings.reset"), ""
        )
        self.resetCard.button.clicked.connect(self._reset)
        for c in (self.openCfgCard, self.resetCard):
            self.g_data.addSettingCard(c)
        self.vbox.addWidget(self.g_data)

        # 记下每个分组的原始 qss：retheme() 每次都基于这份原始样式重新拼接背景色，
        # 否则反复切主题会让背景规则不断叠加，越攒越长且后写的未必生效
        self._group_qss = {
            self.g_general: self.g_general.styleSheet(),
            self.g_convert: self.g_convert.styleSheet(),
            self.g_data: self.g_data.styleSheet(),
        }

        self.vbox.addStretch(1)
        self.retheme()

    # --- 主题 ---
    def retheme(self):
        """跟随主题刷新设置页背景。

        Notes:
            踩坑教训：qfluentwidgets 的 SettingCard 自带一层淡白色叠加，
            单改卡片背景在深色下依旧发白。真正决定明暗观感的是
            SettingCardGroup 的背景，所以这里显式给分组刷主题色。
        """
        super().retheme()
        bg = surface().name()
        for grp, base_qss in self._group_qss.items():
            grp.setStyleSheet(f"{base_qss}\nSettingCardGroup{{ background-color: {bg}; }}")

    # --- 动作 ---
    def _open_config(self):
        """用系统默认程序打开 config.json（v0.8.0 Q5 / INFRA-04）。

        原实现是 ``subprocess.Popen(["notepad.exe", <拼出来的路径>])``，三个毛病：

        1. 硬编码 notepad.exe——用户装了 VS Code / Notepad++ 也用不上；
        2. 没带 ``CREATE_NO_WINDOW``，会闪一个黑框；
        3. 路径是 ``app_base_dir()/config.json`` 手工拼的，和
           ``core.platform.config_file()`` 各算各的，将来改目录必漏一处。

        现在优先走 ``QDesktopServices.openUrl``（系统默认关联程序）；它返回
        False（比如 .json 没有关联程序）时才回退 notepad.exe，且走
        ``popen_silent`` 不闪黑框。
        """
        path = config_file()
        if not path.exists():
            # 配置还没落过盘（全默认值且从未改动）：先写一份再打开，
            # 否则默认程序会开出一个"文件不存在"的错。
            qconfig.save()
        if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            return
        log.info("系统未关联 .json，回退 notepad.exe 打开配置：%s", path)
        try:
            popen_silent(["notepad.exe", str(path)])
        except OSError:
            log.exception("打开配置文件失败：%s", path)
            InfoBar.warning(
                tr("settings.open_config"),
                str(path),
                parent=self.window(),
                duration=4000,
                position=InfoBarPosition.TOP_RIGHT,
            )

    def _reset(self):
        """恢复**全部**设置项的默认值（v0.8.0 Q4）。

        原实现只重置了 language / hardware / maxThreads / ffmpegSource 四项，
        输出目录、输出模式、后缀、右键菜单开关、通知开关等十几项纹丝不动——
        按钮叫「恢复默认设置」，实际只恢复了五分之一。

        改为遍历 ``Config`` 自己声明的所有 ``ConfigItem`` 逐个写回
        ``defaultValue``。刻意只取 ``Config.__dict__``（不含父类 ``QConfig`` 的
        themeMode / themeColor）：主题色是程序常量而非用户设置，重置它反而会把
        品牌绿冲掉。
        """
        box = MessageBox(tr("settings.reset"), tr("settings.reset_confirm"), self.window())
        if not box.exec():
            return

        for item in vars(type(cfg)).values():
            if isinstance(item, ConfigItem):
                item.value = item.defaultValue
        qconfig.save()

        # 右键菜单开关被重置回"全关"后，注册表里的菜单项必须跟着撤掉，
        # 否则设置说没开、资源管理器里却还挂着（配置与系统状态脱节）。
        try:
            quick_launch.apply_all(False, {})
        except OSError:
            log.exception("重置时注销右键菜单失败")

        InfoBar.success(
            tr("settings.reset"),
            tr("settings.restart_hint"),
            parent=self.window(),
            duration=2000,
            position=InfoBarPosition.TOP_RIGHT,
        )

    def retranslateUi(self):
        self.retranslate(tr("settings.title"))
        self.g_general.titleLabel.setText(tr("settings.group.general"))
        self.g_convert.titleLabel.setText(tr("settings.group.conversion"))
        self.g_data.titleLabel.setText(tr("settings.group.data"))
        self.langCard.setTitle(tr("settings.language"))
        self.trayCard.setTitle(tr("settings.close_to_tray"))
        self.trayCard.setContent(tr("settings.close_to_tray.hint"))
        self.hwCard.setTitle(tr("settings.hardware"))
        self.hwCard.setContent(tr("settings.hardware.hint"))
        self.threadsCard.setTitle(tr("settings.threads"))
        self.threadsCard.setContent(tr("settings.threads.hint"))
        self.ffCard.setTitle(tr("settings.ffmpeg"))
        self.openCfgCard.setTitle(tr("settings.open_config"))
        self.resetCard.setTitle(tr("settings.reset"))
