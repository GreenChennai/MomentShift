"""Expandable "advanced settings" panel shown inside the staging (待处理文件) card.

It exposes per-category conversion knobs that feed ``core.advanced`` (which in turn
shapes the ffmpeg command). The top "高级设置" expands to reveal one sub-panel per
media category that is currently present in the staging list.
"""

from __future__ import annotations

from ..core.qt_compat import QWidget, QVBoxLayout, QHBoxLayout, QLabel, Signal, Qt
from qfluentwidgets import (
    StrongBodyLabel,
    ComboBox,
    SwitchButton,
    Slider,
    BodyLabel,
    CaptionLabel,
)
from ..core import advanced
from ..core import compressor
from ..i18n.translator import tr
from .theme import sub_text

CATEGORY_PANEL_TITLE = {
    "image": "convert.advanced.image",
    "video": "convert.advanced.video",
    "audio": "convert.advanced.audio",
}


class _Header(QWidget):
    """Clickable header that toggles an :class:`ExpandWidget`."""

    def __init__(self, title: str, expanded: bool, parent=None):
        super().__init__(parent)
        self._expanded = expanded
        h = QHBoxLayout(self)
        h.setContentsMargins(4, 6, 4, 6)
        h.setSpacing(8)
        self.chevron = QLabel("▾" if expanded else "▸")
        self.set_expanded_look()
        self.titleLabel = StrongBodyLabel(title)
        h.addWidget(self.chevron)
        h.addWidget(self.titleLabel)
        h.addStretch(1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_expanded_look(self) -> None:
        self.chevron.setStyleSheet(f"color: {sub_text()}; background-color: transparent; font-size: 12px;")

    def retheme(self) -> None:
        self.set_expanded_look()

    def set_expanded(self, v: bool) -> None:
        self._expanded = v
        self.chevron.setText("▾" if v else "▸")


class ExpandWidget(QWidget):
    """A collapsible container with a clickable header."""

    def __init__(self, title: str, parent=None, expanded: bool = True):
        super().__init__(parent)
        self.header = _Header(title, expanded)
        self.body = QWidget()
        self.bodyLayout = QVBoxLayout(self.body)
        self.bodyLayout.setContentsMargins(10, 6, 10, 10)
        self.bodyLayout.setSpacing(10)
        self.body.setVisible(expanded)

        self.header.mousePressEvent = lambda e: self.toggle()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.header)
        outer.addWidget(self.body)

    @property
    def body_layout(self) -> QVBoxLayout:
        return self.bodyLayout

    def toggle(self) -> None:
        self.set_expanded(not self.body.isVisible())

    def set_expanded(self, v: bool) -> None:
        self.body.setVisible(v)
        self.header.set_expanded(v)


class AdvancedPanel(QWidget):
    """Top-level "高级设置" expander holding per-category sub-panels."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._expand = ExpandWidget(tr("convert.advanced.title"), expanded=True)
        self._cat_panels: dict[str, ExpandWidget] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        outer.addWidget(self._expand)

    # -- public -----------------------------------------------------------
    def refresh(self, categories: list[str]) -> None:
        """Rebuild the per-category sub-panels for the given categories."""
        for w in self._cat_panels.values():
            self._expand.body_layout.removeWidget(w)
            w.deleteLater()
        self._cat_panels = {}

        for cat in categories:
            panel = self._build_category_panel(cat)
            self._cat_panels[cat] = panel
            self._expand.body_layout.addWidget(panel)

    # -- builders ---------------------------------------------------------
    @staticmethod
    def _row(label: str, control: QWidget) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        lab = BodyLabel(label)
        lab.setFixedWidth(72)
        h.addWidget(lab)
        h.addWidget(control, 1)
        return row

    def _build_category_panel(self, cat: str) -> ExpandWidget:
        panel = ExpandWidget(tr(CATEGORY_PANEL_TITLE.get(cat, cat)), expanded=True)
        opt = advanced.get(cat)

        if cat == "image":
            slider = Slider(Qt.Orientation.Horizontal)
            slider.setRange(1, 100)
            slider.setValue(int(opt.get("quality", 100)))
            val_label = CaptionLabel(f"{slider.value()}")
            val_label.setFixedWidth(34)
            slider.valueChanged.connect(
                lambda v: (advanced.adv[cat].__setitem__("quality", v),
                           val_label.setText(str(v)))
            )
            qrow = QWidget()
            qh = QHBoxLayout(qrow)
            qh.setContentsMargins(0, 0, 0, 0)
            qh.setSpacing(10)
            qh.addWidget(slider, 1)
            qh.addWidget(val_label)
            panel.body_layout.addWidget(self._row(tr("convert.advanced.quality"), qrow))
            panel.body_layout.addWidget(
                CaptionLabel(tr("convert.advanced.quality_hint"))
            )
            # --- image compression controls (feature: Compress) ---
            panel.body_layout.addWidget(self._build_image_compress(cat))

        elif cat == "video":
            panel.body_layout.addWidget(self._combo_row(
                cat, "resolution", tr("convert.advanced.resolution"), advanced.RESOLUTIONS))
            panel.body_layout.addWidget(self._combo_row(
                cat, "fps", tr("convert.advanced.fps"), advanced.FPS_OPTIONS))
            panel.body_layout.addWidget(self._combo_row(
                cat, "bitrate", tr("convert.advanced.vbitrate"), advanced.VIDEO_BITRATES))
            panel.body_layout.addWidget(self._switch_row(
                cat, "merge", tr("convert.advanced.merge")))

        elif cat == "audio":
            panel.body_layout.addWidget(self._combo_row(
                cat, "bitrate", tr("convert.advanced.abitrate"), advanced.AUDIO_BITRATES))
            panel.body_layout.addWidget(self._switch_row(
                cat, "merge", tr("convert.advanced.merge")))

        return panel

    def _build_image_compress(self, cat: str) -> QWidget:
        """Compression controls for the image advanced panel.

        Adds: a compress on/off switch, a lossless/lossy mode selector, a backend
        picker (auto + whatever is installed), and a nested expander exposing the
        per-backend tuning knobs (oxipng / OptiPNG / mozjpeg).
        """
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        img = advanced.adv[cat]

        # compress on/off
        sw = SwitchButton()
        sw.setChecked(bool(img.get("compress", True)))
        sw.checkedChanged.connect(lambda b: advanced.adv[cat].__setitem__("compress", bool(b)))
        v.addWidget(self._row(tr("compress.advanced.enable"), sw))

        # mode (lossless / lossy)
        mode_combo = ComboBox()
        for text, val in ((tr("compress.mode.lossless"), "lossless"),
                          (tr("compress.mode.lossy"), "lossy")):
            mode_combo.addItem(text, userData=val)
        for i in range(mode_combo.count()):
            mode_combo.setCurrentIndex(i)
            if mode_combo.currentData() == img.get("compress_mode", "lossless"):
                break
        mode_combo.currentIndexChanged.connect(
            lambda _i, cb=mode_combo: advanced.adv[cat].__setitem__("compress_mode", cb.currentData())
        )
        v.addWidget(self._row(tr("compress.mode"), mode_combo))

        # backend picker
        backend_combo = ComboBox()
        backend_combo.addItem(tr("compress.backend.auto"), userData="auto")
        for bid, b in compressor.available_backends().items():
            backend_combo.addItem(b["name"], userData=bid)
        for i in range(backend_combo.count()):
            backend_combo.setCurrentIndex(i)
            if backend_combo.currentData() == img.get("compress_backend", "auto"):
                break
        backend_combo.currentIndexChanged.connect(
            lambda _i, cb=backend_combo: advanced.adv[cat].__setitem__("compress_backend", cb.currentData())
        )
        v.addWidget(self._row(tr("compress.backend"), backend_combo))

        # per-backend advanced params
        exp = ExpandWidget(tr("compress.advanced.params"), expanded=False)
        exp.body_layout.addWidget(self._oxipng_params())
        exp.body_layout.addWidget(self._optipng_params())
        exp.body_layout.addWidget(self._mozjpeg_params())
        v.addWidget(exp)
        return container

    def _oxipng_params(self) -> QWidget:
        grp = QWidget()
        v = QVBoxLayout(grp)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(BodyLabel(tr("compress.advanced.oxipng")))
        opt = advanced.adv["image"]["png_oxipng"]

        lvl = Slider(Qt.Orientation.Horizontal)
        lvl.setRange(0, 6)
        lvl.setValue(int(opt.get("level", 2)))
        lvl_val = CaptionLabel(str(lvl.value()))
        lvl_val.setFixedWidth(34)
        lvl.valueChanged.connect(
            lambda val: (opt.__setitem__("level", val), lvl_val.setText(str(val)))
        )
        lr = QWidget(); lrh = QHBoxLayout(lr); lrh.setContentsMargins(0,0,0,0)
        lrh.setSpacing(10); lrh.addWidget(lvl, 1); lrh.addWidget(lvl_val)
        v.addWidget(self._row(tr("compress.advanced.level"), lr))

        inter = SwitchButton(); inter.setChecked(bool(opt.get("interlace", False)))
        inter.checkedChanged.connect(lambda b: opt.__setitem__("interlace", bool(b)))
        v.addWidget(self._row(tr("compress.advanced.interlace"), inter))

        strip = ComboBox()
        for t in ("safe", "all"):
            strip.addItem(t, userData=t)
        for i in range(strip.count()):
            strip.setCurrentIndex(i)
            if strip.currentData() == opt.get("strip", "safe"):
                break
        strip.currentIndexChanged.connect(lambda _i, cb=strip: opt.__setitem__("strip", cb.currentData()))
        v.addWidget(self._row(tr("compress.advanced.strip"), strip))
        return grp

    def _optipng_params(self) -> QWidget:
        grp = QWidget()
        v = QVBoxLayout(grp)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(BodyLabel(tr("compress.advanced.optipng")))
        opt = advanced.adv["image"]["png_optipng"]

        lvl = Slider(Qt.Orientation.Horizontal)
        lvl.setRange(0, 7)
        lvl.setValue(int(opt.get("level", 2)))
        lvl_val = CaptionLabel(str(lvl.value()))
        lvl_val.setFixedWidth(34)
        lvl.valueChanged.connect(
            lambda val: (opt.__setitem__("level", val), lvl_val.setText(str(val)))
        )
        lr = QWidget(); lrh = QHBoxLayout(lr); lrh.setContentsMargins(0,0,0,0)
        lrh.setSpacing(10); lrh.addWidget(lvl, 1); lrh.addWidget(lvl_val)
        v.addWidget(self._row(tr("compress.advanced.level"), lr))

        strip = ComboBox()
        for t in ("all", "safe", "none"):
            strip.addItem(t, userData=t)
        for i in range(strip.count()):
            strip.setCurrentIndex(i)
            if strip.currentData() == opt.get("strip", "all"):
                break
        strip.currentIndexChanged.connect(lambda _i, cb=strip: opt.__setitem__("strip", cb.currentData()))
        v.addWidget(self._row(tr("compress.advanced.strip"), strip))
        return grp

    def _mozjpeg_params(self) -> QWidget:
        grp = QWidget()
        v = QVBoxLayout(grp)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(BodyLabel(tr("compress.advanced.mozjpeg")))
        opt = advanced.adv["image"]["jpg_mozjpeg"]

        q = Slider(Qt.Orientation.Horizontal)
        q.setRange(1, 100)
        q.setValue(int(opt.get("quality", 100)))
        q_val = CaptionLabel(str(q.value()))
        q_val.setFixedWidth(34)
        q.valueChanged.connect(
            lambda val: (opt.__setitem__("quality", val), q_val.setText(str(val)))
        )
        qr = QWidget(); qrh = QHBoxLayout(qr); qrh.setContentsMargins(0,0,0,0)
        qrh.setSpacing(10); qrh.addWidget(q, 1); qrh.addWidget(q_val)
        v.addWidget(self._row(tr("compress.quality"), qr))

        prog = SwitchButton(); prog.setChecked(bool(opt.get("progressive", True)))
        prog.checkedChanged.connect(lambda b: opt.__setitem__("progressive", bool(b)))
        v.addWidget(self._row(tr("compress.advanced.progressive"), prog))

        arith = SwitchButton(); arith.setChecked(bool(opt.get("arithmetic", False)))
        arith.checkedChanged.connect(lambda b: opt.__setitem__("arithmetic", bool(b)))
        v.addWidget(self._row(tr("compress.advanced.arithmetic"), arith))

        strip = SwitchButton(); strip.setChecked(bool(opt.get("strip", True)))
        strip.checkedChanged.connect(lambda b: opt.__setitem__("strip", bool(b)))
        v.addWidget(self._row(tr("compress.advanced.strip_meta"), strip))
        return grp

    def _combo_row(self, cat: str, key: str, label: str, options: list[str]) -> QWidget:
        combo = ComboBox()
        for o in options:
            combo.addItem(tr("convert.advanced.original") if o == "original" else o,
                          userData=o)
        current = advanced.get(cat).get(key, "original")
        for i in range(combo.count()):
            combo.setCurrentIndex(i)
            if combo.currentData() == current:
                break
        combo.currentIndexChanged.connect(
            lambda _i, c=cat, k=key, cb=combo: advanced.adv[c].__setitem__(k, cb.currentData())
        )
        return self._row(label, combo)

    def _switch_row(self, cat: str, key: str, label: str) -> QWidget:
        sw = SwitchButton()
        sw.setChecked(bool(advanced.get(cat).get(key, False)))
        sw.checkedChanged.connect(
            lambda v, c=cat, k=key: advanced.adv[c].__setitem__(k, bool(v))
        )
        row = self._row(label, sw)
        row.setToolTip(tr("convert.advanced.merge_hint"))
        return row

    def retranslate(self):
        self._expand.header.titleLabel.setText(tr("convert.advanced.title"))
        for cat, panel in self._cat_panels.items():
            panel.header.titleLabel.setText(tr(CATEGORY_PANEL_TITLE.get(cat, cat)))

    def retheme(self):
        """Re-apply theme-aware colors to every header chevron."""
        self._expand.header.retheme()
        for panel in self._cat_panels.values():
            panel.header.retheme()
