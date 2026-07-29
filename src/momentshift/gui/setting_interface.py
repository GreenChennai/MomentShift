"""Settings interface: language, theme, hardware, threads, ffmpeg, output, etc.

Rebuilt with qfluentwidgets ``SettingCard`` building blocks (no bespoke UI).
Grouped into General / Conversion / Data sections to match user expectations.
"""

from __future__ import annotations

from ..core.qt_compat import QFileDialog, QDesktopServices, QUrl
from qfluentwidgets import (
    SettingCard,
    SettingCardGroup,
    RangeSettingCard,
    PushSettingCard,
    SwitchSettingCard,
    ComboBox,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    qconfig,
)
from ..core.config import cfg, config_dir
from ..i18n.translator import tr, LocaleKey, available_languages
from .base import InterfaceBase


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


class SettingInterface(InterfaceBase):
    def __init__(self, parent=None):
        super().__init__("Settings", tr("settings.title"), "", parent)

        # --- General: language + theme + output --------------------------
        self.g_general = SettingCardGroup(tr("settings.group.general"))

        lang_options = [(name, key.value) for key, name in available_languages()]
        self.langCard = ComboSettingCard(
            cfg.language, FIF.LANGUAGE, tr("settings.language"), "", lang_options)
        self.themeCard = ComboSettingCard(
            cfg.theme, FIF.PALETTE, tr("settings.theme"),
            tr("settings.restart_hint"),
            [(tr("settings.theme.auto"), "auto"),
             (tr("settings.theme.light"), "light"),
             (tr("settings.theme.dark"), "dark")])
        self.outCard = PushSettingCard(
            cfg.outputFolder.value or tr("settings.output.fixed_hint"),
            FIF.FOLDER, tr("settings.output"), tr("settings.output.hint"))
        self.outCard.button.clicked.connect(self._choose_output)
        self.autoFoldCard = SwitchSettingCard(
            FIF.HIDE, tr("settings.auto_fold"), tr("settings.auto_fold.hint"),
            cfg.autoCollapse)
        for c in (self.langCard, self.themeCard, self.outCard, self.autoFoldCard):
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
        self.threadsCard = RangeSettingCard(
            cfg.maxThreads, FIF.SYNC, tr("settings.threads"), tr("settings.threads.hint"))
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

        self.vbox.addStretch(1)
        self.retheme()

    # -- theme -----------------------------------------------------------
    def retheme(self):
        super().retheme()

    # -- actions ---------------------------------------------------------
    def _choose_output(self):
        d = QFileDialog.getExistingDirectory(
            self, tr("settings.output"), cfg.outputFolder.value or "")
        if d:
            cfg.outputFolder.value = d
            self.outCard.button.setText(d)

    def _open_config(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(config_dir())))

    def _reset(self):
        box = MessageBox(tr("settings.reset"), tr("settings.restart_hint"), self.window())
        if box.exec():
            cfg.language.value = "Auto"
            cfg.theme.value = "auto"
            cfg.hardware.value = "auto"
            cfg.maxThreads.value = 4
            cfg.ffmpegSource.value = "auto"
            cfg.outputFolder.value = ""
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
        self.themeCard.setTitle(tr("settings.theme"))
        self.themeCard.setContent(tr("settings.restart_hint"))
        self.outCard.setTitle(tr("settings.output"))
        self.outCard.setContent(tr("settings.output.hint"))
        self.autoFoldCard.setTitle(tr("settings.auto_fold"))
        self.autoFoldCard.setContent(tr("settings.auto_fold.hint"))
        self.hwCard.setTitle(tr("settings.hardware"))
        self.threadsCard.setTitle(tr("settings.threads"))
        self.threadsCard.setContent(tr("settings.threads.hint"))
        self.ffCard.setTitle(tr("settings.ffmpeg"))
        self.openCfgCard.setTitle(tr("settings.open_config"))
        self.resetCard.setTitle(tr("settings.reset"))
