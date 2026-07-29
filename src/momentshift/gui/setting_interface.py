"""Settings interface: language, hardware, threads, output, theme, etc."""

from ..core.qt_compat import QFileDialog, QDesktopServices, QUrl
from qfluentwidgets import (
    SettingCard,
    SettingCardGroup,
    RangeSettingCard,
    PushSettingCard,
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
    """A setting card with a ComboBox whose selected value is stored as the
    config item *value* (not the display text), via ``userData``."""

    def __init__(self, configItem, icon, title, content, options, parent=None):
        super().__init__(icon, title, content, parent)
        self.configItem = configItem
        self.options = options
        self.combo = ComboBox(self)
        for text, value in options:
            self.combo.addItem(text, userData=value)
        self.hBoxLayout.addStretch(1)
        self.hBoxLayout.addWidget(self.combo)
        self.hBoxLayout.addSpacing(8)  # keep a safe distance from the right edge

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
        self.setStyleSheet("#cardSub{color: rgba(128,128,128,1);}")

        group = SettingCardGroup(tr("settings.group.general"))

        lang_options = [(name, key.value) for key, name in available_languages()]
        self.langCard = ComboSettingCard(
            cfg.language, FIF.LANGUAGE, tr("settings.language"), "", lang_options
        )
        hw_options = [
            (tr("settings.hardware.auto"), "auto"),
            (tr("settings.hardware.cpu"), "cpu"),
            (tr("settings.hardware.gpu"), "gpu"),
        ]
        self.hwCard = ComboSettingCard(
            cfg.hardware, FIF.VIDEO, tr("settings.hardware"),
            tr("settings.threads.hint"), hw_options,
        )
        self.threadsCard = RangeSettingCard(
            cfg.maxThreads, FIF.IOT, tr("settings.threads"), tr("settings.threads.hint")
        )
        ff_options = [
            (tr("settings.ffmpeg.auto"), "auto"),
            (tr("settings.ffmpeg.path"), "path"),
        ]
        self.ffCard = ComboSettingCard(
            cfg.ffmpegSource, FIF.ROBOT, tr("settings.ffmpeg"), "", ff_options
        )
        self.outCard = PushSettingCard(
            cfg.outputFolder.value or "", FIF.FOLDER,
            tr("settings.output"), tr("settings.output.hint"),
        )
        self.outCard.button.clicked.connect(self._choose_output)

        theme_options = [
            (tr("settings.theme.auto"), "auto"),
            (tr("settings.theme.light"), "light"),
            (tr("settings.theme.dark"), "dark"),
        ]
        self.themeCard = ComboSettingCard(
            cfg.theme, FIF.SETTING, tr("settings.theme"),
            tr("settings.restart_hint"), theme_options,
        )

        self.openCfgCard = PushSettingCard(
            tr("settings.open_config_btn"), FIF.FOLDER, tr("settings.open_config"), ""
        )
        self.openCfgCard.button.clicked.connect(self._open_config)
        self.resetCard = PushSettingCard(
            tr("settings.reset_btn"), FIF.DELETE, tr("settings.reset"), ""
        )
        self.resetCard.button.clicked.connect(self._reset)

        for card in (
            self.langCard, self.hwCard, self.threadsCard, self.ffCard,
            self.outCard, self.themeCard, self.openCfgCard, self.resetCard,
        ):
            group.addSettingCard(card)

        self.vbox.addWidget(group)
        self.vbox.addStretch(1)

    # -- actions ---------------------------------------------------------
    def _choose_output(self):
        d = QFileDialog.getExistingDirectory(
            self, tr("settings.output"), cfg.outputFolder.value or ""
        )
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
                duration=2000, position=InfoBarPosition.TOP_RIGHT,
            )

    def retranslateUi(self):
        self.retranslate(tr("settings.title"))
        if hasattr(self, "group"):
            self.group.setTitle(tr("settings.group.general"))
        self.langCard.setTitle(tr("settings.language"))
        self.hwCard.setTitle(tr("settings.hardware"))
        self.threadsCard.setTitle(tr("settings.threads"))
        self.threadsCard.setContent(tr("settings.threads.hint"))
        self.ffCard.setTitle(tr("settings.ffmpeg"))
        self.outCard.setTitle(tr("settings.output"))
        self.outCard.setContent(tr("settings.output.hint"))
        self.themeCard.setTitle(tr("settings.theme"))
        self.openCfgCard.setTitle(tr("settings.open_config"))
        self.resetCard.setTitle(tr("settings.reset"))
