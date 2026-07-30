"""Settings interface: language, theme, hardware, threads, ffmpeg, output, etc.

Rebuilt with qfluentwidgets ``SettingCard`` building blocks (no bespoke UI).
Grouped into General / Conversion / Data sections to match user expectations.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QSpinBox

from ..core.qt_compat import QFileDialog, QDesktopServices, QUrl
from qfluentwidgets import (
    SettingCard,
    SettingCardGroup,
    PushSettingCard,
    SwitchSettingCard,
    ComboBox,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    qconfig,
)
from ..core.config import cfg, app_base_dir
from ..i18n.translator import tr, LocaleKey, available_languages
from .base import InterfaceBase
from .theme import surface


class ComboSettingCard(SettingCard):
    """Setting card with a ComboBox whose selected value is stored as the config
    item *value* (via ``userData``), so it works for both plain and option items."""

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
        val = self.combo.currentData()
        if val is not None:
            self.configItem.value = val

    def _on_external(self, value):
        self.combo.blockSignals(True)
        for i in range(self.combo.count()):
            self.combo.setCurrentIndex(i)
            if self.combo.currentData() == value:
                break
        self.combo.blockSignals(False)


class IntInputSettingCard(SettingCard):
    """A setting card with a numeric **input box** (spin box).

    Used for "并发线程数" (#8): a bounded integer input instead of a slider.
    """

    def __init__(self, configItem, icon, title, content, parent=None,
                 minimum: int = 1, maximum: int = 16):
        super().__init__(icon, title, content, parent)
        self.configItem = configItem
        self.spin = QSpinBox(self)
        self.spin.setRange(minimum, maximum)
        self.spin.setValue(int(configItem.value))
        self.spin.setFixedWidth(76)
        self.spin.setObjectName("threadsSpin")
        # Remove the +/- stepper buttons — a plain numeric text input (#7).
        self.spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.spin.setStyleSheet(
            "QSpinBox{ border: 1px solid #d0d0d0; border-radius: 6px;"
            " padding: 4px 8px; background: #ffffff; }"
        )
        self.hBoxLayout.addStretch(1)
        self.hBoxLayout.addWidget(self.spin)
        self.hBoxLayout.addSpacing(8)
        self.spin.valueChanged.connect(self._on_changed)
        configItem.valueChanged.connect(self._on_external)

    def _on_changed(self, value: int):
        self.configItem.value = value

    def _on_external(self, value):
        self.spin.blockSignals(True)
        self.spin.setValue(int(value))
        self.spin.blockSignals(False)


class SettingInterface(InterfaceBase):
    def __init__(self, parent=None):
        super().__init__("Settings", tr("settings.title"), "", parent)

        # --- General: language + theme -----------------------------------
        self.g_general = SettingCardGroup(tr("settings.group.general"))

        lang_options = [(name, key.value) for key, name in available_languages()]
        self.langCard = ComboSettingCard(
            cfg.language, FIF.LANGUAGE, tr("settings.language"), "", lang_options)
        self.autoFoldCard = SwitchSettingCard(
            FIF.HIDE, tr("settings.auto_fold"), tr("settings.auto_fold.hint"),
            cfg.autoCollapse)
        self.trayCard = SwitchSettingCard(
            FIF.MINIMIZE, tr("settings.close_to_tray"), tr("settings.close_to_tray.hint"),
            cfg.closeToTray)
        for c in (self.langCard, self.autoFoldCard, self.trayCard):
            self.g_general.addSettingCard(c)
        self.vbox.addWidget(self.g_general)

        # --- Conversion: hardware + threads + ffmpeg source --------------
        self.g_convert = SettingCardGroup(tr("settings.group.conversion"))
        self.hwCard = ComboSettingCard(
            cfg.hardware, FIF.ROBOT, tr("settings.hardware"),
            tr("settings.threads.hint"),
            [(tr("settings.hardware.auto"), "auto"),
             (tr("settings.hardware.cpu"), "cpu"),
             (tr("settings.hardware.gpu"), "gpu")])
        # "并发线程数" is now a numeric input box (default 3), see #8.
        self.threadsCard = IntInputSettingCard(
            cfg.maxThreads, FIF.SYNC, tr("settings.threads"), tr("settings.threads.hint"),
            minimum=1, maximum=16)
        self.ffCard = ComboSettingCard(
            cfg.ffmpegSource, FIF.CLOUD, tr("settings.ffmpeg"), "",
            [(tr("settings.ffmpeg.auto"), "auto"),
             (tr("settings.ffmpeg.path"), "path")])
        for c in (self.hwCard, self.threadsCard, self.ffCard):
            self.g_convert.addSettingCard(c)
        self.vbox.addWidget(self.g_convert)

        # --- Data: open config + reset -----------------------------------
        self.g_data = SettingCardGroup(tr("settings.group.data"))
        self.openCfgCard = PushSettingCard(
            tr("settings.open_config_btn"), FIF.FOLDER_ADD, tr("settings.open_config"), "")
        self.openCfgCard.button.clicked.connect(self._open_config)
        self.resetCard = PushSettingCard(
            tr("settings.reset_btn"), FIF.DELETE, tr("settings.reset"), "")
        self.resetCard.button.clicked.connect(self._reset)
        for c in (self.openCfgCard, self.resetCard):
            self.g_data.addSettingCard(c)
        self.vbox.addWidget(self.g_data)

        # Remember each group's pristine qss so retheme() can re-apply it with
        # an explicit theme background without the rule accumulating (#2).
        self._group_qss = {
            self.g_general: self.g_general.styleSheet(),
            self.g_convert: self.g_convert.styleSheet(),
            self.g_data: self.g_data.styleSheet(),
        }

        self.vbox.addStretch(1)
        self.retheme()

    # -- theme -----------------------------------------------------------
    def retheme(self):
        super().retheme()
        # qfluentwidgets SettingCard paints a faint white overlay, so theming
        # the *group* background is what actually drives light/dark for the
        # whole settings page. Apply our theme surface explicitly so the page
        # follows dark mode (the reported #2 regression).
        bg = surface().name()
        for grp, base_qss in self._group_qss.items():
            grp.setStyleSheet(f"{base_qss}\nSettingCardGroup{{ background-color: {bg}; }}")

    # -- actions ---------------------------------------------------------
    def _open_config(self):
        import subprocess, os
        config_file = os.path.join(str(app_base_dir()), "config.json")
        subprocess.Popen(["notepad.exe", config_file])

    def _reset(self):
        box = MessageBox(tr("settings.reset"), tr("settings.restart_hint"), self.window())
        if box.exec():
            cfg.language.value = "Auto"
            cfg.hardware.value = "auto"
            cfg.maxThreads.value = 3
            cfg.ffmpegSource.value = "auto"
            qconfig.save()
            InfoBar.success(
                tr("settings.reset"), "", parent=self.window(),
                duration=2000, position=InfoBarPosition.TOP_RIGHT)

    def retranslateUi(self):
        self.retranslate(tr("settings.title"))
        self.g_general.titleLabel.setText(tr("settings.group.general"))
        self.g_convert.titleLabel.setText(tr("settings.group.conversion"))
        self.g_data.titleLabel.setText(tr("settings.group.data"))
        self.langCard.setTitle(tr("settings.language"))
        self.autoFoldCard.setTitle(tr("settings.auto_fold"))
        self.autoFoldCard.setContent(tr("settings.auto_fold.hint"))
        self.trayCard.setTitle(tr("settings.close_to_tray"))
        self.trayCard.setContent(tr("settings.close_to_tray.hint"))
        self.hwCard.setTitle(tr("settings.hardware"))
        self.threadsCard.setTitle(tr("settings.threads"))
        self.threadsCard.setContent(tr("settings.threads.hint"))
        self.ffCard.setTitle(tr("settings.ffmpeg"))
        self.openCfgCard.setTitle(tr("settings.open_config"))
        self.resetCard.setTitle(tr("settings.reset"))
