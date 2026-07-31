"""Advanced, per-category conversion options panel (Convert screen).

Mutates ``core.advanced.adv`` in place — the same live store the engine reads when
building commands. Public API kept for ``convert_interface``:

- ``AdvancedPanel(parent)``
- ``refresh(categories)``
- ``retranslate()`` / ``retheme()``
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPixmap, QTransform
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider

from qfluentwidgets import FluentIcon as FIF, ComboBox, SwitchButton, CaptionLabel

from ..core import advanced
from ..core.qt_compat import Signal
from ..i18n.translator import tr
from .theme import field_row, muted_text


# --------------------------------------------------------------------------
# Expandable section
# --------------------------------------------------------------------------
class _Header(QWidget):
    clicked = Signal()

    def __init__(self, title: str, expanded: bool = True, parent=None):
        super().__init__(parent)
        self._title = title
        self._expanded = expanded
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        hb = QHBoxLayout(self)
        hb.setContentsMargins(0, 4, 0, 4)
        hb.setSpacing(8)
        self.titleLbl = CaptionLabel(title)
        self.titleLbl.setStyleSheet("font-weight:700; color:#1a1a1a;" if not False
                                    else "font-weight:700; color:#e8e8e8;")
        hb.addWidget(self.titleLbl)
        hb.addStretch(1)
        self.chevron = QLabel()
        self._paint_chevron()
        hb.addWidget(self.chevron)

    def _paint_chevron(self):
        color = QColor(120, 120, 120) if not False else QColor(170, 170, 170)
        pix = FIF.CHEVRON_RIGHT.icon(color).pixmap(16, 16)
        if self._expanded:
            pix = pix.transformed(QTransform().rotate(90))
        self.chevron.setPixmap(pix)

    def set_expanded(self, b: bool):
        self._expanded = b
        self._paint_chevron()

    def set_title(self, t: str):
        self._title = t
        self.titleLbl.setText(t)

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)

    def retheme(self):
        self._paint_chevron()
        self.titleLbl.setStyleSheet("font-weight:700; color:#1a1a1a;" if not False
                                    else "font-weight:700; color:#e8e8e8;")


class ExpandWidget(QWidget):
    def __init__(self, title: str, parent=None, expanded: bool = True):
        super().__init__(parent)
        self._expanded = expanded
        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(6)
        self.header = _Header(title, expanded)
        self.header.clicked.connect(self.toggle)
        vb.addWidget(self.header)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(4, 4, 4, 4)
        self.body_layout.setSpacing(10)
        self.body.setVisible(expanded)
        vb.addWidget(self.body)

    def toggle(self):
        self.set_expanded(not self._expanded)

    def set_expanded(self, b: bool):
        self._expanded = b
        self.body.setVisible(b)
        self.header.set_expanded(b)

    def retheme(self):
        self.header.retheme()


# --------------------------------------------------------------------------
# Panel
# --------------------------------------------------------------------------
class AdvancedPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._categories: list[str] = []
        self.vbox = QVBoxLayout(self)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(10)
        self._expanders: list[ExpandWidget] = []

    # -- build ------------------------------------------------------------
    def refresh(self, categories: list[str]):
        self._clear()
        self._categories = list(categories)
        if "image" in categories:
            self._add_image()
        if "video" in categories:
            self._add_video()
        if "audio" in categories:
            self._add_audio()
        self.vbox.addStretch(1)

    def _add_expander(self, title_key: str, builder) -> ExpandWidget:
        ex = ExpandWidget(tr(title_key))
        builder(ex.body_layout)
        self.vbox.addWidget(ex)
        return ex

    def _add_image(self):
        """v0.4.2：图像压缩参数直接展开（无二级折叠）。"""
        adv = advanced.adv["image"]
        if not isinstance(adv.get("compress"), dict):
            adv["compress"] = {"backend": "oxipng", "level": 3, "interlace": False,
                               "strip": "safe", "quality": 95}
        comp = adv["compress"]

        # 压缩后端
        backend = _combo(
            [(tr("advanced.compression.oxipng"), "oxipng"),
             (tr("advanced.compression.imagecodecs"), "imagecodecs"),
             (tr("advanced.compression.pillow"), "pillow")],
            comp.get("backend", "oxipng"),
            lambda v: comp.__setitem__("backend", v),
        )
        self.vbox.addWidget(field_row(tr("advanced.compression.backend"), backend, label_width=80))

        # 质量滑块
        q = QSlider(Qt.Orientation.Horizontal)
        q.setRange(1, 100); q.setValue(int(comp.get("quality", 95)))
        q.valueChanged.connect(lambda v: comp.__setitem__("quality", v))
        self.vbox.addWidget(field_row(tr("advanced.quality"), q, label_width=80))

        # oxipng 专用参数
        oxi_grp = QWidget()
        oxi_grp.setStyleSheet("background: transparent;")
        oxi_l = QVBoxLayout(oxi_grp)
        oxi_l.setContentsMargins(0, 0, 0, 0); oxi_l.setSpacing(6)
        lvl = QSlider(Qt.Orientation.Horizontal)
        lvl.setRange(0, 6); lvl.setValue(int(comp.get("level", 3)))
        lvl_label = QLabel(str(comp.get("level", 3)))
        lvl.valueChanged.connect(lambda v: (comp.__setitem__("level", v), lvl_label.setText(str(v))))
        row = QHBoxLayout(); row.addWidget(lvl_label); row.addWidget(lvl, 1)
        oxi_l.addWidget(field_row(tr("advanced.level"), row))
        inter = SwitchButton(tr("advanced.interlace"))
        inter.setChecked(bool(comp.get("interlace", False)))
        inter.checkedChanged.connect(lambda b: comp.__setitem__("interlace", b))
        oxi_l.addWidget(field_row(tr("advanced.interlace"), inter))
        strip = _combo(
            [(tr("advanced.strip.safe"), "safe"), (tr("advanced.strip.all"), "all")],
            comp.get("strip", "safe"), lambda v: comp.__setitem__("strip", v))
        oxi_l.addWidget(field_row(tr("advanced.strip"), strip))
        self.vbox.addWidget(oxi_grp)
        oxi_grp._oxi_grp = True  # 标记用于 retheme

        def _on_backend_change(val: str):
            oxi_grp.setVisible(val == "oxipng")
        _on_backend_change(comp.get("backend", "oxipng"))
        backend.currentTextChanged.connect(
            lambda t: _on_backend_change(backend._mapping.get(t, t)))


    def _add_video(self):
        """v0.4.2：视频参数直接展开。"""
        adv = advanced.adv["video"]
        res = _combo(_opt_list(advanced.RESOLUTIONS),
                     adv.get("resolution", "original"),
                     lambda v: adv.__setitem__("resolution", v))
        self.vbox.addWidget(field_row(tr("advanced.resolution"), res, label_width=80))
        fps = _combo(_opt_list(advanced.FPS_OPTIONS),
                     adv.get("fps", "original"),
                     lambda v: adv.__setitem__("fps", v))
        self.vbox.addWidget(field_row(tr("advanced.fps"), fps, label_width=80))
        br = _combo(_opt_list(advanced.VIDEO_BITRATES),
                    adv.get("bitrate", "original"),
                    lambda v: adv.__setitem__("bitrate", v))
        self.vbox.addWidget(field_row(tr("advanced.bitrate"), br, label_width=80))
        codec = _combo(
            [(tr("advanced.original"), "original"),
             ("H.264", "H.264"), ("H.265", "H.265"), ("copy", "copy")],
            adv.get("codec", "original"),
            lambda v: adv.__setitem__("codec", v),
        )
        self.vbox.addWidget(field_row(tr("advanced.codec"), codec, label_width=80))
        merge = SwitchButton(tr("advanced.merge"))
        merge.setChecked(bool(adv.get("merge", False)))
        merge.checkedChanged.connect(lambda b: adv.__setitem__("merge", b))
        self.vbox.addWidget(field_row(tr("advanced.merge"), merge, label_width=80))

    def _add_audio(self):
        """v0.4.2：音频参数直接展开。"""
        adv = advanced.adv["audio"]
        br = _combo(_opt_list(advanced.AUDIO_BITRATES),
                    adv.get("bitrate", "original"),
                    lambda v: adv.__setitem__("bitrate", v))
        self.vbox.addWidget(field_row(tr("advanced.bitrate"), br, label_width=80))
        sr = _combo(_opt_list(advanced.SAMPLE_RATES),
                    adv.get("sample_rate", "original"),
                    lambda v: adv.__setitem__("sample_rate", v))
        self.vbox.addWidget(field_row(tr("advanced.sample_rate"), sr, label_width=80))
        ch = _combo(
            [(tr("advanced.original"), "original"),
             (tr("advanced.channels.stereo"), "stereo"),
             (tr("advanced.channels.mono"), "mono")],
            adv.get("channels", "original"),
            lambda v: adv.__setitem__("channels", v),
        )
        self.vbox.addWidget(field_row(tr("advanced.channels"), ch, label_width=80))
        merge = SwitchButton(tr("advanced.merge"))
        merge.setChecked(bool(adv.get("merge", False)))
        merge.checkedChanged.connect(lambda b: adv.__setitem__("merge", b))
        self.vbox.addWidget(field_row(tr("advanced.merge"), merge, label_width=80))

    # -- updates ----------------------------------------------------------
    def get_args(self, category: str, target: str = "") -> list[str]:
        """Return ffmpeg CLI args for ``category`` based on current panel state."""
        return advanced.build_advanced_args(category, target, advanced.get(category))

    def retranslate(self):
        self.refresh(self._categories)

    def retheme(self):
        for ex in self._expanders:
            ex.retheme()

    def _clear(self):
        from PyQt6.QtWidgets import QLayout

        while self.vbox.count():
            item = self.vbox.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
            lay = item.layout()
            if lay:
                _clear_layout(lay)
                lay.deleteLater()
        self._expanders.clear()


def _opt_list(values: list[str]):
    return [(tr("advanced.original") if v == "original" else v, v) for v in values]


def _combo(mapping: list[tuple[str, str]], current, on_change) -> ComboBox:
    combo = ComboBox()
    for display, value in mapping:
        combo.addItem(display)
    for i, (display, value) in enumerate(mapping):
        if value == current:
            combo.setCurrentIndex(i)
            break
    combo._mapping = dict(mapping)
    combo.currentTextChanged.connect(lambda t: on_change(combo._mapping.get(t, t)))
    return combo


def _clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w:
            w.deleteLater()
