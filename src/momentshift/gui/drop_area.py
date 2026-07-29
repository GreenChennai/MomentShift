"""Drag-and-drop target area for adding files to the conversion queue."""

from ..core.qt_compat import QWidget, QVBoxLayout, QLabel, Signal, QDragEnterEvent, QDropEvent, Qt
from qfluentwidgets import CardWidget, FluentIcon as FIF
from ..i18n.translator import tr


class DropArea(CardWidget):
    """A card that accepts file drops and click-to-select."""

    filesDropped = Signal(list)
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(168)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(6)

        self.iconLabel = QLabel()
        self.iconLabel.setPixmap(FIF.FOLDER.icon().pixmap(46, 46))
        self.iconLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.titleLabel = QLabel(tr("convert.drop.title"))
        self.titleLabel.setObjectName("dropTitle")
        self.titleLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hintLabel = QLabel(tr("convert.drop.hint"))
        self.hintLabel.setObjectName("dropHint")
        self.hintLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.formatsLabel = QLabel(tr("convert.drop.formats"))
        self.formatsLabel.setObjectName("dropFormats")
        self.formatsLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self.iconLabel)
        layout.addWidget(self.titleLabel)
        layout.addWidget(self.hintLabel)
        layout.addWidget(self.formatsLabel)

    # -- drag & drop ------------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setProperty("drop", "hover")
            self.style().polish(self)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.setProperty("drop", "")
        self.style().polish(self)

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        if paths:
            self.filesDropped.emit(paths)
        event.acceptProposedAction()
        self.setProperty("drop", "")
        self.style().polish(self)

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)

    def retranslate(self):
        self.titleLabel.setText(tr("convert.drop.title"))
        self.hintLabel.setText(tr("convert.drop.hint"))
        self.formatsLabel.setText(tr("convert.drop.formats"))
