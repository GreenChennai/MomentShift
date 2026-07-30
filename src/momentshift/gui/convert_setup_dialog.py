"""Convert setup dialog (v0.2.7, #4).

After the user picks files in the Convert screen, this 800x500 popup opens with
the three pieces that used to live on the main screen:

- *Pending files*     — the selected files (removable before confirming)
- *Target format*     — a FormatGrid (one format per media category)
- *Advanced settings* — an AdvancedPanel (per-category ffmpeg options)

Confirming groups the files by category and pushes them (with the chosen format
and the current advanced options) straight into the main conversion queue via
``ConversionManager.add_files``. The main screen keeps only the input card and
the conversion queue (the original UI components, per the redesign spec).
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame, QDialog,
)

from qfluentwidgets import FluentIcon as FIF, isDarkTheme

from ..core.config import cfg
from ..core.presets import guess_category
from ..i18n.translator import tr
from .theme import (
    ThemedCard, primary_btn, ghost_btn, icon_btn,
    muted_text, sub_text, surface, scrollbar_qss,
)
from .format_grid import FormatGrid
from .advanced_panel import AdvancedPanel


class ConvertSetupDialog(QDialog):
    """Staging + format + advanced options in one modal popup."""

    def __init__(self, parent, manager, paths, selection, gpu_enabled_fn):
        super().__init__(parent)
        self.manager = manager
        self._paths = list(paths)
        self._selection = dict(selection)
        self._gpu = gpu_enabled_fn

        self.setWindowTitle(tr("convert.setup.title"))
        self.resize(800, 500)
        self.setMinimumSize(680, 440)
        self.setObjectName("setupDlg")
        self.setStyleSheet(f"#setupDlg {{ background-color: {surface().name()}; }}")

        self._build_ui()
        self._render_staging()

        cats = sorted({guess_category(p) for p in self._paths if guess_category(p)})
        self.formatGrid.setup(cats, self._selection)
        self.advancedPanel.refresh(cats)
        self._update_confirm()

    # -- construction ----------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # header
        title = QLabel(tr("convert.setup.title"))
        title.setStyleSheet(
            "font-size:18px; font-weight:700; color:%s;"
            % ("#1a1a1a" if not isDarkTheme() else "#e8e8e8")
        )
        root.addWidget(title)
        hint = QLabel(tr("convert.setup.hint"))
        hint.setStyleSheet(f"color:{muted_text()};")
        root.addWidget(hint)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color:{muted_text()};")
        root.addWidget(sep)

        # body split: left = staging, right = format + advanced
        body = QHBoxLayout()
        body.setSpacing(14)
        root.addLayout(body)

        # --- left: pending files ---------------------------------------
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(8)
        leftLbl = QLabel(tr("convert.setup.staging"))
        leftLbl.setStyleSheet("font-weight:700;")
        left.addWidget(leftLbl)
        self.stagingScroll = QScrollArea()
        self.stagingScroll.setWidgetResizable(True)
        self.stagingScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stagingScroll.setStyleSheet(
            f"QScrollArea{{border:none;background:transparent;}} {scrollbar_qss()}")
        self.stagingScroll.viewport().setStyleSheet("background:transparent;")
        self.stagingList = QWidget()
        self.stagingLayout = QVBoxLayout(self.stagingList)
        self.stagingLayout.setContentsMargins(0, 0, 0, 0)
        self.stagingLayout.setSpacing(6)
        self.stagingLayout.addStretch(1)
        self.stagingScroll.setWidget(self.stagingList)
        left.addWidget(self.stagingScroll, 1)
        leftCard = ThemedCard(self)
        leftCard.setFixedWidth(300)
        leftCard.setLayout(left)
        body.addWidget(leftCard)

        # --- right: format + advanced -----------------------------------
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(10)
        fmtLbl = QLabel(tr("convert.setup.format"))
        fmtLbl.setStyleSheet("font-weight:700;")
        right.addWidget(fmtLbl)
        self.formatGrid = FormatGrid(self)
        self.formatGrid.selectionChanged.connect(self._on_selection)
        right.addWidget(self.formatGrid)
        advLbl = QLabel(tr("convert.setup.advanced"))
        advLbl.setStyleSheet("font-weight:700;")
        right.addWidget(advLbl)
        self.advancedPanel = AdvancedPanel(self)
        right.addWidget(self.advancedPanel, 1)
        rightCard = ThemedCard(self)
        rightCard.setLayout(right)
        rightScroll = QScrollArea()
        rightScroll.setWidgetResizable(True)
        rightScroll.setWidget(rightCard)
        rightScroll.setStyleSheet(
            f"QScrollArea{{border:none;background:transparent;}} {scrollbar_qss()}")
        rightScroll.viewport().setStyleSheet("background:transparent;")
        body.addWidget(rightScroll, 1)

        # bottom bar
        bar = QHBoxLayout()
        bar.addStretch(1)
        self.cancelBtn = ghost_btn(tr("convert.setup.cancel"), icon=FIF.CLOSE)
        self.cancelBtn.clicked.connect(self.reject)
        self.confirmBtn = primary_btn(tr("convert.setup.confirm"), icon=FIF.UP)
        self.confirmBtn.clicked.connect(self._on_confirm)
        bar.addWidget(self.cancelBtn)
        bar.addWidget(self.confirmBtn)
        root.addLayout(bar)

    # -- staging list ----------------------------------------------------
    def _render_staging(self):
        while self.stagingLayout.count():
            item = self.stagingLayout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not self._paths:
            empty = QLabel(tr("convert.setup.empty"))
            empty.setStyleSheet(f"color:{muted_text()};")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.stagingLayout.insertWidget(0, empty)
            self.stagingLayout.addStretch(1)
            return
        for p in self._paths:
            row = QWidget()
            hb = QHBoxLayout(row)
            hb.setContentsMargins(6, 4, 6, 4)
            hb.setSpacing(8)
            name = QLabel(Path(p).name)
            name.setStyleSheet(f"color:{sub_text()};")
            name.setToolTip(p)
            hb.addWidget(name, 1)
            rm = icon_btn(FIF.DELETE, tr("convert.action.remove"))
            rm.setFixedSize(30, 30)
            rm.clicked.connect(lambda _, path=p: self._remove_staged(path))
            hb.addWidget(rm)
            self.stagingLayout.insertWidget(self.stagingLayout.count() - 1, row)
        self.stagingLayout.addStretch(1)

    def _remove_staged(self, path: str):
        if path in self._paths:
            self._paths.remove(path)
        self._render_staging()
        cats = sorted({guess_category(p) for p in self._paths if guess_category(p)})
        self.formatGrid.setup(cats, self._selection)
        self.advancedPanel.refresh(cats)
        self._update_confirm()

    # -- format selection ------------------------------------------------
    def _on_selection(self, selection: dict):
        self._selection.update(selection)

    def get_selection(self) -> dict:
        return dict(self._selection)

    def _update_confirm(self):
        self.confirmBtn.setEnabled(bool(self._paths))

    # -- confirm ----------------------------------------------------------
    def _on_confirm(self):
        if not self._paths:
            return
        mode = cfg.outputMode.value
        suffix = cfg.outputSuffix.value
        folder = cfg.outputFolder.value or ""
        by_cat: dict[str, list[str]] = {}
        for p in self._paths:
            c = guess_category(p)
            if c:
                by_cat.setdefault(c, []).append(p)
        gpu = self._gpu()
        for cat, paths in by_cat.items():
            fmt = self.formatGrid.get_selection().get(cat)
            if not fmt:
                continue
            self.manager.add_files(
                paths, fmt, folder if mode == "fixed" else None,
                gpu, mode, suffix,
            )
        self.accept()
