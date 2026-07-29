"""About interface: app identity, links, license notice.

Rebuilt as a single, premium ``ThemedCard`` with an accent rule under the title
to match the rest of the v0.1.7 design system.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QVBoxLayout, QFrame, QHBoxLayout

from ..core.qt_compat import QDesktopServices, QUrl
from qfluentwidgets import (
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
from .theme import ThemedCard, accent_name, CARD_MARGIN, muted_text


class AboutInterface(InterfaceBase):
    def __init__(self, parent=None):
        super().__init__("About", tr("about.title"), "", parent)

        card = ThemedCard()
        cv = QVBoxLayout(card)
        cv.setContentsMargins(CARD_MARGIN, 20, CARD_MARGIN, 20)
        cv.setSpacing(10)

        # title + accent underline
        self.nameLabel = TitleLabel(f"{APP_NAME}  ·  {tr('app.title')}")
        cv.addWidget(self.nameLabel)
        self.accentRule = QFrame()
        self.accentRule.setFrameShape(QFrame.Shape.HLine)
        self.accentRule.setFixedHeight(3)
        self.accentRule.setFixedWidth(40)
        self.accentRule.setStyleSheet(
            f"QFrame{{ background: {accent_name()}; border: none; border-radius: 2px; }}")
        cv.addWidget(self.accentRule)
        cv.addSpacing(6)

        self.tagLabel = BodyLabel(tr("about.description"))
        self.tagLabel.setWordWrap(True)
        self.verLabel = StrongBodyLabel(f"{tr('about.version')}: {VERSION}")
        self.authorLabel = BodyLabel(f"{tr('about.author')}: {AUTHOR}")
        cv.addWidget(self.tagLabel)
        cv.addWidget(self.verLabel)
        cv.addWidget(self.authorLabel)

        cv.addSpacing(8)
        # Use a compact URL text so it fits in 450px-wide window
        self.repoCard = HyperlinkCard(
            "github.com/GreenChennai/MomentShift", REPO_URL,
            FIF.GITHUB, tr("about.repo"))
        cv.addWidget(self.repoCard)

        cv.addSpacing(8)
        self.techLabel = CaptionLabel(tr("about.tech"))
        self.licenseLabel = CaptionLabel(tr("about.license"))
        self.disclaimerLabel = CaptionLabel(tr("about.disclaimer"))
        for lbl in (self.techLabel, self.licenseLabel, self.disclaimerLabel):
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color: {muted_text()};")
            cv.addWidget(lbl)

        cv.addSpacing(10)
        self.updateBtn = PushButton(tr("about.check_update"), icon=FIF.UPDATE)
        self.updateBtn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(RELEASE_URL)))
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.updateBtn)
        cv.addLayout(row)

        self.vbox.addWidget(card)
        self.vbox.addStretch(1)
        self.retheme()

    def retheme(self):
        super().retheme()
        self.accentRule.setStyleSheet(
            f"QFrame{{ background: {accent_name()}; border: none; border-radius: 2px; }}")
        for lbl in (self.techLabel, self.licenseLabel, self.disclaimerLabel):
            lbl.setStyleSheet(f"color: {muted_text()};")

    def retranslateUi(self):
        self.retranslate(tr("about.title"))
        self.nameLabel.setText(f"{APP_NAME}  ·  {tr('app.title')}")
        self.tagLabel.setText(tr("about.description"))
        self.verLabel.setText(f"{tr('about.version')}: {VERSION}")
        self.authorLabel.setText(f"{tr('about.author')}: {AUTHOR}")
        self.repoCard.setTitle(tr("about.repo"))
        self.techLabel.setText(tr("about.tech"))
        self.licenseLabel.setText(tr("about.license"))
        self.disclaimerLabel.setText(tr("about.disclaimer"))
        self.updateBtn.setText(tr("about.check_update"))
