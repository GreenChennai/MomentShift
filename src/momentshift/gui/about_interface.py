"""About interface: app identity, links, license notice."""

from ..core.qt_compat import QVBoxLayout, QDesktopServices, QUrl
from qfluentwidgets import (
    CardWidget,
    FluentIcon as FIF,
    TitleLabel,
    BodyLabel,
    StrongBodyLabel,
    CaptionLabel,
    HyperlinkCard,
    PushButton,
)
from ..i18n.translator import tr
from ..metadata import APP_NAME, VERSION, AUTHOR, REPO_URL, RELEASE_URL
from .base import InterfaceBase


class AboutInterface(InterfaceBase):
    def __init__(self, parent=None):
        super().__init__("About", tr("about.title"), "", parent)

        card = CardWidget()
        cv = QVBoxLayout(card)
        cv.setContentsMargins(22, 22, 22, 22)
        cv.setSpacing(10)

        self.nameLabel = TitleLabel(f"{tr('app.title')}  ·  {APP_NAME}")
        self.tagLabel = BodyLabel(tr("about.description"))
        self.verLabel = StrongBodyLabel(f"{tr('about.version')}: {VERSION}")
        self.authorLabel = BodyLabel(f"{tr('about.author')}: {AUTHOR}")
        self.repoCard = HyperlinkCard(REPO_URL, REPO_URL, FIF.LINK, tr("about.repo"))
        self.techLabel = CaptionLabel(tr("about.tech"))
        self.licenseLabel = CaptionLabel(tr("about.license"))
        self.disclaimerLabel = CaptionLabel(tr("about.disclaimer"))
        self.updateBtn = PushButton(tr("about.check_update"), icon=FIF.LINK)
        self.updateBtn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(RELEASE_URL))
        )

        for w in (
            self.nameLabel, self.tagLabel, self.verLabel, self.authorLabel,
            self.repoCard, self.techLabel, self.licenseLabel,
            self.disclaimerLabel, self.updateBtn,
        ):
            cv.addWidget(w)

        self.vbox.addWidget(card)
        self.vbox.addStretch(1)

    def retranslateUi(self):
        self.retranslate(tr("about.title"))
        self.nameLabel.setText(f"{tr('app.title')}  ·  {APP_NAME}")
        self.tagLabel.setText(tr("about.description"))
        self.verLabel.setText(f"{tr('about.version')}: {VERSION}")
        self.authorLabel.setText(f"{tr('about.author')}: {AUTHOR}")
        self.repoCard.setTitle(tr("about.repo"))
        self.techLabel.setText(tr("about.tech"))
        self.licenseLabel.setText(tr("about.license"))
        self.disclaimerLabel.setText(tr("about.disclaimer"))
        self.updateBtn.setText(tr("about.check_update"))
