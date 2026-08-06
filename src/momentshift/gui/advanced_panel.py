"""转换界面的「按分类」高级参数面板。

职责边界：
- 做：就地修改 core.advanced.adv（引擎读取的同一份实时存储），提供刷新 / 多语言 /
  换肤接口。
- 不做：不直接执行命令；不持有任务队列。

依赖：core/advanced、core/qt_compat、i18n/translator、gui/theme；
被依赖：gui/convert_interface。

公开 API：
- AdvancedPanel(parent)
- refresh(categories)
- retranslate() / retheme()
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QTransform
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSlider, QSpinBox, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ComboBox, SwitchButton
from qfluentwidgets import FluentIcon as FIF

from ..core import advanced
from ..core.logger import get_logger
from ..core.qt_compat import Signal
from ..i18n.translator import tr
from .base import bind_combo_mapping, combo_value, select_combo_value
from .theme import (
    apply_text,
    apply_transparent,
    field_row,
    muted_text,
    text_secondary,
    text_strong,
)

log = get_logger("advanced_panel")


# --------------------------------------------------------------------------
# 可折叠分节
# --------------------------------------------------------------------------
class _Header(QWidget):
    """折叠分节的标题栏，点击后发出 clicked 由外层切换展开状态。"""

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
        self._apply_title()
        hb.addWidget(self.titleLbl)
        hb.addStretch(1)
        self.chevron = QLabel()
        self._paint_chevron()
        hb.addWidget(self.chevron)

    def _apply_title(self) -> None:
        """把标题文字样式落到 titleLbl 上（构造期与 retheme 共用）。

        v0.8.0 ODD-01：原来是 ``"浅色样式" if not False else "深色样式"``——
        ``not False`` 恒真，深色分支自 v0.7.x 砍掉深色主题后就是死代码，
        且两个分支的颜色都是硬编码灰阶（近黑 / 近白），绕开了 theme 的设计
        token。现在直接取 theme 的主文字色。
        """
        apply_text(self.titleLbl, text_strong(), weight=700)

    def _paint_chevron(self):
        # 同 ODD-01：折叠箭头颜色改用 theme 的次要文字色（原为魔法数
        # QColor(120,120,120)，与 TEXT_SECONDARY #757575 肉眼无差）。
        color = QColor(text_secondary())
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
        self._apply_title()


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
# 高级参数面板
# --------------------------------------------------------------------------
class AdvancedPanel(QWidget):
    """按文件分类动态拼装的高级参数面板。

    典型用法::

        panel.set_video_context(video_paths)
        panel.refresh(["image", "video"])
        args = panel.get_args("video", "mp4")

    线程约定：仅在 GUI 主线程使用；参数值写入 core.advanced 的全局字典，
    由入队时统一取快照，避免入队后改面板影响已排队任务。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._categories: list[str] = []
        self._video_paths: list[str] = []  # 视频文件上下文
        self.vbox = QVBoxLayout(self)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(12)
        self._expanders: list[ExpandWidget] = []

    def _add_help(self, widget, help_key: str):
        """帮助按钮：灰色图标 + 点击弹出美化弹窗（无音效）。"""
        from .help_bubble import attach_help

        attach_help(widget, help_key, self)

    # --- 构建 ---
    def refresh(self, categories: list[str]):
        """按分类重建面板内容。

        Args:
            categories: 当前待转换文件涉及的分类，如 ["image", "video"]。
                只为出现过的分类生成分节，避免面板堆满无关参数。
        """
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
        valid_default = default_backend(f)  # oxipng / jpegoptim / gifsicle / pillow
        for bid in ("oxipng", "jpegoptim", "gifsicle"):  # + gifsicle
            if bid not in self._backend_order:
                continue
            idx = self._backend_order.index(bid)
            disabled = valid_default != bid
            try:
                self._backend_combo.setItemEnabled(idx, not disabled)
            except Exception:
                log.debug("禁用下拉项失败，忽略")  # 静默原因：combobox 可能已随界面销毁
        comp = advanced.adv["image"].get("compress", {})
        if isinstance(comp, dict):
            cur = comp.get("backend", "auto")
            if cur in ("oxipng", "jpegoptim", "gifsicle") and valid_default != cur:
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
            [
                (tr("advanced.compression.auto"), "auto"),
                (tr("advanced.compression.oxipng"), "oxipng"),
                (tr("advanced.compression.jpegoptim"), "jpegoptim"),
                (tr("advanced.compression.gifsicle"), "gifsicle"),
                (tr("advanced.compression.pillow"), "pillow"),
                (tr("advanced.compression.ffmpeg"), "ffmpeg"),
            ],
            comp.get("backend", "auto"),
            lambda v: comp.__setitem__("backend", v),
        )
        self._backend_combo = backend
        self._backend_order = ["auto", "oxipng", "jpegoptim", "gifsicle", "pillow", "ffmpeg"]
        fr = field_row(tr("advanced.compression.backend"), backend, label_width=80)
        self._add_help(fr, "advanced.help.backend")
        self.vbox.addWidget(fr)

        # 路由提示
        hint = CaptionLabel(tr("advanced.compression.route"))
        hint.setWordWrap(True)
        apply_text(hint, muted_text(), transparent=True)
        self.vbox.addWidget(hint)
        self._route_hint = hint

        # -- oxipng 参数组 ----------------------------------------------
        oxi_grp, oxi_l = self._param_group()
        lvl = QSlider(Qt.Orientation.Horizontal)
        lvl.setRange(0, 6)
        lvl.setValue(int(comp.get("level", 3)))
        lvl_label = QLabel(str(comp.get("level", 3)))
        lvl.valueChanged.connect(
            lambda v: (comp.__setitem__("level", v), lvl_label.setText(str(v)))
        )
        row = QHBoxLayout()
        row.addWidget(lvl_label)
        row.addWidget(lvl, 1)
        fr = field_row(tr("advanced.level"), row)
        self._add_help(fr, "advanced.help.level")
        oxi_l.addWidget(fr)

        inter = SwitchButton()
        inter.setChecked(bool(comp.get("interlace", False)))
        inter.checkedChanged.connect(lambda b: comp.__setitem__("interlace", b))
        fr = field_row(tr("advanced.interlace"), inter)
        self._add_help(fr, "advanced.help.interlace")
        oxi_l.addWidget(fr)

        # 调整1：增加「全部保留」选项，默认不删除元数据
        strip = _combo(
            [
                (tr("advanced.strip.none"), "none"),
                (tr("advanced.strip.safe"), "safe"),
                (tr("advanced.strip.all"), "all"),
            ],
            comp.get("strip", "none"),
            lambda v: comp.__setitem__("strip", v),
        )
        fr = field_row(tr("advanced.strip"), strip)
        self._add_help(fr, "advanced.help.strip")
        oxi_l.addWidget(fr)

        o_filt = _combo(
            [
                (tr("advanced.filter.none"), 0),
                (tr("advanced.filter.sub"), 1),
                (tr("advanced.filter.up"), 2),
                (tr("advanced.filter.average"), 3),
                (tr("advanced.filter.paeth"), 4),
                (tr("advanced.filter.mixed"), 5),
            ],
            comp.get("filter", 0),
            lambda v: comp.__setitem__("filter", int(v)),
        )
        fr = field_row(tr("advanced.filter"), o_filt)
        self._add_help(fr, "advanced.help.filter")
        oxi_l.addWidget(fr)

        zc = QSlider(Qt.Orientation.Horizontal)
        zc.setRange(1, 9)
        zc.setValue(int(comp.get("zc", 6)))
        zc_label = QLabel(str(comp.get("zc", 6)))
        zc.valueChanged.connect(lambda v: (comp.__setitem__("zc", v), zc_label.setText(str(v))))
        zc_row = QHBoxLayout()
        zc_row.addWidget(zc_label)
        zc_row.addWidget(zc, 1)
        fr = field_row(tr("advanced.zc"), zc_row)
        self._add_help(fr, "advanced.help.zc")
        oxi_l.addWidget(fr)

        alpha = SwitchButton()
        alpha.setChecked(bool(comp.get("alpha", False)))
        alpha.checkedChanged.connect(lambda b: comp.__setitem__("alpha", b))
        fr = field_row(tr("advanced.alpha"), alpha)
        self._add_help(fr, "advanced.help.alpha")
        oxi_l.addWidget(fr)
        self.vbox.addWidget(oxi_grp)

        # -- jpegoptim 参数组 -------------------------------------------
        jo_grp, jo_l = self._param_group()
        jo_mode = _combo(
            [
                (tr("advanced.jo.mode.lossless"), "lossless"),
                (tr("advanced.jo.mode.lossy"), "lossy"),
            ],
            comp.get("jo_mode", "lossless"),
            lambda v: (comp.__setitem__("jo_mode", v), _sync_jo_max(v)),
        )
        fr = field_row(tr("advanced.jo.mode"), jo_mode)
        self._add_help(fr, "advanced.help.jo.mode")
        jo_l.addWidget(fr)

        jo_max = QSlider(Qt.Orientation.Horizontal)
        jo_max.setRange(0, 100)
        jo_max.setValue(int(comp.get("jo_max", 85)))
        jo_max_spin = QSpinBox()
        jo_max_spin.setRange(0, 100)
        jo_max_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        jo_max_spin.setValue(int(comp.get("jo_max", 85)))
        jo_max.valueChanged.connect(
            lambda v: (comp.__setitem__("jo_max", v), jo_max_spin.setValue(v))
        )
        jo_max_spin.valueChanged.connect(
            lambda v: (comp.__setitem__("jo_max", v), jo_max.setValue(v))
        )
        jm_row = QHBoxLayout()
        jm_row.addWidget(jo_max, 1)
        jm_row.addWidget(jo_max_spin)
        jo_max_fr = field_row(tr("advanced.jo.max"), jm_row)
        self._add_help(jo_max_fr, "advanced.help.jo.max")
        jo_l.addWidget(jo_max_fr)

        def _sync_jo_max(mode: str):
            jo_max_fr.setEnabled(mode == "lossy")

        _sync_jo_max(comp.get("jo_mode", "lossless"))

        jo_strip = _combo(
            [
                (tr("advanced.jo.strip.none"), "none"),
                (tr("advanced.jo.strip.meta"), "meta"),
                (tr("advanced.jo.strip.exif"), "exif"),
                (tr("advanced.jo.strip.icc"), "icc"),
                (tr("advanced.jo.strip.all"), "all"),
            ],
            comp.get("jo_strip", "none"),
            lambda v: comp.__setitem__("jo_strip", v),
        )
        fr = field_row(tr("advanced.jo.strip"), jo_strip)
        self._add_help(fr, "advanced.help.jo.strip")
        jo_l.addWidget(fr)

        jo_prog = _combo(
            [
                (tr("advanced.jo.prog.auto"), "auto"),
                (tr("advanced.jo.prog.keep"), "keep"),
                (tr("advanced.jo.prog.progressive"), "progressive"),
                (tr("advanced.jo.prog.normal"), "normal"),
            ],
            comp.get("jo_progressive", "auto"),
            lambda v: comp.__setitem__("jo_progressive", v),
        )
        fr = field_row(tr("advanced.jo.prog"), jo_prog)
        self._add_help(fr, "advanced.help.jo.prog")
        jo_l.addWidget(fr)

        jo_thr = QSpinBox()
        jo_thr.setRange(0, 99)
        jo_thr.setSuffix("%")
        jo_thr.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        jo_thr.setValue(int(comp.get("jo_threshold", 0)))
        jo_thr.valueChanged.connect(lambda v: comp.__setitem__("jo_threshold", v))
        fr = field_row(tr("advanced.jo.threshold"), jo_thr)
        self._add_help(fr, "advanced.help.jo.threshold")
        jo_l.addWidget(fr)

        jo_pres = SwitchButton()
        jo_pres.setChecked(bool(comp.get("jo_preserve", True)))
        jo_pres.checkedChanged.connect(lambda b: comp.__setitem__("jo_preserve", b))
        fr = field_row(tr("advanced.jo.preserve"), jo_pres)
        self._add_help(fr, "advanced.help.jo.preserve")
        jo_l.addWidget(fr)

        jo_retry = SwitchButton()
        jo_retry.setChecked(bool(comp.get("jo_retry", False)))
        jo_retry.checkedChanged.connect(lambda b: comp.__setitem__("jo_retry", b))
        fr = field_row(tr("advanced.jo.retry"), jo_retry)
        self._add_help(fr, "advanced.help.jo.retry")
        jo_l.addWidget(fr)
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
        pq.valueChanged.connect(lambda v: (comp.__setitem__("pil_quality", v), pq_spin.setValue(v)))
        pq_spin.valueChanged.connect(lambda v: (comp.__setitem__("pil_quality", v), pq.setValue(v)))
        pq_row = QHBoxLayout()
        pq_row.addWidget(pq, 1)
        pq_row.addWidget(pq_spin)
        fr = field_row(tr("advanced.pil.quality"), pq_row)
        self._add_help(fr, "advanced.help.pil.quality")
        pil_l.addWidget(fr)

        pil_opt = SwitchButton()
        pil_opt.setChecked(bool(comp.get("pil_optimize", True)))
        pil_opt.checkedChanged.connect(lambda b: comp.__setitem__("pil_optimize", b))
        fr = field_row(tr("advanced.pil.optimize"), pil_opt)
        self._add_help(fr, "advanced.help.pil.optimize")
        pil_l.addWidget(fr)

        pil_prog = SwitchButton()
        pil_prog.setChecked(bool(comp.get("pil_progressive", True)))
        pil_prog.checkedChanged.connect(lambda b: comp.__setitem__("pil_progressive", b))
        fr = field_row(tr("advanced.pil.progressive"), pil_prog)
        self._add_help(fr, "advanced.help.pil.progressive")
        pil_l.addWidget(fr)

        pil_sub = _combo(
            [
                (tr("advanced.pil.sub.444"), "4:4:4"),
                (tr("advanced.pil.sub.422"), "4:2:2"),
                (tr("advanced.pil.sub.420"), "4:2:0"),
            ],
            comp.get("pil_subsampling", "4:4:4"),
            lambda v: comp.__setitem__("pil_subsampling", v),
        )
        fr = field_row(tr("advanced.pil.subsampling"), pil_sub)
        self._add_help(fr, "advanced.help.pil.subsampling")
        pil_l.addWidget(fr)
        self.vbox.addWidget(pil_grp)

        # --- Gifsicle 参数组 ---
        gs_grp, gs_l = self._param_group()
        gs_opt = QSlider(Qt.Orientation.Horizontal)
        gs_opt.setRange(1, 3)
        gs_opt.setValue(int(comp.get("gs_optimize", 3)))
        gs_opt_label = QLabel(str(comp.get("gs_optimize", 3)))
        gs_opt.valueChanged.connect(
            lambda v: (comp.__setitem__("gs_optimize", v), gs_opt_label.setText(str(v)))
        )
        gs_row = QHBoxLayout()
        gs_row.addWidget(gs_opt_label)
        gs_row.addWidget(gs_opt, 1)
        fr = field_row(tr("advanced.gifsicle.optimize"), gs_row)
        self._add_help(fr, "advanced.help.gifsicle.optimize")
        gs_l.addWidget(fr)

        gs_loop = QSpinBox()
        gs_loop.setRange(0, 100)
        gs_loop.setValue(int(comp.get("gs_loop", 0)))
        gs_loop.valueChanged.connect(lambda v: comp.__setitem__("gs_loop", v))
        fr = field_row(tr("advanced.gifsicle.loop"), gs_loop)
        self._add_help(fr, "advanced.help.gifsicle.loop")
        gs_l.addWidget(fr)

        gs_lossy = QSlider(Qt.Orientation.Horizontal)
        gs_lossy.setRange(0, 200)
        gs_lossy.setValue(int(comp.get("gs_lossy", 0)))
        gs_lossy_label = QLabel(str(comp.get("gs_lossy", 0)))
        gs_lossy.valueChanged.connect(
            lambda v: (comp.__setitem__("gs_lossy", v), gs_lossy_label.setText(str(v)))
        )
        gs_row = QHBoxLayout()
        gs_row.addWidget(gs_lossy_label)
        gs_row.addWidget(gs_lossy, 1)
        fr = field_row(tr("advanced.gifsicle.lossy"), gs_row)
        self._add_help(fr, "advanced.help.gifsicle.lossy")
        gs_l.addWidget(fr)
        self.vbox.addWidget(gs_grp)

        # -- FFmpeg 参数组（v0.8.22 UI#2）-------------------------------
        # 压缩程序下拉新增「FFmpeg 压缩」。只整合图片类别的参数行；
        # 「输出位置 / 文件名后缀 / 输出文件夹」三件套属于「创建压缩任务」
        # 面板的定位逻辑，转换流程的图片压缩永远就地替换，这里不整合。
        ff_grp, ff_l = self._param_group()
        from ..core import compressor as _compressor

        ff_params = _compressor.FFMPEG_PARAMS_BY_KIND.get("image", {})
        ff_profile_key = "ff_i_profile"
        ff_setters: dict = {}

        def _ff_profile_mapping():
            presets = _compressor.FFMPEG_PRESETS.get("image", {})
            mapping = [(tr(f"ffmpeg.profile.{name}"), name) for name in presets.keys()]
            mapping.append((tr("ffmpeg.profile.custom"), "custom"))
            return mapping

        def _on_ff_profile(preset: str):
            comp[ff_profile_key] = preset
            if preset == "custom":
                return
            overrides = _compressor.ffmpeg_preset_values("image", preset)
            for k, v in overrides.items():
                comp[k] = v
                setter = ff_setters.get(k)
                if setter:
                    setter(v)

        prof = _combo(
            _ff_profile_mapping(),
            comp.get(ff_profile_key, "balanced"),
            _on_ff_profile,
        )
        fr = field_row(tr("ffmpeg.ff_i_profile"), prof)
        self._add_help(fr, "ffmpeg.help.ff_i_profile")
        ff_l.addWidget(fr)

        for pkey, spec in ff_params.items():
            if pkey == ff_profile_key:
                continue
            t = spec.get("type")
            if t == "bool":
                ctl = SwitchButton()
                ctl.setChecked(bool(comp.get(pkey, spec.get("default", False))))
                ctl.checkedChanged.connect(
                    lambda b, k=pkey: comp.__setitem__(k, b)
                )
                ff_setters[pkey] = lambda v, c=ctl: c.setChecked(bool(v))
                fr = field_row(tr(f"ffmpeg.{pkey}"), ctl)
            elif t == "choice":
                vals = spec.get("values", [])
                labels = spec.get("labels") or {}
                mapping = [
                    (labels.get(v, _compressor.FFMPEG_VALUE_LABELS.get(v, v)), v)
                    for v in vals
                ]
                ctl = _combo(
                    mapping,
                    comp.get(pkey, spec.get("default")),
                    lambda v, k=pkey: comp.__setitem__(k, v),
                )
                ff_setters[pkey] = lambda v, c=ctl: select_combo_value(c, v)
                fr = field_row(tr(f"ffmpeg.{pkey}"), ctl)
            else:
                lo, hi = int(spec.get("min", 0)), int(spec.get("max", 100))
                slider = QSlider(Qt.Orientation.Horizontal)
                slider.setRange(lo, hi)
                slider.setValue(int(comp.get(pkey, spec.get("default", lo)) or lo))
                spin = QSpinBox()
                spin.setRange(lo, hi)
                spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
                spin.setValue(slider.value())
                slider.valueChanged.connect(
                    lambda v, k=pkey, s=spin: (comp.__setitem__(k, v), s.setValue(v))
                )
                spin.valueChanged.connect(
                    lambda v, k=pkey, s=slider: (comp.__setitem__(k, v), s.setValue(v))
                )
                row = QHBoxLayout()
                row.addWidget(slider, 1)
                row.addWidget(spin)

                def _set_int(v, s=slider, sp=spin):
                    s.setValue(int(v))
                    sp.setValue(int(v))

                ff_setters[pkey] = _set_int
                fr = field_row(tr(f"ffmpeg.{pkey}"), row)
            self._add_help(fr, f"ffmpeg.help.{pkey}")
            ff_l.addWidget(fr)
        self.vbox.addWidget(ff_grp)

        # -- 按后端显示对应参数组 ----------------------------------------
        self._backend_groups = {
            "oxipng": oxi_grp,
            "jpegoptim": jo_grp,
            "gifsicle": gs_grp,
            "pillow": pil_grp,
            "ffmpeg": ff_grp,
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
        backend.currentTextChanged.connect(lambda _t: _sync(combo_value(backend)))

    def _param_group(self) -> tuple[QWidget, QVBoxLayout]:
        """新建一个缩进的参数分组容器。"""
        grp = QWidget()
        apply_transparent(grp)
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(8, 0, 0, 0)
        lay.setSpacing(6)
        return grp, lay

    def _add_video(self):
        """视频高级设置。

        V0.8.21 功能调整 1：原来的「分辨率 / 帧率 / 码率 / 编码器」四个下拉
        是一套只在转换阶段拼命令行的简化参数，与「创建压缩任务-视频」里的
        FFmpeg 压缩参数各说各话——同一件事两套 UI、两套默认值、两套预设。
        现在整体换成后者（质量预设 + 完整 FFmpeg 参数），写入
        ``adv["video"]["compress"]``，在转换后的压缩阶段生效。

        保留不动的三项（产品要求）：合并为单个文件、仅提取音频、音频格式。
        不含「输出位置 / 文件名后缀 / 输出文件夹」——那是压缩任务对话框的
        职责，转换流程的输出位置由转换界面统一决定。
        """
        adv = advanced.adv["video"]
        # v0.7.18 的动态分辨率下拉随参数一起下线；置空防止 _refresh_video_resolution
        # 摸到上一次 refresh 留下的野指针（控件已 deleteLater）。
        self._res_combo = None

        # --- v0.8.3 仅提取音频 + 合并为单个文件：水平对齐（仅提取音频在前） ---
        # 启用「仅提取音频」后不再转换视频画面，只输出所选格式的音频
        # （见 presets.build_args），视频压缩参数整组失效。
        extract = SwitchButton()
        extract.setChecked(bool(adv.get("extract_audio", False)))
        extract.checkedChanged.connect(
            lambda b: (adv.__setitem__("extract_audio", b), self._sync_extract_audio())
        )
        self._extract_switch = extract
        extract_row = field_row(tr("advanced.video.extract_audio"), extract, label_width=132)

        merge = SwitchButton()
        merge.setChecked(bool(adv.get("merge", False)))
        merge.checkedChanged.connect(lambda b: adv.__setitem__("merge", b))
        # label_width 需容纳 7 个汉字，否则「合并为单个文件」显示不全
        merge_row = field_row(tr("advanced.merge"), merge, label_width=132)

        # v0.8.22 UI#1：两个开关水平并排（仅提取音频在前），末尾 stretch 保持整体左对齐
        two = QHBoxLayout()
        two.setSpacing(24)
        two.addWidget(extract_row)
        two.addWidget(merge_row)
        two.addStretch(1)
        self.vbox.addLayout(two)

        hint = CaptionLabel(tr("advanced.video.extract_audio.hint"))
        hint.setWordWrap(True)
        apply_text(hint, muted_text(), transparent=True)
        self.vbox.addWidget(hint)
        self._extract_hint = hint

        fmt = _combo(
            [(f.upper(), f) for f in advanced.AUDIO_FORMATS],  # MP3 排第一（产品要求）
            adv.get("audio_format", "mp3"),
            lambda v: adv.__setitem__("audio_format", v),
        )
        self._audio_fmt_combo = fmt
        # 「音频格式」在「仅提取音频 / 合并为单个文件」下方、整体左对齐
        fmt_row = field_row(tr("advanced.video.audio_format"), fmt, label_width=132)
        self.vbox.addWidget(fmt_row)
        self._audio_fmt_row = fmt_row

        # --- V0.8.21：FFmpeg 视频压缩参数 ---
        ff_grp = self._add_ff_compress("video", self._ff_comp(adv, "video"))
        self.vbox.addWidget(ff_grp)

        # 「仅提取音频」时输出的是音频文件，视频压缩参数整组失效
        self._video_param_rows = [ff_grp]
        self._sync_extract_audio()

    def _sync_extract_audio(self):
        """v0.8.3：同步「仅提取音频」开关与相关行的可用/可见状态。"""
        adv = advanced.adv["video"]
        enabled = bool(adv.get("extract_audio", False))
        if hasattr(self, "_audio_fmt_row"):
            self._audio_fmt_row.setEnabled(enabled)
        if hasattr(self, "_extract_hint"):
            self._extract_hint.setVisible(enabled)
        for row in getattr(self, "_video_param_rows", []):
            if row is not None:
                row.setEnabled(not enabled)

    def _add_audio(self):
        """音频高级设置。

        V0.8.21 功能调整 2：同 :meth:`_add_video`，把「码率 / 采样率 / 声道」
        三个简化下拉换成「创建压缩任务-音频」里那套 FFmpeg 音频压缩参数，
        只保留「合并为单个文件」。
        """
        adv = advanced.adv["audio"]
        merge = SwitchButton()
        merge.setChecked(bool(adv.get("merge", False)))
        merge.checkedChanged.connect(lambda b: adv.__setitem__("merge", b))
        # label_width 需容纳 7 个汉字
        self.vbox.addWidget(field_row(tr("advanced.merge"), merge, label_width=132))

        # --- V0.8.21：FFmpeg 音频压缩参数 ---
        self.vbox.addWidget(self._add_ff_compress("audio", self._ff_comp(adv, "audio")))

    # --- V0.8.21：FFmpeg 压缩参数渲染（与 compress_task_panel 同源） ---
    @staticmethod
    def _ff_comp(adv: dict, kind: str) -> dict:
        """取出（必要时补建）该分类的 ``compress`` 子字典。

        ``adv`` 是 :mod:`core.advanced` 里那份实时存储，控件直接就地改它；
        入队时由 ``advanced.snapshot()`` 深拷贝，因此后续改动不会串台到
        已入队任务。旧配置里可能没有 ``compress`` 键（或是别的类型），
        这里统一补成一份带默认值的新字典。
        """
        comp = adv.get("compress")
        if not isinstance(comp, dict) or not comp:
            comp = advanced.ffmpeg_param_defaults(kind)
            adv["compress"] = comp
        return comp

    def _add_ff_compress(self, kind: str, comp: dict) -> QWidget:
        """按 ``PARAMS_BY_KIND`` 声明式渲染一组 FFmpeg 压缩参数。

        Args:
            kind: ``"video"`` / ``"audio"``。
            comp: 目标存储字典（控件就地写入）。

        Returns:
            承载「质量预设 + 各参数行」的容器控件，由调用方塞进 vbox。

        Notes:
            与 :meth:`gui.compress_task_panel.CompressTaskPanel._build_ffmpeg`
            共用同一份参数表 / 预设表 / i18n key，二者永远不会漂移。
        """
        from ..core import compressor

        params = compressor.FFMPEG_PARAMS_BY_KIND.get(kind, {})
        profile_key = {"video": "ff_v_profile", "audio": "ff_a_profile"}[kind]
        grp, gl = self._param_group()

        setters: dict = {}
        mapping = [(tr(f"ffmpeg.profile.{n}"), n) for n in compressor.FFMPEG_PRESETS.get(kind, {})]
        mapping.append((tr("ffmpeg.profile.custom"), "custom"))
        prof = _combo(
            mapping,
            comp.get(profile_key, "balanced"),
            lambda v: self._apply_ff_profile(kind, comp, profile_key, setters, v),
        )
        gl.addWidget(field_row(tr("ffmpeg.quality_preset"), prof, label_width=110))

        for pkey, spec in params.items():
            if pkey == profile_key:
                continue
            ctl, setter = self._build_ff_param(comp, pkey, spec)
            fr = field_row(tr(f"ffmpeg.{pkey}"), ctl, label_width=110)
            self._add_help(fr, f"ffmpeg.help.{pkey}")
            gl.addWidget(fr)
            setters[pkey] = setter
        return grp

    def _build_ff_param(self, comp: dict, pkey: str, spec: dict):
        """按参数声明造一个控件，返回 ``(控件或布局, 回填函数)``。

        回填函数供「质量预设」切换时把预设值刷回 UI；它只动控件，
        数据由 :meth:`_apply_ff_profile` 直接写 ``comp``。
        """
        from ..core import compressor

        t = spec.get("type")
        if t == "bool":
            ctl = SwitchButton()
            ctl.setChecked(bool(comp.get(pkey, spec.get("default", False))))
            ctl.checkedChanged.connect(lambda b: comp.__setitem__(pkey, b))
            return ctl, (lambda v: ctl.setChecked(bool(v)))
        if t == "choice":
            vals = spec.get("values", [])
            ov = spec.get("labels") or {}
            mapping = [(ov.get(v, compressor.FFMPEG_VALUE_LABELS.get(v, v)), v) for v in vals]
            ctl = _combo(
                mapping,
                comp.get(pkey, spec.get("default")),
                lambda v: comp.__setitem__(pkey, v),
            )
            # V0.8.21 Bug1：与压缩任务对话框一致，视频编码器要过硬件门禁，
            # 否则 AMD/Intel 用户选了 *_nvenc 要等真跑 ffmpeg 才报
            # "Could not open encoder"。探测走后台线程 + 进程内缓存。
            if pkey == "ff_v_encoder":
                self._gate_encoder_combo(ctl)
            return ctl, (lambda v: select_combo_value(ctl, v))
        lo = int(spec.get("min", 0))
        hi = int(spec.get("max", 100))
        cur = int(comp.get(pkey, spec.get("default", lo)) or lo)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(cur)
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        spin.setValue(cur)
        slider.valueChanged.connect(lambda v: (comp.__setitem__(pkey, v), spin.setValue(v)))
        spin.valueChanged.connect(lambda v: (comp.__setitem__(pkey, v), slider.setValue(v)))
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(spin)
        return row, (lambda v: (slider.setValue(int(v)), spin.setValue(int(v))))

    def _gate_encoder_combo(self, combo) -> None:
        """给视频编码器下拉挂硬件门禁（异步，结果回来再置灰不可用项）。"""
        from .hw_probe import apply_encoder_gate, probe_async

        def _apply(result: dict):
            try:
                apply_encoder_gate(combo, result)
            except RuntimeError:
                log.debug("编码器门禁回调时控件已销毁，忽略")

        probe_async(_apply)

    def _apply_ff_profile(self, kind: str, comp: dict, profile_key: str, setters: dict, preset):
        """切换质量预设：写回预设覆盖值并同步 UI；``custom`` 只记选择。"""
        from ..core import compressor

        comp[profile_key] = preset
        if preset == "custom":
            return
        for k, v in compressor.ffmpeg_preset_values(kind, preset).items():
            comp[k] = v
            fn = setters.get(k)
            if fn:
                fn(v)

    # --- 状态更新 ---
    def get_args(self, category: str, target: str = "") -> list[str]:
        """按面板当前取值生成该分类的 ffmpeg 命令行参数。

        Args:
            category: 分类名，如 "image" / "video" / "audio"。
            target: 目标格式（扩展名，不带点）；不同目标格式的可用参数不同。

        Returns:
            可直接拼进 ffmpeg 命令行的参数列表；无高级参数时返回空列表。
        """
        return advanced.build_advanced_args(category, target, advanced.get(category))

    def set_video_context(self, video_paths: list[str]):
        """记录当前待转换的视频路径。

        v0.7.18 时这个方法要按实际分辨率动态重建「分辨率」下拉；V0.8.21
        视频高级设置整体换成 FFmpeg 压缩参数后，缩放交给 ``ff_v_scale``
        （按比例，不依赖源分辨率），探测源尺寸不再必要。方法保留是因为
        ``convert_setup_dialog`` 在两处调用它，且路径上下文后续（如按时长
        估算输出体积）还用得上。
        """
        self._video_paths = list(video_paths or [])

    def retranslate(self):
        self.refresh(self._categories)

    def retheme(self):
        for ex in self._expanders:
            ex.retheme()

    def _clear(self):

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


def _combo(mapping: list[tuple[str, str]], current, on_change) -> ComboBox:
    """建一个「显示文案 -> 逻辑值」映射型下拉框。

    Args:
        mapping: ``[(显示文案, 逻辑值), ...]``，按此顺序填充候选项。
        current: 初始选中的**逻辑值**；不在映射里则保持第 0 项。
        on_change: 选择变化时的回调，收到的是逻辑值而非显示文案。

    Returns:
        已填充候选项、已绑定映射并接好信号的 ``ComboBox``。

    Notes:
        v0.8.0 ODD-07：映射的挂载与读取一律走 gui/base 的公开 API，
        本模块不再直接碰 ``combo._mapping`` 这个私有属性。
    """
    combo = ComboBox()
    for display, _value in mapping:
        combo.addItem(display)
    # 必须先绑映射再按逻辑值选中；顺序反了 select_combo_value 找不到候选项。
    bind_combo_mapping(combo, mapping)
    select_combo_value(combo, current)
    combo.currentTextChanged.connect(lambda _t: on_change(combo_value(combo)))
    return combo


def _clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w:
            w.deleteLater()
