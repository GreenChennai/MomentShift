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
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QSpinBox

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
        self.vbox.setSpacing(12)
        self._expanders: list[ExpandWidget] = []

    def _add_help(self, widget, help_key: str):
        """帮助按钮：灰色图标 + 点击弹出美化弹窗（无音效）。"""
        from .help_bubble import attach_help
        attach_help(widget, help_key, self)

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

    def on_format_change(self, fmt: str):
        """目标格式切换（v0.7.0：png→oxipng / jpg→jpegoptim / 其他→pillow）。

        后端为「自动」时只刷新参数组显示，不覆盖用户手动选择的后端。
        v0.7.1：按目标格式禁用不匹配的压缩程序（如 png 禁用 jpegoptim）。
        """
        self._current_fmt = (fmt or "").lower().lstrip(".")
        if not hasattr(self, "_backend_combo"):
            return
        comp = advanced.adv["image"].get("compress", {})
        current = comp.get("backend", "auto") if isinstance(comp, dict) else "auto"
        if hasattr(self, "_sync_backend_groups"):
            self._sync_backend_groups(current)
        self._sync_backend_enabled(fmt)

    def _sync_backend_enabled(self, fmt: str):
        """按目标格式禁用不匹配的压缩程序选项（v0.7.1, F5）。

        png → 禁用 jpegoptim；jpg/jpeg → 禁用 oxipng；其他 → 二者均禁用。
        若当前选中项被禁用，则回退到「自动选择」。
        """
        if not hasattr(self, "_backend_combo"):
            return
        from ..core.compressor import default_backend
        f = (fmt or "").lower().lstrip(".")
        valid_default = default_backend(f)  # oxipng / jpegoptim / pillow
        for bid in ("oxipng", "jpegoptim"):
            idx = self._backend_order.index(bid)
            disabled = valid_default != bid
            try:
                self._backend_combo.setItemEnabled(idx, not disabled)
            except Exception:
                pass
        comp = advanced.adv["image"].get("compress", {})
        if isinstance(comp, dict):
            cur = comp.get("backend", "auto")
            if cur in ("oxipng", "jpegoptim") and valid_default != cur:
                comp["backend"] = "auto"
                self._backend_combo.setCurrentText(tr("advanced.compression.auto"))

    def _add_expander(self, title_key: str, builder) -> ExpandWidget:
        ex = ExpandWidget(tr(title_key))
        builder(ex.body_layout)
        self.vbox.addWidget(ex)
        return ex

    def _add_image(self):
        """图像压缩参数（v0.7.0：oxipng / jpegoptim / Pillow 三组按后端切换）。"""
        from ..core.compressor import default_backend

        adv = advanced.adv["image"]
        if not isinstance(adv.get("compress"), dict):
            adv["compress"] = dict(advanced.default_options()["image"]["compress"])
        comp = adv["compress"]

        # -- 压缩后端 ---------------------------------------------------
        backend = _combo(
            [(tr("advanced.compression.auto"), "auto"),
             (tr("advanced.compression.oxipng"), "oxipng"),
             (tr("advanced.compression.jpegoptim"), "jpegoptim"),
             (tr("advanced.compression.pillow"), "pillow")],
            comp.get("backend", "auto"),
            lambda v: comp.__setitem__("backend", v),
        )
        self._backend_combo = backend
        self._backend_order = ["auto", "oxipng", "jpegoptim", "pillow"]
        fr = field_row(tr("advanced.compression.backend"), backend, label_width=80)
        self._add_help(fr, "advanced.help.backend")
        self.vbox.addWidget(fr)

        # 路由提示
        hint = CaptionLabel(tr("advanced.compression.route"))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {muted_text()}; background: transparent;")
        self.vbox.addWidget(hint)
        self._route_hint = hint

        # -- oxipng 参数组 ----------------------------------------------
        oxi_grp, oxi_l = self._param_group()
        lvl = QSlider(Qt.Orientation.Horizontal)
        lvl.setRange(0, 6)
        lvl.setValue(int(comp.get("level", 3)))
        lvl_label = QLabel(str(comp.get("level", 3)))
        lvl.valueChanged.connect(
            lambda v: (comp.__setitem__("level", v), lvl_label.setText(str(v))))
        row = QHBoxLayout(); row.addWidget(lvl_label); row.addWidget(lvl, 1)
        fr = field_row(tr("advanced.level"), row)
        self._add_help(fr, "advanced.help.level"); oxi_l.addWidget(fr)

        inter = SwitchButton()
        inter.setChecked(bool(comp.get("interlace", False)))
        inter.checkedChanged.connect(lambda b: comp.__setitem__("interlace", b))
        fr = field_row(tr("advanced.interlace"), inter)
        self._add_help(fr, "advanced.help.interlace"); oxi_l.addWidget(fr)

        # v0.7.7 调整1：增加「全部保留」选项，默认不删除元数据
        strip = _combo(
            [(tr("advanced.strip.none"), "none"),
             (tr("advanced.strip.safe"), "safe"),
             (tr("advanced.strip.all"), "all")],
            comp.get("strip", "none"), lambda v: comp.__setitem__("strip", v))
        fr = field_row(tr("advanced.strip"), strip)
        self._add_help(fr, "advanced.help.strip"); oxi_l.addWidget(fr)

        o_filt = _combo([(tr("advanced.filter.none"), 0), (tr("advanced.filter.sub"), 1),
                         (tr("advanced.filter.up"), 2), (tr("advanced.filter.average"), 3),
                         (tr("advanced.filter.paeth"), 4), (tr("advanced.filter.mixed"), 5)],
                        comp.get("filter", 0),
                        lambda v: comp.__setitem__("filter", int(v)))
        fr = field_row(tr("advanced.filter"), o_filt)
        self._add_help(fr, "advanced.help.filter"); oxi_l.addWidget(fr)

        zc = QSlider(Qt.Orientation.Horizontal)
        zc.setRange(1, 9)
        zc.setValue(int(comp.get("zc", 6)))
        zc_label = QLabel(str(comp.get("zc", 6)))
        zc.valueChanged.connect(
            lambda v: (comp.__setitem__("zc", v), zc_label.setText(str(v))))
        zc_row = QHBoxLayout(); zc_row.addWidget(zc_label); zc_row.addWidget(zc, 1)
        fr = field_row(tr("advanced.zc"), zc_row)
        self._add_help(fr, "advanced.help.zc"); oxi_l.addWidget(fr)

        alpha = SwitchButton()
        alpha.setChecked(bool(comp.get("alpha", False)))
        alpha.checkedChanged.connect(lambda b: comp.__setitem__("alpha", b))
        fr = field_row(tr("advanced.alpha"), alpha)
        self._add_help(fr, "advanced.help.alpha"); oxi_l.addWidget(fr)
        self.vbox.addWidget(oxi_grp)

        # -- jpegoptim 参数组 -------------------------------------------
        jo_grp, jo_l = self._param_group()
        jo_mode = _combo(
            [(tr("advanced.jo.mode.lossless"), "lossless"),
             (tr("advanced.jo.mode.lossy"), "lossy")],
            comp.get("jo_mode", "lossless"),
            lambda v: (comp.__setitem__("jo_mode", v), _sync_jo_max(v)))
        fr = field_row(tr("advanced.jo.mode"), jo_mode)
        self._add_help(fr, "advanced.help.jo.mode"); jo_l.addWidget(fr)

        jo_max = QSlider(Qt.Orientation.Horizontal)
        jo_max.setRange(0, 100)
        jo_max.setValue(int(comp.get("jo_max", 85)))
        jo_max_spin = QSpinBox()
        jo_max_spin.setRange(0, 100)
        jo_max_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        jo_max_spin.setValue(int(comp.get("jo_max", 85)))
        jo_max.valueChanged.connect(
            lambda v: (comp.__setitem__("jo_max", v), jo_max_spin.setValue(v)))
        jo_max_spin.valueChanged.connect(
            lambda v: (comp.__setitem__("jo_max", v), jo_max.setValue(v)))
        jm_row = QHBoxLayout(); jm_row.addWidget(jo_max, 1); jm_row.addWidget(jo_max_spin)
        jo_max_fr = field_row(tr("advanced.jo.max"), jm_row)
        self._add_help(jo_max_fr, "advanced.help.jo.max"); jo_l.addWidget(jo_max_fr)

        def _sync_jo_max(mode: str):
            jo_max_fr.setEnabled(mode == "lossy")
        _sync_jo_max(comp.get("jo_mode", "lossless"))

        jo_strip = _combo(
            [(tr("advanced.jo.strip.none"), "none"),
             (tr("advanced.jo.strip.meta"), "meta"),
             (tr("advanced.jo.strip.exif"), "exif"),
             (tr("advanced.jo.strip.icc"), "icc"),
             (tr("advanced.jo.strip.all"), "all")],
            comp.get("jo_strip", "none"),
            lambda v: comp.__setitem__("jo_strip", v))
        fr = field_row(tr("advanced.jo.strip"), jo_strip)
        self._add_help(fr, "advanced.help.jo.strip"); jo_l.addWidget(fr)

        jo_prog = _combo(
            [(tr("advanced.jo.prog.auto"), "auto"),
             (tr("advanced.jo.prog.keep"), "keep"),
             (tr("advanced.jo.prog.progressive"), "progressive"),
             (tr("advanced.jo.prog.normal"), "normal")],
            comp.get("jo_progressive", "auto"),
            lambda v: comp.__setitem__("jo_progressive", v))
        fr = field_row(tr("advanced.jo.prog"), jo_prog)
        self._add_help(fr, "advanced.help.jo.prog"); jo_l.addWidget(fr)

        jo_thr = QSpinBox()
        jo_thr.setRange(0, 99)
        jo_thr.setSuffix("%")
        jo_thr.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        jo_thr.setValue(int(comp.get("jo_threshold", 0)))
        jo_thr.valueChanged.connect(lambda v: comp.__setitem__("jo_threshold", v))
        fr = field_row(tr("advanced.jo.threshold"), jo_thr)
        self._add_help(fr, "advanced.help.jo.threshold"); jo_l.addWidget(fr)

        jo_pres = SwitchButton()
        jo_pres.setChecked(bool(comp.get("jo_preserve", True)))
        jo_pres.checkedChanged.connect(lambda b: comp.__setitem__("jo_preserve", b))
        fr = field_row(tr("advanced.jo.preserve"), jo_pres)
        self._add_help(fr, "advanced.help.jo.preserve"); jo_l.addWidget(fr)

        jo_retry = SwitchButton()
        jo_retry.setChecked(bool(comp.get("jo_retry", False)))
        jo_retry.checkedChanged.connect(lambda b: comp.__setitem__("jo_retry", b))
        fr = field_row(tr("advanced.jo.retry"), jo_retry)
        self._add_help(fr, "advanced.help.jo.retry"); jo_l.addWidget(fr)
        self.vbox.addWidget(jo_grp)

        # -- Pillow 参数组 ----------------------------------------------
        pil_grp, pil_l = self._param_group()
        pq = QSlider(Qt.Orientation.Horizontal)
        pq.setRange(0, 95)
        pq.setValue(int(comp.get("pil_quality", 95)))
        pq_spin = QSpinBox()
        pq_spin.setRange(0, 95)
        pq_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        pq_spin.setValue(int(comp.get("pil_quality", 95)))
        pq.valueChanged.connect(
            lambda v: (comp.__setitem__("pil_quality", v), pq_spin.setValue(v)))
        pq_spin.valueChanged.connect(
            lambda v: (comp.__setitem__("pil_quality", v), pq.setValue(v)))
        pq_row = QHBoxLayout(); pq_row.addWidget(pq, 1); pq_row.addWidget(pq_spin)
        fr = field_row(tr("advanced.pil.quality"), pq_row)
        self._add_help(fr, "advanced.help.pil.quality"); pil_l.addWidget(fr)

        pil_opt = SwitchButton()
        pil_opt.setChecked(bool(comp.get("pil_optimize", True)))
        pil_opt.checkedChanged.connect(lambda b: comp.__setitem__("pil_optimize", b))
        fr = field_row(tr("advanced.pil.optimize"), pil_opt)
        self._add_help(fr, "advanced.help.pil.optimize"); pil_l.addWidget(fr)

        pil_prog = SwitchButton()
        pil_prog.setChecked(bool(comp.get("pil_progressive", True)))
        pil_prog.checkedChanged.connect(lambda b: comp.__setitem__("pil_progressive", b))
        fr = field_row(tr("advanced.pil.progressive"), pil_prog)
        self._add_help(fr, "advanced.help.pil.progressive"); pil_l.addWidget(fr)

        pil_sub = _combo(
            [(tr("advanced.pil.sub.444"), "4:4:4"),
             (tr("advanced.pil.sub.422"), "4:2:2"),
             (tr("advanced.pil.sub.420"), "4:2:0")],
            comp.get("pil_subsampling", "4:4:4"),
            lambda v: comp.__setitem__("pil_subsampling", v))
        fr = field_row(tr("advanced.pil.subsampling"), pil_sub)
        self._add_help(fr, "advanced.help.pil.subsampling"); pil_l.addWidget(fr)
        self.vbox.addWidget(pil_grp)

        # -- 按后端显示对应参数组 ----------------------------------------
        self._backend_groups = {
            "oxipng": oxi_grp, "jpegoptim": jo_grp, "pillow": pil_grp,
        }

        def _sync(val: str):
            if val == "auto":
                fmt = getattr(self, "_current_fmt", "png")
                val = default_backend(fmt)
            for bid, grp in self._backend_groups.items():
                grp.setVisible(bid == val)
            hint.setVisible(comp.get("backend", "auto") == "auto")

        self._sync_backend_groups = _sync
        _sync(comp.get("backend", "auto"))
        backend.currentTextChanged.connect(
            lambda t: _sync(backend._mapping.get(t, t)))

    def _param_group(self) -> tuple[QWidget, QVBoxLayout]:
        """新建一个缩进的参数分组容器。"""
        grp = QWidget()
        grp.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(8, 0, 0, 0)
        lay.setSpacing(6)
        return grp, lay


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
        merge = SwitchButton()
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
        merge = SwitchButton()
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
