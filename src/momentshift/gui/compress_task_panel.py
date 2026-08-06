"""压缩任务设置面板 —— 按文件类型路由压缩后端（V0.8.18 重构）。

职责边界：
- 做：按文件类型（png / jpg / gif / 其他图片 / 视频 / 音频）给出可用的压缩
  程序候选、各后端参数控件、FFmpeg 分段参数（仅显示与文件类型对应的类别）、
  输出位置三件套（模式 / 后缀 / 固定目录），并产出冻结的压缩设置快照。
- 不做：不负责文件入队、队列调度、任务执行（在 ``gui/compress_interface``）。

为什么独立成模块（V0.8.18）：
大组件「压缩」的「压缩设置」卡片被删除后，主界面不再持有后端参数面板；
「创建压缩任务」弹窗（主界面入队流与右键快速调用共用）改为**按文件类型**
单独弹出，每种类型一个面板实例。路由表（哪种文件能用哪些程序）是核心单一
事实源，收在本模块顶层常量，主界面与 quick_runner 只调 ``compress_kind()``。

路由表（需求 V0.8.18-2）：
    png   → oxipng（默认）/ Pillow / FFmpeg（仅图片栏）
    jpg   → jpegoptim（默认）/ Pillow / FFmpeg（仅图片栏）
    gif   → Gifsicle（唯一）
    其他图片 → Pillow（默认）/ FFmpeg（仅图片栏）
    视频   → FFmpeg（仅视频栏）
    音频   → FFmpeg（仅音频栏）

依赖：core/compressor、core/config、core/presets、gui/theme、i18n/translator；
被依赖：gui/compress_interface、gui/quick_dialogs、quick_runner。
"""

from __future__ import annotations

import copy
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ComboBox,
    StrongBodyLabel,
    SwitchButton,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from ..core import compressor
from ..core.config import cfg
from ..core.presets import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS
from ..i18n.translator import tr
from . import tokens
from .base import combo_value, select_combo_value
from .help_bubble import attach_help
from .theme import (
    apply_text,
    apply_transparent,
    field_row,
    icon_btn,
    sub_text,
)

__all__ = [
    "COMPRESS_KINDS",
    "KIND_BACKENDS",
    "compress_kind",
    "settings_mode",
    "settings_quality",
    "settings_opts",
    "CompressTaskPanel",
]


# =============================================================================
# 路由表（单一事实源）
# =============================================================================
# 各文件类型的可用压缩程序（顺序 = 下拉显示顺序，第一项 = 默认）。
KIND_BACKENDS: dict[str, list[str]] = {
    "png": ["oxipng", "pillow", "ffmpeg"],
    "jpg": ["jpegoptim", "pillow", "ffmpeg"],
    "gif": ["gifsicle"],
    "image": ["pillow", "ffmpeg"],
    "video": ["ffmpeg"],
    "audio": ["ffmpeg"],
}

# 各文件类型对应的 FFmpeg 面板类别；None = 该类型不提供 FFmpeg 后端。
KIND_FF_CATEGORY: dict[str, str | None] = {
    "png": "image",
    "jpg": "image",
    "gif": None,
    "image": "image",
    "video": "video",
    "audio": "audio",
}

COMPRESS_KINDS: tuple[str, ...] = ("png", "jpg", "gif", "image", "video", "audio")

# JPG 家族（jpegoptim 支持）；PNG 单扩展名；GIF 单扩展名
_JPG_EXTS = {"jpg", "jpeg", "jpe"}


def compress_kind(path_or_ext: str) -> str | None:
    """按扩展名判定压缩任务的文件类型（路由用）。

    Returns:
        ``"png"`` / ``"jpg"`` / ``"gif"`` / ``"image"`` / ``"video"`` /
        ``"audio"``；无法识别返回 None。
    """
    ext = Path(path_or_ext).suffix.lower().lstrip(".")
    if not ext:
        return None
    if ext == "png":
        return "png"
    if ext in _JPG_EXTS:
        return "jpg"
    if ext == "gif":
        return "gif"
    if f".{ext}" in IMAGE_EXTS:
        return "image"
    if f".{ext}" in VIDEO_EXTS:
        return "video"
    if f".{ext}" in AUDIO_EXTS:
        return "audio"
    return None


# =============================================================================
# 设置快照的派生逻辑（供面板与 CompressInterface 兼容 API 共用）
# =============================================================================
def settings_mode(program: str, tool_opts: dict) -> str:
    """某后端的压缩模式（无损 / 有损），逻辑对齐旧 CompressInterface。"""
    if program == "jpegoptim":
        return tool_opts["jpegoptim"].get("jo_mode", "lossless")
    if program in ("oxipng", "gifsicle"):
        return "lossless"
    return "lossy"  # ffmpeg / pillow


def settings_quality(program: str, tool_opts: dict) -> int:
    """某后端的质量值（0~100）。"""
    if program == "jpegoptim":
        o = tool_opts["jpegoptim"]
        return int(o.get("jo_max", 85)) if o.get("jo_mode") == "lossy" else 100
    if program == "pillow":
        return int(tool_opts["pillow"].get("pil_quality", 95))
    return 100


def settings_opts(program: str, tool_opts: dict) -> dict:
    """某后端的参数字典（深拷贝，入队后不再被 UI 改动影响）。"""
    return copy.deepcopy(tool_opts.get(program, {}))


def _default_tool_opts() -> dict:
    """后端参数默认值（照搬旧 CompressInterface._tool_opts，行为零漂移）。

    不直接用 :func:`compressor.param_defaults`：oxipng 的 ``filter`` / ``zc``
    在参数表里 default=None 会被跳过，且 oxipng strip 默认（表=safe）与压缩
    设置历史默认（none）不一致；此处显式固定历史默认值。
    """
    return {
        "oxipng": {
            "level": 3,
            "interlace": True,
            "strip": "none",
            "filter": 0,
            "zc": 6,
            "alpha": False,
        },
        "jpegoptim": {
            "jo_mode": "lossless",
            "jo_max": 85,
            "jo_strip": "none",
            "jo_progressive": "auto",
            "jo_threshold": 0,
            "jo_preserve": True,
            "jo_retry": False,
        },
        "gifsicle": {"gs_optimize": 3, "gs_loop": 0, "gs_lossy": 0},
        "pillow": {
            "pil_quality": 95,
            "pil_optimize": True,
            "pil_progressive": True,
            "pil_subsampling": "4:4:4",
        },
        "ffmpeg": compressor.ffmpeg_param_defaults(),
    }


# =============================================================================
# 面板
# =============================================================================
class CompressTaskPanel(QWidget):
    """「创建压缩任务」的设置面板（按文件类型路由）。

    Attributes:
        kind: ``compress_kind`` 的返回值，决定候选后端与 FFmpeg 类别。
        settings(): 产出冻结设置快照（program/target/mode/quality/opts/输出三件套）。
        输出位置三件套与主界面「压缩 → 输出位置」双向同步：
            - 面板改动 → 写回 ``cfg.compressMode/Suffix/Folder`` 并回调
              ``set_output_sync`` 注册的同步函数（主界面用它刷新自己的控件）；
            - 面板初始化 → 从 cfg 读取（主界面改动已落 cfg）。
    """

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        if kind not in KIND_BACKENDS:
            raise ValueError(f"未知压缩文件类型：{kind!r}")
        self.kind = kind
        self._backends = list(KIND_BACKENDS[kind])
        self._tool_opts = _default_tool_opts()
        # 输出位置状态（与 cfg.compressMode/Suffix/Folder 同源）
        self._output_mode = cfg.compressMode.value
        self._suffix = cfg.compressSuffix.value or ""
        self._folder = cfg.compressFolder.value or ""
        self._output_sync = None
        self._picking = False

        # 供 retranslate / 语言切换后重建下拉的引用
        self._param_rows: list[tuple] = []
        self._ff_setters: dict = {}
        self._ff_cat_headers: dict = {}
        self._ff_profile_labels: dict = {}
        self._ff_profile_combos: dict = {}
        self._switches: list[SwitchButton] = []
        self._sections: dict[str, QWidget] = {}

        apply_transparent(self)
        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(14)

        # ------------------------------------------------------------------
        # 压缩程序（按文件类型路由）
        # ------------------------------------------------------------------
        mapping = [(tr(compressor.BACKENDS_BY_ID[b].i18n_key), b) for b in self._backends]
        self.backendCombo = ComboBox()
        for disp, _val in mapping:
            self.backendCombo.addItem(disp)
        from .base import bind_combo_mapping

        bind_combo_mapping(self.backendCombo, mapping)
        select_combo_value(self.backendCombo, self._backends[0])
        self.backendCombo.currentTextChanged.connect(
            lambda _t: self._on_backend(combo_value(self.backendCombo))
        )
        vb.addWidget(field_row(tr("advanced.compression.backend"), self.backendCombo))
        self._backend_mapping = mapping

        # ------------------------------------------------------------------
        # 后端参数分区（只建本类型可用的后端）
        # ------------------------------------------------------------------
        self._backend_container = QWidget()
        apply_transparent(self._backend_container)
        bly = QVBoxLayout(self._backend_container)
        bly.setContentsMargins(0, 0, 0, 0)
        bly.setSpacing(16)
        if "oxipng" in self._backends:
            self._sections["oxipng"] = self._backend_section(
                "oxipng", self._build_oxipng()
            )
            bly.addWidget(self._sections["oxipng"])
        if "jpegoptim" in self._backends:
            self._sections["jpegoptim"] = self._backend_section(
                "jpegoptim", self._build_jpegoptim()
            )
            bly.addWidget(self._sections["jpegoptim"])
        if "gifsicle" in self._backends:
            self._sections["gifsicle"] = self._backend_section(
                "gifsicle", self._build_gifsicle()
            )
            bly.addWidget(self._sections["gifsicle"])
        if "pillow" in self._backends:
            self._sections["pillow"] = self._backend_section(
                "pillow", self._build_pillow()
            )
            bly.addWidget(self._sections["pillow"])
        if "ffmpeg" in self._backends:
            ff_cat = KIND_FF_CATEGORY.get(self.kind) or "image"
            self._sections["ffmpeg"] = self._backend_section(
                "ffmpeg", self._build_ffmpeg(ff_cat)
            )
            bly.addWidget(self._sections["ffmpeg"])
        vb.addWidget(self._backend_container)

        # ------------------------------------------------------------------
        # 输出位置（与大组件「压缩 → 输出位置」同步）
        # ------------------------------------------------------------------
        out_box = QWidget()
        apply_transparent(out_box)
        olv = QVBoxLayout(out_box)
        olv.setContentsMargins(0, 0, 0, 0)
        olv.setSpacing(8)
        self.outputSwitch = SwitchButton()
        self.outputSwitch.checkedChanged.connect(self._on_output_mode)
        olv.addWidget(field_row(tr("compress.output.mode"), self.outputSwitch))
        self.suffixEdit = QLineEdit(self._suffix)
        self.suffixEdit.setPlaceholderText(tr("compress.output.suffix_hint"))
        self.suffixEdit.textChanged.connect(self._on_suffix_changed)
        # V0.8.21 Bug2：必须持有整行的引用。field_row 产出的是「标签 + 输入框」
        # 一整行，只隐藏 suffixEdit 会把「文件名后缀」这个标签单独留在行里，
        # 被布局居中显示成一句莫名其妙的话。
        self._suffixRow = field_row(tr("compress.output.suffix"), self.suffixEdit)
        olv.addWidget(self._suffixRow)
        self.folderEdit = QLineEdit(self._folder)
        self.folderEdit.setReadOnly(True)
        self.browseBtn = icon_btn(FIF.FOLDER, self)
        self.browseBtn.setFixedSize(34, 34)
        self.browseBtn.clicked.connect(self._pick_output)
        frow = QHBoxLayout()
        frow.addWidget(self.folderEdit, 1)
        frow.addWidget(self.browseBtn)
        self._folderRow = field_row(tr("compress.output.folder"), frow)
        olv.addWidget(self._folderRow)
        vb.addWidget(out_box)
        self._apply_output_mode()
        self._restyle_switches()

        # 初始只显示默认后端的分区
        self._on_backend(self._backends[0])
        vb.addStretch(1)

    # ------------------------------------------------------------------
    # 设置快照
    # ------------------------------------------------------------------
    def settings(self) -> dict:
        """产出冻结的压缩设置（入队时按此快照执行）。"""
        program = combo_value(self.backendCombo) or self._backends[0]
        return {
            "program": program,
            "target": "same",
            "mode": settings_mode(program, self._tool_opts),
            "quality": settings_quality(program, self._tool_opts),
            "opts": settings_opts(program, self._tool_opts),
            "output_mode": self._output_mode,
            "suffix": self._suffix,
            "folder": self._folder,
        }

    # ------------------------------------------------------------------
    # 输出位置同步
    # ------------------------------------------------------------------
    def set_output_sync(self, cb) -> None:
        """注册输出位置变更回调：``cb(mode, suffix, folder)``。"""
        self._output_sync = cb

    def _push_output_sync(self):
        if self._output_sync is not None:
            try:
                self._output_sync(self._output_mode, self._suffix, self._folder)
            except Exception:  # noqa: BLE001 - 同步失败不应阻断面板交互
                pass

    def _on_output_mode(self, checked: bool):
        self._output_mode = "same" if checked else "fixed"
        cfg.compressMode.value = self._output_mode
        self._apply_output_mode()
        self._push_output_sync()

    def _on_suffix_changed(self, text: str):
        self._suffix = text
        cfg.compressSuffix.value = text
        self._push_output_sync()

    def _apply_output_mode(self):
        same = self._output_mode == "same"
        self.outputSwitch.setChecked(same)
        self.outputSwitch.setText(
            tr("compress.output.same") if same else tr("compress.output.fixed")
        )
        # 整行显隐，与 _folderRow 对称：后缀只在「保存在源文件旁」时有意义，
        # 切到「指定文件夹」后连标签一起收掉。
        self._suffixRow.setVisible(same)
        self._folderRow.setVisible(not same)

    def _pick_output(self):
        if self._picking:
            return
        self._picking = True
        try:
            d = QFileDialog.getExistingDirectory(
                self.window(), tr("compress.output.browse"), self._folder or ""
            )
            if d:
                self._folder = d
                cfg.compressFolder.value = d
                self.folderEdit.setText(d)
                self._push_output_sync()
        finally:
            self._picking = False

    def _restyle_switches(self):
        for sw in self._switches:
            sw.setOnText(tr("common.on"))
            sw.setOffText(tr("common.off"))

    # ------------------------------------------------------------------
    # 后端切换
    # ------------------------------------------------------------------
    def _on_backend(self, bid: str):
        for key, section in self._sections.items():
            section.setVisible(key == bid)

    # ------------------------------------------------------------------
    # 分区 / 参数行构造（移植自 CompressInterface）
    # ------------------------------------------------------------------
    def _backend_section(self, key: str, inner: QWidget) -> QWidget:
        w = QWidget()
        apply_transparent(w)
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)
        hdr = StrongBodyLabel(tr(f"advanced.compression.{key}"))
        apply_text(hdr, sub_text(), transparent=True)
        ly.addWidget(hdr)
        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {tokens.BORDER}; border: none;")
        ly.addWidget(rule)
        ly.addWidget(inner)
        w._header = hdr
        w._rule = rule
        return w

    def _param_row(self, key: str, control, label_width: int = 96):
        fr = field_row(tr(key), control, label_width=label_width)
        self._param_rows.append((fr, key))
        return fr

    # -- oxipng ---------------------------------------------------------
    def _build_oxipng(self):
        grp = self._tool_opts["oxipng"]
        w = QWidget()
        apply_transparent(w)
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)
        lvl = QSlider(Qt.Orientation.Horizontal)
        lvl.setRange(0, 6)
        lvl.setValue(int(grp["level"]))
        lvl_label = QLabel(str(grp["level"]))
        lvl.valueChanged.connect(
            lambda v: (grp.__setitem__("level", v), lvl_label.setText(str(v)))
        )
        row = QHBoxLayout()
        row.addWidget(lvl_label)
        row.addWidget(lvl, 1)
        fr = self._param_row("advanced.level", row)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.level")
        inter = SwitchButton()
        inter.setChecked(bool(grp["interlace"]))
        inter.checkedChanged.connect(lambda b: grp.__setitem__("interlace", b))
        self._switches.append(inter)
        fr = self._param_row("advanced.interlace", inter)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.interlace")
        strip = self._make_combo(
            [
                (tr("advanced.strip.none"), "none"),
                (tr("advanced.strip.safe"), "safe"),
                (tr("advanced.strip.all"), "all"),
            ],
            grp["strip"],
            lambda v: grp.__setitem__("strip", v),
        )
        fr = self._param_row("advanced.strip", strip)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.strip")
        filt = self._make_combo(
            [
                (tr("advanced.filter.none"), 0),
                (tr("advanced.filter.sub"), 1),
                (tr("advanced.filter.up"), 2),
                (tr("advanced.filter.average"), 3),
                (tr("advanced.filter.paeth"), 4),
                (tr("advanced.filter.mixed"), 5),
            ],
            grp["filter"],
            lambda v: grp.__setitem__("filter", int(v)),
        )
        fr = self._param_row("advanced.filter", filt)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.filter")
        zc = QSlider(Qt.Orientation.Horizontal)
        zc.setRange(1, 9)
        zc.setValue(int(grp["zc"]))
        zc_label = QLabel(str(grp["zc"]))
        zc.valueChanged.connect(
            lambda v: (grp.__setitem__("zc", v), zc_label.setText(str(v)))
        )
        zc_row = QHBoxLayout()
        zc_row.addWidget(zc_label)
        zc_row.addWidget(zc, 1)
        fr = self._param_row("advanced.zc", zc_row)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.zc")
        alpha = SwitchButton()
        alpha.setChecked(bool(grp["alpha"]))
        alpha.checkedChanged.connect(lambda b: grp.__setitem__("alpha", b))
        self._switches.append(alpha)
        fr = self._param_row("advanced.alpha", alpha)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.alpha")
        return w

    # -- jpegoptim ------------------------------------------------------
    def _build_jpegoptim(self):
        grp = self._tool_opts["jpegoptim"]
        w = QWidget()
        apply_transparent(w)
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)
        jo_mode = self._make_combo(
            [
                (tr("advanced.jo.mode.lossless"), "lossless"),
                (tr("advanced.jo.mode.lossy"), "lossy"),
            ],
            grp["jo_mode"],
            lambda v: (grp.__setitem__("jo_mode", v), self._sync_jo_max(v)),
        )
        fr = self._param_row("advanced.jo.mode", jo_mode)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.jo.mode")
        jo_max = QSlider(Qt.Orientation.Horizontal)
        jo_max.setRange(0, 100)
        jo_max.setValue(int(grp["jo_max"]))
        jo_max_spin = QSpinBox()
        jo_max_spin.setRange(0, 100)
        jo_max_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        jo_max_spin.setValue(int(grp["jo_max"]))
        jo_max.valueChanged.connect(
            lambda v: (grp.__setitem__("jo_max", v), jo_max_spin.setValue(v))
        )
        jo_max_spin.valueChanged.connect(
            lambda v: (grp.__setitem__("jo_max", v), jo_max.setValue(v))
        )
        jm_row = QHBoxLayout()
        jm_row.addWidget(jo_max, 1)
        jm_row.addWidget(jo_max_spin)
        jo_max_fr = self._param_row("advanced.jo.max", jm_row)
        ly.addWidget(jo_max_fr)
        attach_help(jo_max_fr, "advanced.help.jo.max")
        jo_strip = self._make_combo(
            [
                (tr("advanced.jo.strip.none"), "none"),
                (tr("advanced.jo.strip.meta"), "meta"),
                (tr("advanced.jo.strip.exif"), "exif"),
                (tr("advanced.jo.strip.icc"), "icc"),
                (tr("advanced.jo.strip.all"), "all"),
            ],
            grp["jo_strip"],
            lambda v: grp.__setitem__("jo_strip", v),
        )
        fr = self._param_row("advanced.jo.strip", jo_strip)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.jo.strip")
        jo_prog = self._make_combo(
            [
                (tr("advanced.jo.prog.auto"), "auto"),
                (tr("advanced.jo.prog.keep"), "keep"),
                (tr("advanced.jo.prog.progressive"), "progressive"),
                (tr("advanced.jo.prog.normal"), "normal"),
            ],
            grp["jo_progressive"],
            lambda v: grp.__setitem__("jo_progressive", v),
        )
        fr = self._param_row("advanced.jo.prog", jo_prog)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.jo.prog")
        jo_thr = QSpinBox()
        jo_thr.setRange(0, 99)
        jo_thr.setSuffix("%")
        jo_thr.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        jo_thr.setValue(int(grp["jo_threshold"]))
        jo_thr.valueChanged.connect(lambda v: grp.__setitem__("jo_threshold", v))
        fr = self._param_row("advanced.jo.threshold", jo_thr)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.jo.threshold")
        jo_pres = SwitchButton()
        jo_pres.setChecked(bool(grp["jo_preserve"]))
        jo_pres.checkedChanged.connect(lambda b: grp.__setitem__("jo_preserve", b))
        self._switches.append(jo_pres)
        fr = self._param_row("advanced.jo.preserve", jo_pres)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.jo.preserve")
        jo_retry = SwitchButton()
        jo_retry.setChecked(bool(grp["jo_retry"]))
        jo_retry.checkedChanged.connect(lambda b: grp.__setitem__("jo_retry", b))
        self._switches.append(jo_retry)
        fr = self._param_row("advanced.jo.retry", jo_retry)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.jo.retry")
        self._jo_max_fr = jo_max_fr
        self._sync_jo_max(grp["jo_mode"])
        return w

    def _sync_jo_max(self, mode: str):
        if hasattr(self, "_jo_max_fr"):
            self._jo_max_fr.setEnabled(mode == "lossy")

    # -- gifsicle -------------------------------------------------------
    def _build_gifsicle(self):
        grp = self._tool_opts["gifsicle"]
        w = QWidget()
        apply_transparent(w)
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)
        lvl = QSlider(Qt.Orientation.Horizontal)
        lvl.setRange(1, 3)
        lvl.setValue(int(grp.get("gs_optimize", 3)))
        lvl_label = QLabel(str(grp.get("gs_optimize", 3)))
        lvl.valueChanged.connect(
            lambda v: (grp.__setitem__("gs_optimize", v), lvl_label.setText(str(v)))
        )
        row = QHBoxLayout()
        row.addWidget(lvl_label)
        row.addWidget(lvl, 1)
        fr = self._param_row("advanced.gifsicle.optimize", row)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.gifsicle.optimize")
        loop = QSpinBox()
        loop.setRange(0, 100)
        loop.setValue(int(grp.get("gs_loop", 0)))
        loop.valueChanged.connect(lambda v: grp.__setitem__("gs_loop", v))
        fr = self._param_row("advanced.gifsicle.loop", loop)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.gifsicle.loop")
        lossy = QSlider(Qt.Orientation.Horizontal)
        lossy.setRange(0, 200)
        lossy.setValue(int(grp.get("gs_lossy", 0)))
        lossy_label = QLabel(str(grp.get("gs_lossy", 0)))
        lossy.valueChanged.connect(
            lambda v: (grp.__setitem__("gs_lossy", v), lossy_label.setText(str(v)))
        )
        row = QHBoxLayout()
        row.addWidget(lossy_label)
        row.addWidget(lossy, 1)
        fr = self._param_row("advanced.gifsicle.lossy", row)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.gifsicle.lossy")
        return w

    # -- pillow ---------------------------------------------------------
    def _build_pillow(self):
        grp = self._tool_opts["pillow"]
        w = QWidget()
        apply_transparent(w)
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)
        pq = QSlider(Qt.Orientation.Horizontal)
        pq.setRange(0, 95)
        pq.setValue(int(grp["pil_quality"]))
        pq_spin = QSpinBox()
        pq_spin.setRange(0, 95)
        pq_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        pq_spin.setValue(int(grp["pil_quality"]))
        pq.valueChanged.connect(
            lambda v: (grp.__setitem__("pil_quality", v), pq_spin.setValue(v))
        )
        pq_spin.valueChanged.connect(
            lambda v: (grp.__setitem__("pil_quality", v), pq.setValue(v))
        )
        pq_row = QHBoxLayout()
        pq_row.addWidget(pq, 1)
        pq_row.addWidget(pq_spin)
        fr = self._param_row("advanced.pil.quality", pq_row)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.pil.quality")
        pil_opt = SwitchButton()
        pil_opt.setChecked(bool(grp["pil_optimize"]))
        pil_opt.checkedChanged.connect(lambda b: grp.__setitem__("pil_optimize", b))
        self._switches.append(pil_opt)
        fr = self._param_row("advanced.pil.optimize", pil_opt)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.pil.optimize")
        pil_prog = SwitchButton()
        pil_prog.setChecked(bool(grp["pil_progressive"]))
        pil_prog.checkedChanged.connect(lambda b: grp.__setitem__("pil_progressive", b))
        self._switches.append(pil_prog)
        fr = self._param_row("advanced.pil.progressive", pil_prog)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.pil.progressive")
        pil_sub = self._make_combo(
            [
                (tr("advanced.pil.sub.444"), "4:4:4"),
                (tr("advanced.pil.sub.422"), "4:2:2"),
                (tr("advanced.pil.sub.420"), "4:2:0"),
            ],
            grp["pil_subsampling"],
            lambda v: grp.__setitem__("pil_subsampling", v),
        )
        fr = self._param_row("advanced.pil.subsampling", pil_sub)
        ly.addWidget(fr)
        attach_help(fr, "advanced.help.pil.subsampling")
        return w

    # -- FFmpeg（仅显示与文件类型对应的类别）-----------------------------
    def _ff_profile_mapping(self, category: str) -> list:
        presets = compressor.FFMPEG_PRESETS.get(category, {})
        mapping = [(tr(f"ffmpeg.profile.{name}"), name) for name in presets.keys()]
        mapping.append((tr("ffmpeg.profile.custom"), "custom"))
        return mapping

    def _ff_profile_key(self, category: str) -> str:
        return {
            "video": "ff_v_profile",
            "audio": "ff_a_profile",
            "image": "ff_i_profile",
        }[category]

    def _build_ffmpeg(self, category: str):
        grp = self._tool_opts["ffmpeg"]
        w = QWidget()
        apply_transparent(w)
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(10)
        params = compressor.FFMPEG_PARAMS_BY_KIND.get(category, {})
        profile_key = self._ff_profile_key(category)

        hdr = QHBoxLayout()
        cat_lbl = StrongBodyLabel(tr(f"ffmpeg.cat.{category}"))
        apply_text(cat_lbl, sub_text(), transparent=True)
        prof_label = QLabel(tr("ffmpeg.quality_preset"))
        apply_text(prof_label, sub_text(), transparent=True)
        prof_combo = self._make_combo(
            self._ff_profile_mapping(category),
            grp.get(profile_key, "balanced"),
            lambda v: self._on_ff_profile(category, v),
        )
        hdr.addWidget(cat_lbl)
        hdr.addStretch(1)
        hdr.addWidget(prof_label)
        hdr.addWidget(prof_combo)
        ly.addLayout(hdr)
        self._ff_cat_headers[category] = cat_lbl
        self._ff_profile_labels[category] = prof_label
        self._ff_profile_combos[category] = prof_combo

        setters: dict = {}
        for pkey, spec in params.items():
            if pkey == profile_key:
                continue
            control, setter = self._build_ff_param(grp, pkey, spec)
            fr = self._param_row(f"ffmpeg.{pkey}", control)
            ly.addWidget(fr)
            attach_help(fr, f"ffmpeg.help.{pkey}")
            setters[pkey] = setter
        self._ff_setters[category] = setters
        return w

    def _build_ff_param(self, grp: dict, pkey: str, spec: dict):
        t = spec.get("type")
        if t == "bool":
            ctl = SwitchButton()
            ctl.setChecked(bool(grp.get(pkey, spec.get("default", False))))
            ctl.checkedChanged.connect(lambda b: grp.__setitem__(pkey, b))
            self._switches.append(ctl)
            return ctl, (lambda v: ctl.setChecked(bool(v)))
        if t == "choice":
            vals = spec.get("values", [])
            ov = spec.get("labels") or {}
            mapping = [(ov.get(v, compressor.FFMPEG_VALUE_LABELS.get(v, v)), v) for v in vals]
            ctl = self._make_combo(
                mapping, grp.get(pkey, spec.get("default")), lambda v: grp.__setitem__(pkey, v)
            )
            # V0.8.21 Bug1：视频编码器下拉必须过硬件门禁。此前静态列出
            # h264_nvenc / hevc_nvenc，AMD 与 Intel 用户选中后要等到真跑
            # ffmpeg 才炸 "Could not open encoder"。探测走后台线程 + 缓存，
            # 不阻塞对话框弹出；结果回来再把用不了的项置灰。
            if pkey == "ff_v_encoder":
                self._gate_encoder_combo(ctl)
            return ctl, (lambda v: select_combo_value(ctl, v))
        lo = int(spec.get("min", 0))
        hi = int(spec.get("max", 100))
        cur = int(grp.get(pkey, spec.get("default", lo)) or lo)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(cur)
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        spin.setValue(cur)
        slider.valueChanged.connect(lambda v: (grp.__setitem__(pkey, v), spin.setValue(v)))
        spin.valueChanged.connect(lambda v: (grp.__setitem__(pkey, v), slider.setValue(v)))
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(spin)
        return row, (lambda v: (slider.setValue(int(v)), spin.setValue(int(v))))

    def _gate_encoder_combo(self, combo) -> None:
        """给视频编码器下拉框挂上硬件门禁（异步，结果回来再置灰）。

        Args:
            combo: 由 ``_make_combo`` 造出的、带 ``._mapping`` 的下拉框。
        """
        from .hw_probe import apply_encoder_gate, probe_async

        def _apply(result: dict):
            try:
                apply_encoder_gate(combo, result)
            except RuntimeError:
                pass  # 静默原因：探测返回时面板可能已关闭销毁

        probe_async(_apply)

    def _on_ff_profile(self, category: str, preset: str):
        grp = self._tool_opts["ffmpeg"]
        grp[self._ff_profile_key(category)] = preset
        if preset == "custom":
            return
        overrides = compressor.ffmpeg_preset_values(category, preset)
        setters = self._ff_setters.get(category, {})
        for k, v in overrides.items():
            if k in grp:
                grp[k] = v
            s = setters.get(k)
            if s:
                s(v)

    # -- 通用下拉 --------------------------------------------------------
    def _make_combo(self, mapping, current, on_change) -> ComboBox:
        from .base import bind_combo_mapping

        combo = ComboBox()
        for disp, _val in mapping:
            combo.addItem(disp)
        bind_combo_mapping(combo, mapping)
        select_combo_value(combo, current)
        combo.currentTextChanged.connect(lambda _t: on_change(combo_value(combo)))
        return combo
