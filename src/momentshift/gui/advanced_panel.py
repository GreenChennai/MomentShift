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

from qfluentwidgets import FluentIcon as FIF, ComboBox, SwitchButton, CaptionLabel, isDarkTheme

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
        self.titleLbl.setStyleSheet("font-weight:700; color:#1a1a1a;" if not isDarkTheme()
                                    else "font-weight:700; color:#e8e8e8;")
        hb.addWidget(self.titleLbl)
        hb.addStretch(1)
        self.chevron = QLabel()
        self._paint_chevron()
        hb.addWidget(self.chevron)

    def _paint_chevron(self):
        color = QColor(120, 120, 120) if not isDarkTheme() else QColor(170, 170, 170)
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
        self.titleLbl.setStyleSheet("font-weight:700; color:#1a1a1a;" if not isDarkTheme()
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
            self._expanders.append(self._add_image())
        if "video" in categories:
            self._expanders.append(self._add_video())
        if "audio" in categories:
            self._expanders.append(self._add_audio())
        self.vbox.addStretch(1)

    def _add_expander(self, title_key: str, builder) -> ExpandWidget:
        ex = ExpandWidget(tr(title_key))
        builder(ex.body_layout)
        self.vbox.addWidget(ex)
        return ex

    def _add_image(self):
        adv = advanced.adv["image"]

        def build(layout):
            # --- Compression backend dropdown ---
            backend = _combo(
                [(tr("advanced.compression.pillow"), "pillow"),
                 ("oxipng", "oxipng"),
                 ("OptiPNG", "optipng"),
                 ("Mozilla JPEG", "mozjpeg")],
                adv.get("compress_backend", "pillow"),
                lambda v: adv.__setitem__("compress_backend", v),
            )
            layout.addWidget(field_row(tr("advanced.compression.backend"), backend, label_width=80))

            # --- FFmpeg quality group ---
            ffmpeg_group = QWidget()
            fq = QVBoxLayout(ffmpeg_group)
            fq.setContentsMargins(0, 0, 0, 0)
            fq.setSpacing(6)

            q = QSlider(Qt.Orientation.Horizontal)
            q.setRange(1, 100)
            q.setValue(int(adv.get("quality", 100)))
            q.valueChanged.connect(lambda v: adv.__setitem__("quality", v))
            fq.addWidget(field_row(tr("advanced.quality"), q, label_width=80))

            # PNG format override
            png_ex = ExpandWidget(tr("advanced.png.options"))
            png_lvl = QSlider(Qt.Orientation.Horizontal)
            png_lvl.setRange(0, 9)
            png_val = adv.get("png_quality")
            png_lvl.setValue(int(png_val) if png_val is not None else 9)
            png_lvl.valueChanged.connect(lambda v: adv.__setitem__("png_quality", v))
            png_enable = SwitchButton(tr("advanced.enable"))
            png_enable.setChecked(png_val is not None)
            png_enable.checkedChanged.connect(
                lambda b: adv.__setitem__("png_quality", png_lvl.value() if b else None)
            )
            png_lvl.setEnabled(png_val is not None)
            png_enable.checkedChanged.connect(lambda b: png_lvl.setEnabled(b))
            png_label = QLabel(f"{png_lvl.value()}")
            png_lvl.valueChanged.connect(lambda v: png_label.setText(str(v)))
            png_row = QHBoxLayout()
            png_row.addWidget(png_label)
            png_row.addWidget(png_lvl, 1)
            png_row.addWidget(png_enable)
            png_ex.body_layout.addWidget(
                field_row(tr("advanced.png.compression"), png_row, label_width=80)
            )
            fq.addWidget(png_ex)

            # JPG format override
            jpg_ex = ExpandWidget(tr("advanced.jpg.options"))
            jpg_q = QSlider(Qt.Orientation.Horizontal)
            jpg_q.setRange(1, 100)
            jpg_val = adv.get("jpg_quality")
            jpg_q.setValue(int(jpg_val) if jpg_val is not None else 95)
            jpg_q.valueChanged.connect(lambda v: adv.__setitem__("jpg_quality", v))
            jpg_enable = SwitchButton(tr("advanced.enable"))
            jpg_enable.setChecked(jpg_val is not None)
            jpg_enable.checkedChanged.connect(
                lambda b: adv.__setitem__("jpg_quality", jpg_q.value() if b else None)
            )
            jpg_q.setEnabled(jpg_val is not None)
            jpg_enable.checkedChanged.connect(lambda b: jpg_q.setEnabled(b))
            jpg_labelx = QLabel(f"{jpg_q.value()}")
            jpg_q.valueChanged.connect(lambda v: jpg_labelx.setText(str(v)))
            jpg_row = QHBoxLayout()
            jpg_row.addWidget(jpg_labelx)
            jpg_row.addWidget(jpg_q, 1)
            jpg_row.addWidget(jpg_enable)
            jpg_ex.body_layout.addWidget(
                field_row(tr("advanced.jpg.quality"), jpg_row, label_width=80)
            )
            fq.addWidget(jpg_ex)

            # WebP format override
            webp_ex = ExpandWidget(tr("advanced.webp.options"))
            webp_q = QSlider(Qt.Orientation.Horizontal)
            webp_q.setRange(1, 100)
            webp_val = adv.get("webp_quality")
            webp_q.setValue(int(webp_val) if webp_val is not None else 90)
            webp_q.valueChanged.connect(lambda v: adv.__setitem__("webp_quality", v))
            webp_enable = SwitchButton(tr("advanced.enable"))
            webp_enable.setChecked(webp_val is not None)
            webp_enable.checkedChanged.connect(
                lambda b: adv.__setitem__("webp_quality", webp_q.value() if b else None)
            )
            webp_q.setEnabled(webp_val is not None)
            webp_enable.checkedChanged.connect(lambda b: webp_q.setEnabled(b))
            webp_labelx = QLabel(f"{webp_q.value()}")
            webp_q.valueChanged.connect(lambda v: webp_labelx.setText(str(v)))
            webp_row = QHBoxLayout()
            webp_row.addWidget(webp_labelx)
            webp_row.addWidget(webp_q, 1)
            webp_row.addWidget(webp_enable)
            webp_ex.body_layout.addWidget(
                field_row(tr("advanced.webp.quality"), webp_row, label_width=80)
            )
            fq.addWidget(webp_ex)

            layout.addWidget(ffmpeg_group)

            # --- Tool-specific parameter groups ---
            oxi = self._oxipng_ex(adv)
            opti = self._optipng_ex(adv)
            moz = self._mozjpeg_ex(adv)
            layout.addWidget(oxi)
            layout.addWidget(opti)
            layout.addWidget(moz)

            # --- Show / hide based on selected backend ---
            def _on_backend_change(val: str):
                ffmpeg_group.setVisible(val == "ffmpeg")
                oxi.setVisible(val == "oxipng")
                opti.setVisible(val == "optipng")
                moz.setVisible(val == "mozjpeg")

            _on_backend_change(adv.get("compress_backend", "ffmpeg"))
            backend.currentTextChanged.connect(
                lambda t: _on_backend_change(backend._mapping.get(t, t))
            )

        return self._add_expander("advanced.image.title", build)

    def _oxipng_ex(self, adv):
        grp = adv.setdefault("png_oxipng", {})
        ex = ExpandWidget(tr("advanced.oxipng"))
        lvl = QSlider(Qt.Orientation.Horizontal)
        lvl.setRange(0, 6)
        lvl.setValue(int(grp.get("level", 2)))
        lvl.valueChanged.connect(lambda v: grp.__setitem__("level", v))
        ex.body_layout.addWidget(field_row(tr("advanced.level"), lvl, label_width=80))
        inter = SwitchButton(tr("advanced.interlace"))
        inter.setChecked(bool(grp.get("interlace", False)))
        inter.checkedChanged.connect(lambda b: grp.__setitem__("interlace", b))
        ex.body_layout.addWidget(field_row(tr("advanced.interlace"), inter, label_width=80))
        strip = _combo([(tr("advanced.strip.safe"), "safe"),
                        (tr("advanced.strip.all"), "all")],
                       grp.get("strip", "safe"),
                       lambda v: grp.__setitem__("strip", v))
        ex.body_layout.addWidget(field_row(tr("advanced.strip"), strip, label_width=80))
        return ex

    def _optipng_ex(self, adv):
        grp = adv.setdefault("png_optipng", {})
        ex = ExpandWidget(tr("advanced.optipng"))
        lvl = QSlider(Qt.Orientation.Horizontal)
        lvl.setRange(0, 7)
        lvl.setValue(int(grp.get("level", 2)))
        lvl.valueChanged.connect(lambda v: grp.__setitem__("level", v))
        ex.body_layout.addWidget(field_row(tr("advanced.level"), lvl, label_width=80))
        strip = _combo([(tr("advanced.strip.all"), "all"),
                        (tr("advanced.strip.safe"), "safe")],
                       grp.get("strip", "all"),
                       lambda v: grp.__setitem__("strip", v))
        ex.body_layout.addWidget(field_row(tr("advanced.strip"), strip, label_width=80))
        return ex

    def _mozjpeg_ex(self, adv):
        grp = adv.setdefault("jpg_mozjpeg", {})
        ex = ExpandWidget(tr("advanced.mozjpeg"))
        q = QSlider(Qt.Orientation.Horizontal)
        q.setRange(1, 100)
        q.setValue(int(grp.get("quality", 100)))
        q.valueChanged.connect(lambda v: grp.__setitem__("quality", v))
        ex.body_layout.addWidget(field_row(tr("advanced.quality"), q, label_width=80))
        prog = SwitchButton(tr("advanced.progressive"))
        prog.setChecked(bool(grp.get("progressive", True)))
        prog.checkedChanged.connect(lambda b: grp.__setitem__("progressive", b))
        ex.body_layout.addWidget(field_row(tr("advanced.progressive"), prog, label_width=80))
        stripx = SwitchButton(tr("advanced.strip"))
        stripx.setChecked(bool(grp.get("strip", True)))
        stripx.checkedChanged.connect(lambda b: grp.__setitem__("strip", b))
        ex.body_layout.addWidget(field_row(tr("advanced.strip"), stripx, label_width=80))
        arith = SwitchButton(tr("advanced.arithmetic"))
        arith.setChecked(bool(grp.get("arithmetic", False)))
        arith.checkedChanged.connect(lambda b: grp.__setitem__("arithmetic", b))
        ex.body_layout.addWidget(field_row(tr("advanced.arithmetic"), arith, label_width=80))
        return ex

    def _add_video(self):
        adv = advanced.adv["video"]

        def build(layout):
            res = _combo(_opt_list(advanced.RESOLUTIONS),
                         adv.get("resolution", "original"),
                         lambda v: adv.__setitem__("resolution", v))
            layout.addWidget(field_row(tr("advanced.resolution"), res, label_width=80))
            fps = _combo(_opt_list(advanced.FPS_OPTIONS),
                         adv.get("fps", "original"),
                         lambda v: adv.__setitem__("fps", v))
            layout.addWidget(field_row(tr("advanced.fps"), fps, label_width=80))
            br = _combo(_opt_list(advanced.VIDEO_BITRATES),
                        adv.get("bitrate", "original"),
                        lambda v: adv.__setitem__("bitrate", v))
            layout.addWidget(field_row(tr("advanced.bitrate"), br, label_width=80))
            codec = _combo(
                [(tr("advanced.original"), "original"),
                 ("H.264", "H.264"), ("H.265", "H.265"), ("copy", "copy")],
                adv.get("codec", "original"),
                lambda v: adv.__setitem__("codec", v),
            )
            layout.addWidget(field_row(tr("advanced.codec"), codec, label_width=80))
            merge = SwitchButton(tr("advanced.merge"))
            merge.setChecked(bool(adv.get("merge", False)))
            merge.checkedChanged.connect(lambda b: adv.__setitem__("merge", b))
            layout.addWidget(field_row(tr("advanced.merge"), merge, label_width=80))

        return self._add_expander("advanced.video.title", build)

    def _add_audio(self):
        adv = advanced.adv["audio"]

        def build(layout):
            br = _combo(_opt_list(advanced.AUDIO_BITRATES),
                        adv.get("bitrate", "original"),
                        lambda v: adv.__setitem__("bitrate", v))
            layout.addWidget(field_row(tr("advanced.bitrate"), br, label_width=80))
            sr = _combo(_opt_list(advanced.SAMPLE_RATES),
                        adv.get("sample_rate", "original"),
                        lambda v: adv.__setitem__("sample_rate", v))
            layout.addWidget(field_row(tr("advanced.sample_rate"), sr, label_width=80))
            ch = _combo(
                [(tr("advanced.original"), "original"),
                 (tr("advanced.channels.stereo"), "stereo"),
                 (tr("advanced.channels.mono"), "mono")],
                adv.get("channels", "original"),
                lambda v: adv.__setitem__("channels", v),
            )
            layout.addWidget(field_row(tr("advanced.channels"), ch, label_width=80))
            merge = SwitchButton(tr("advanced.merge"))
            merge.setChecked(bool(adv.get("merge", False)))
            merge.checkedChanged.connect(lambda b: adv.__setitem__("merge", b))
            layout.addWidget(field_row(tr("advanced.merge"), merge, label_width=80))

        return self._add_expander("advanced.audio.title", build)

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
