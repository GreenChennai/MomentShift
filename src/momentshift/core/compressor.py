"""媒体压缩引擎，内置 oxipng + jpegoptim + gifsicle + FFmpeg 四个外部后端。

职责边界：
- 做：按目标格式路由到具体压缩后端、调用外部可执行文件、返回压缩前后体积。
- 不做：不做并发调度（交给 core/task_pool）；不决定输出路径（交给 core/output_path）。
- 不做：不拼 FFmpeg 参数（交给 core/ffmpeg_compress），本模块只负责「选它 / 调它」。

依赖：core/formatting、core/logger、core/platform、core/ffmpeg_compress；
被依赖：core/queue、gui/advanced_panel、gui/compress_interface。

后端
----
- ``pillow``    : 始终可用（Pillow 依赖）。通用兜底，覆盖 PNG/JPEG/WebP/BMP/TIFF。
- ``oxipng``    : 内置 ``resources/oxipng.exe``（v10.1.1）。PNG 无损压缩。
- ``jpegoptim`` : 内置 ``resources/jpegoptim.exe``（v1.5.6）。JPG/JPEG 压缩。
- ``gifsicle``  : GIF 专用（Pillow 压 GIF 会丢多帧动画）。
- ``ffmpeg``    : V0.8.16 新增。**唯一能处理视频与音频**的后端，同时也能压图片。

默认路由
--------
- ``png``        → oxipng
- ``jpg/jpeg``   → jpegoptim
- ``gif``        → gifsicle
- 音频 / 视频    → ffmpeg
- 其他图片类型   → pillow

当 oxipng / jpegoptim 的二进制缺失或调用失败时，写一条日志提示并自动切换到
Pillow 继续任务（需求 6），不会让任务失败。**但音视频不套用这条规则**：Pillow
根本打不开 mp4/mp3，静默兜底只会把「ffmpeg 没装」这条真实原因盖掉，换来一个
莫名其妙的解码错误。所以音视频路径上 ffmpeg 失败就是失败，错误信息直接透出。

参数键约定
----------
同一个 ``opts`` 字典同时承载全部后端的参数，按前缀区分，互不冲突：

- oxipng    : ``level`` ``interlace`` ``strip`` ``filter`` ``zc`` ``alpha``（历史键，无前缀）
- jpegoptim : ``jo_*``
- pillow    : ``pil_*``
- gifsicle  : ``gs_*``
- ffmpeg    : ``ff_v_*``（视频）``ff_a_*``（音频）``ff_i_*``（图片）
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg_compress
from .formatting import human_size
from .logger import get_logger
from .platform import (
    binary_name,
    bundled_tools_dir,
    resources_dir,
    run_silent,
    strip_exe_suffix,
    tools_dir,
)

log = get_logger("compressor")

# 格式集合
_PNG = {"png"}
_JPG = {"jpg", "jpeg"}
_WEBP = {"webp"}
_RASTER = {"bmp", "tiff", "tif", "gif"}
IMAGE_EXTS = _PNG | _JPG | _WEBP | _RASTER

# V0.8.16：压缩模块不再只吃图片。音视频集合直接引用 ffmpeg_compress 的定义，
# 避免两处各维护一份扩展名清单（加个 .m4v 只在一个地方漏改就会出现「能入队但
# 选不到后端」的怪状态）。
AUDIO_EXTS: frozenset[str] = frozenset(ffmpeg_compress.AUDIO_EXTS)
VIDEO_EXTS: frozenset[str] = frozenset(ffmpeg_compress.VIDEO_EXTS)
# 压缩模块能受理的全部扩展名。注意 ffmpeg 侧的图片集合比 IMAGE_EXTS 多了 avif，
# 取并集才不会把 avif 挡在门外。
MEDIA_EXTS: frozenset[str] = frozenset(IMAGE_EXTS) | frozenset(ffmpeg_compress.MEDIA_EXTS)


def media_kind(path_or_ext: str) -> str | None:
    """判断媒体类别：``"video"`` / ``"audio"`` / ``"image"``，无法识别返回 None。

    薄转发，让 GUI 与队列只依赖 compressor 一个入口，不必再 import 一层。
    """
    return ffmpeg_compress.media_kind(path_or_ext)


# =============================================================================
# 后端单一事实源（ODD-16）
# =============================================================================
@dataclass(frozen=True)
class BackendSpec:
    """一个压缩后端的静态描述。

    ``i18n_key`` 放在 core 而不是 GUI，是为了让「新增一个后端」变成纯粹的
    core 侧改动：GUI 只遍历 :data:`BACKENDS` 生成下拉项和状态文案，不再各自
    维护映射表。
    """

    bid: str  # 内部标识，同时也是 opts 前缀与配置里的取值
    label: str  # 展示名（品牌名，不翻译）
    formats: frozenset[str]  # 支持的目标格式
    lossless: bool
    lossy: bool
    tool: str | None  # 依赖的外部可执行文件名；None 表示纯 Python 内置
    i18n_key: str  # 下拉框标签的翻译键


# 全部压缩后端。**唯一事实源**。
# ODD-16 背景：GUI 侧原本另有一份 ``BACKEND_NAMES`` 硬编码字典，还有一处手写的
# 下拉项列表。 加 gifsicle 时只改了其中一处，导致 gifsicle 的下拉项一直
# 到  都不跟随语言切换——代码里当时留的注释就是「retranslate 漏了」。
# 现在两处都改成遍历本元组生成，新增后端不必再动 GUI。
# 顺序即界面下拉框的显示顺序。
BACKENDS: tuple[BackendSpec, ...] = (
    BackendSpec(
        bid="oxipng",
        label="oxipng",
        formats=frozenset(_PNG),
        lossless=True,
        lossy=False,
        tool="oxipng",
        i18n_key="advanced.compression.oxipng",
    ),
    BackendSpec(
        bid="jpegoptim",
        label="jpegoptim",
        formats=frozenset(_JPG),
        lossless=True,
        lossy=True,
        tool="jpegoptim",
        i18n_key="advanced.compression.jpegoptim",
    ),
    BackendSpec(
        bid="gifsicle",
        label="Gifsicle",
        formats=frozenset({"gif"}),
        lossless=True,
        lossy=True,
        tool="gifsicle",
        i18n_key="advanced.compression.gifsicle",
    ),
    BackendSpec(
        bid="ffmpeg",
        label="FFmpeg",
        # 唯一覆盖音视频的后端；图片也能压（webp/avif 由 libwebp/libaom 出）。
        formats=MEDIA_EXTS,
        lossless=True,  # flac / png / 无损 webp
        lossy=True,
        tool="ffmpeg",
        i18n_key="advanced.compression.ffmpeg",
    ),
    BackendSpec(
        bid="pillow",
        label="Pillow",
        formats=frozenset(_PNG | _JPG | _WEBP | _RASTER),
        lossless=True,
        lossy=True,
        tool=None,
        i18n_key="advanced.compression.pillow",
    ),
)

# 按 id 索引的后端表，避免调用方到处写 next(b for b in BACKENDS ...)。
BACKENDS_BY_ID: dict[str, BackendSpec] = {b.bid: b for b in BACKENDS}


def backend_label(bid: str) -> str:
    """后端展示名。未知 id 原样返回，便于日志里看出是哪儿传错了。"""
    spec = BACKENDS_BY_ID.get(bid)
    return spec.label if spec else str(bid)


# =============================================================================
# 中间文件后缀（RISK-10）
# =============================================================================
# 各后端产生的中间文件后缀。
# 原先这些字符串散落在五个函数里各写一遍，异常路径的清理也是各写各的，漏一条
# 就会在用户的输出目录里留下 ``xxx.png.oxi.tmp`` 这种垃圾。集中成常量之后，
# :func:`cleanup_temp_files` 能一次性兜底扫干净。
TMP_SUFFIX_OXIPNG = ".oxi.tmp"
TMP_SUFFIX_JPEGOPTIM = ".jo.tmp"
TMP_SUFFIX_GIFSICLE = ".gs.tmp"
TMP_SUFFIX_COMPRESS = ".cmp.tmp"
TMP_SUFFIX_TRANSCODE = ".tc.tmp"

_TMP_SUFFIXES: tuple[str, ...] = (
    TMP_SUFFIX_OXIPNG,
    TMP_SUFFIX_JPEGOPTIM,
    TMP_SUFFIX_GIFSICLE,
    TMP_SUFFIX_COMPRESS,
    TMP_SUFFIX_TRANSCODE,
)


def cleanup_temp_files(out_path: str) -> int:
    """清掉 ``out_path`` 对应的所有中间文件，返回实际删除的个数。

    兜底用：正常路径上每个后端都会在 ``finally`` 里清自己的临时文件，但任务被
    强杀（用户点清空 / 进程被结束）时轮不到 finally 执行。队列在任务收尾时再
    调一次这里，保证不给用户留垃圾。

    ``TMP_SUFFIX_TRANSCODE`` 后面还会再跟一层格式后缀（``.tc.tmp.png``），所以
    对它用前缀匹配扫同目录。V0.8.16 起 ``TMP_SUFFIX_COMPRESS`` 也可能带尾缀
    （``.cmp.tmp.mp4``）——ffmpeg 靠输出扩展名挑复用器，光给个 ``.tmp`` 它会直接
    报「Unable to find a suitable output format」，所以中间文件必须留真后缀。
    """
    removed = 0
    for suffix in _TMP_SUFFIXES:
        candidate = f"{out_path}{suffix}"
        if os.path.isfile(candidate):
            _rm(candidate)
            removed += 1

    base = os.path.basename(out_path)
    stage_prefixes = (
        f"{base}{TMP_SUFFIX_TRANSCODE}.",
        f"{base}{TMP_SUFFIX_COMPRESS}.",
    )
    directory = os.path.dirname(out_path) or "."
    try:
        names = os.listdir(directory)
    except OSError:
        names = []
    for name in names:
        if name.startswith(stage_prefixes):
            _rm(os.path.join(directory, name))
            removed += 1
    return removed


# =============================================================================
# 参数规格（供 UI 生成控件 / 校验）
# =============================================================================
# jpegoptim 可设置参数（依据 jpegoptim v1.5.6 ``--help``）。
JPEGOPTIM_PARAMS: dict[str, dict] = {
    "jo_mode": {
        "type": "choice",
        "values": ["lossless", "lossy"],
        "default": "lossless",
        "desc": "lossless=纯无损重排哈夫曼表；lossy=按最高质量上限重编码(-m)",
    },
    "jo_max": {
        "type": "int",
        "min": 0,
        "max": 100,
        "default": 85,
        "desc": "有损模式下的最高质量因子 (-m)，仅 jo_mode=lossy 生效",
    },
    "jo_strip": {
        "type": "choice",
        "values": ["none", "all", "exif", "icc", "com", "meta"],
        "default": "none",
        "desc": "元数据剥离：none 全保留 / all 全剥离 / meta 剥离 EXIF+IPTC+XMP+注释（保留 ICC）",
    },
    "jo_progressive": {
        "type": "choice",
        "values": ["keep", "auto", "progressive", "normal"],
        "default": "auto",
        "desc": "keep 不改动 / auto 自动选更小者 / progressive 强制渐进 / normal 强制基线",
    },
    "jo_threshold": {
        "type": "int",
        "min": 0,
        "max": 99,
        "default": 0,
        "desc": "增益低于该百分比时保留原文件 (-T)，0 表示不设阈值",
    },
    "jo_preserve": {
        "type": "bool",
        "default": True,
        "desc": "保留原文件时间戳 (-p)",
    },
    "jo_retry": {
        "type": "bool",
        "default": False,
        "desc": "递归重试直到文件大小不再变化 (-r)，更慢但压得更狠",
    },
}

# Pillow 可设置参数（ 需求 4）。
PILLOW_PARAMS: dict[str, dict] = {
    "pil_quality": {
        "type": "int",
        "min": 0,
        "max": 95,
        "default": 95,
        "desc": "压缩质量，0=最差 95=最佳（Pillow 官方不建议超过 95）",
    },
    "pil_optimize": {
        "type": "bool",
        "default": True,
        "desc": "开启额外优化（多花一轮编码换更小体积）",
    },
    "pil_progressive": {
        "type": "bool",
        "default": True,
        "desc": "生成渐进式 JPEG",
    },
    "pil_subsampling": {
        "type": "choice",
        "values": ["4:4:4", "4:2:2", "4:2:0"],
        "default": "4:4:4",
        "desc": "色度采样：4:4:4 最佳画质 / 4:2:2 中等 / 4:2:0 最大压缩",
    },
}

# oxipng 可设置参数（沿用  键名，保持配置向后兼容）。
OXIPNG_PARAMS: dict[str, dict] = {
    "level": {"type": "int", "min": 0, "max": 6, "default": 3, "desc": "优化等级 (--opt)"},
    "interlace": {"type": "bool", "default": False, "desc": "生成隔行 PNG"},
    "strip": {
        "type": "choice",
        "values": ["none", "safe", "all"],
        "default": "safe",
        "desc": "元数据剥离",
    },
    "filter": {"type": "int", "min": 0, "max": 5, "default": None, "desc": "行过滤器"},
    "zc": {"type": "int", "min": 1, "max": 9, "default": None, "desc": "zlib 压缩级别"},
    "alpha": {"type": "bool", "default": False, "desc": "alpha 通道优化"},
}

# Gifsicle 可设置参数，取值依据 gifsicle 官方文档。
GIFSICLE_PARAMS: dict[str, dict] = {
    "gs_optimize": {
        "type": "int",
        "min": 1,
        "max": 3,
        "default": 3,
        "desc": "优化级别 (-O)：1 基础 / 2 更强 / 3 最强（默认 3）",
    },
    "gs_loop": {
        "type": "int",
        "min": 0,
        "max": 100,
        "default": 0,
        "desc": "循环次数 (-l)：0=无限循环（动图默认），n=播放 n 次",
    },
    "gs_lossy": {
        "type": "int",
        "min": 0,
        "max": 200,
        "default": 0,
        "desc": "有损压缩阈值 (--lossy)：0=纯无损，越大体积越小但画质损失越多",
    },
}


# FFmpeg 可设置参数（V0.8.16）。规格定义在 core/ffmpeg_compress，这里只做转出，
# 让 GUI 保持「参数规格一律找 compressor 要」的单一入口。
FFMPEG_PARAMS: dict[str, dict] = ffmpeg_compress.FFMPEG_PARAMS
FFMPEG_PARAMS_BY_KIND: dict[str, dict[str, dict]] = ffmpeg_compress.PARAMS_BY_KIND
FFMPEG_PRESETS: dict[str, dict[str, dict]] = ffmpeg_compress.PRESETS


def param_defaults(backend: str) -> dict:
    """返回某后端的参数默认值字典。"""
    table = {
        "jpegoptim": JPEGOPTIM_PARAMS,
        "pillow": PILLOW_PARAMS,
        "oxipng": OXIPNG_PARAMS,
        "gifsicle": GIFSICLE_PARAMS,
        "ffmpeg": FFMPEG_PARAMS,
    }.get(backend, {})
    return {k: v["default"] for k, v in table.items() if v.get("default") is not None}


def ffmpeg_param_defaults(kind: str | None = None) -> dict:
    """FFmpeg 后端某一媒体类别的默认值；``kind=None`` 返回三类合集。

    单独开一个函数是因为 FFmpeg 的参数按 video/audio/image 分三段，UI 生成控件
    时只想要当前类别那一段，而 :func:`param_defaults` 的契约是「一个后端一张表」。
    """
    return ffmpeg_compress.param_defaults(kind)


def ffmpeg_preset_values(kind: str, preset: str) -> dict:
    """取 FFmpeg 某类别下某个预设（balanced/archive/small/...）的参数覆盖。"""
    return ffmpeg_compress.preset_values(kind, preset)


# =============================================================================
# 工具路径解析
# =============================================================================
def _bundled(exe_name: str) -> str | None:
    """在内置资源目录中查找可执行文件。

    Args:
        exe_name: 可执行文件名，例如 ``oxipng.exe``。
    Returns:
        存在时返回绝对路径字符串，缺失返回 None（调用方回退 Pillow）。
    """
    p = resources_dir() / exe_name
    return str(p) if p.is_file() else None


def _bundled_oxipng() -> str | None:
    return _bundled(binary_name("oxipng"))


def _bundled_jpegoptim() -> str | None:
    return _bundled(binary_name("jpegoptim"))


def _bundled_gifsicle() -> str | None:
    return _bundled(binary_name("gifsicle"))


def find_tool(name: str) -> str | None:
    """按 内置资源 → tools/ 目录 → gifsicle-bin 包 → 系统 PATH 的顺序查找工具。

    ``name`` 可带或不带 ``.exe``。找不到返回 ``None``。
    """
    stem = strip_exe_suffix(name)
    exe = binary_name(stem)

    # ffmpeg 有自己一套查找规则（内置目录、用户下载落点 ffmpeg_bin、数据重定向后
    # 的可写目录……全在 core/ffmpeg 里）。这里必须委派过去，否则「关于」页明明
    # 显示 FFmpeg 已就绪，压缩后端列表里却找不到它。
    if stem == "ffmpeg":
        from .ffmpeg import find_ffmpeg

        return find_ffmpeg()

    p = _bundled(exe)
    if p:
        return p

    t = tools_dir() / exe
    if t.is_file():
        return str(t)

    # 数据重定向后（安装到 Program Files），安装目录里随包分发的 tools/
    # 与用户下载落点不再是同一个目录，这里补一次只读回退。
    b = bundled_tools_dir() / exe
    if b.is_file():
        return str(b)

    # gifsicle-bin（pip 包，Python 版安装到 Scripts 目录）
    if stem == "gifsicle":
        g = _gifsicle_bin_exe()
        if g:
            return g

    return shutil.which(stem)


def _gifsicle_bin_exe() -> str | None:
    """定位 gifsicle-bin pip 包装的 gifsicle.exe（Scripts 目录）。"""
    try:
        import shutil

        p = shutil.which("gifsicle")
        if p and "gifsicle" in os.path.basename(p).lower():
            return p
    except Exception:
        log.debug(
            "在 PATH 中未找到 gifsicle，继续尝试其他后端"
        )  # 静默原因：仅探测后端，失败则走下一候选
    try:
        import importlib.metadata as _md

        dist = _md.distribution("gifsicle-bin")
        for f in dist.files or []:
            name = (f.name or "").lower()
            stem = name[:-4] if name.endswith(".exe") else name
            if "gifsicle" in stem:
                base = dist.locate_file(f)
                if os.path.isfile(str(base)):
                    return str(base)
    except Exception:
        log.debug("扫描压缩后端路径失败，回退到下一候选")  # 静默原因：探测失败不应阻断主流程
    return None


# =============================================================================
# 后端发现与路由
# =============================================================================
def available_backends() -> dict[str, dict]:
    """返回当前**实际可用**的压缩后端映射。

    ODD-16：条目全部由 :data:`BACKENDS` 生成，不再手写。需要外部二进制的后端只有
    在 :func:`find_tool` 找得到时才出现在结果里；内置后端（Pillow）恒定可用。
    """
    out: dict[str, dict] = {}
    for spec in BACKENDS:
        path = find_tool(spec.tool) if spec.tool else None
        if spec.tool and not path:
            continue
        out[spec.bid] = {
            "name": spec.label,
            "formats": set(spec.formats),
            "lossless": spec.lossless,
            "lossy": spec.lossy,
            "builtin": True,
            "path": path,
        }
    return out


def is_av(fmt: str) -> bool:
    """该格式是否为音频或视频（即「只有 ffmpeg 能处理」）。"""
    f = (fmt or "").lower().lstrip(".")
    return f in AUDIO_EXTS or f in VIDEO_EXTS


def default_backend(fmt: str) -> str:
    """v0.7.0 默认路由：png→oxipng，jpg/jpeg→jpegoptim，其他图片→pillow。
    v0.7.28：gif→gifsicle（Pillow 压缩 GIF 会丢失多帧动画）。
    V0.8.16：音频 / 视频→ffmpeg。

    对应后端不可用时回落到 ``pillow``（调用方负责写日志提示）；音视频没有
    Pillow 这条退路，即使 ffmpeg 没装也照样返回 ``ffmpeg``，让失败原因如实暴露。
    """
    f = (fmt or "").lower().lstrip(".")
    backs = available_backends()
    if f in _PNG and "oxipng" in backs:
        return "oxipng"
    if f in _JPG and "jpegoptim" in backs:
        return "jpegoptim"
    if f == "gif" and "gifsicle" in backs:
        return "gifsicle"
    if is_av(f):
        return "ffmpeg"
    if f not in IMAGE_EXTS and f in MEDIA_EXTS:
        # avif 这类 Pillow 默认装不了插件的图片格式，交给 ffmpeg
        return "ffmpeg"
    return "pillow"


def best_backend(fmt: str, mode: str = "lossless", preferred: str | None = None) -> str:
    """在满足 ``mode`` 的前提下选择后端；``preferred`` 优先。"""
    f = (fmt or "").lower().lstrip(".")
    backs = available_backends()

    def supports(bid: str) -> bool:
        b = backs.get(bid)
        if not b:
            return False
        if f not in b["formats"]:
            return False
        if mode == "lossless" and not b["lossless"]:
            return False
        if mode == "lossy" and not b["lossy"]:
            return False
        return True

    if preferred and supports(preferred):
        return preferred

    routed = default_backend(f)
    if supports(routed):
        return routed
    # 音视频只有 ffmpeg 一条路，兜到 Pillow 等于必定失败且错得莫名其妙
    return "ffmpeg" if is_av(f) else "pillow"


def fallback_to_pillow(backend: str, src: str) -> str:
    """检查后端二进制是否就位；缺失则提示并切到 pillow（需求 6）。

    公开（v0.8.0 起去掉下划线前缀）：压缩界面需要在派发任务前算出「实际会用哪个
    后端」并显示给用户，之前是跨模块调 ``compressor._fallback_to_pillow``——
    调私有函数等于把内部细节写死进 GUI，改名就会静默炸掉。

    V0.8.16 例外：ffmpeg 处理音视频时**不做**这个兜底。Pillow 打不开 mp4/mp3，
    切过去只会把「ffmpeg 缺失」换成一句看不懂的解码错误。
    """
    if backend == "oxipng" and not _bundled_oxipng():
        log.warning("oxipng 不可用（未找到内置二进制），已自动切换到 Pillow：%s", Path(src).name)
        return "pillow"
    if backend == "jpegoptim" and not find_tool("jpegoptim"):
        log.warning("jpegoptim 不可用（未找到内置二进制），已自动切换到 Pillow：%s", Path(src).name)
        return "pillow"
    if backend == "ffmpeg" and not ffmpeg_compress.available():
        fmt = Path(src).suffix.lower().lstrip(".")
        if is_av(fmt) or fmt not in IMAGE_EXTS:
            return backend  # 保持 ffmpeg，让 run() 返回「未找到 ffmpeg」的明确提示
        log.warning("FFmpeg 不可用（未找到可执行文件），已自动切换到 Pillow：%s", Path(src).name)
        return "pillow"
    return backend


# =============================================================================
# 压缩执行
# =============================================================================
def resolve_backend(fmt: str, backend: str | None) -> str:
    """把「用户选的后端」规整成「实际能跑的后端」。

    单独抽出来是因为压缩界面在派发任务**之前**就要显示「实际会用哪个后端」，
    以前 GUI 自己复刻了一遍这段纠偏逻辑，改一处漏一处。
    """
    fmt = (fmt or "").lower().lstrip(".")

    if backend in (None, "", "auto"):
        return default_backend(fmt)
    if backend not in BACKENDS_BY_ID:
        return default_backend(fmt)

    # 音视频只有 ffmpeg 能处理，选了别的一律纠正
    if is_av(fmt):
        return "ffmpeg"

    # 后端与格式不匹配时纠正（例如对 webp 选了 oxipng）
    if backend == "oxipng" and fmt not in _PNG:
        return default_backend(fmt)
    if backend == "jpegoptim" and fmt not in _JPG:
        return default_backend(fmt)
    if backend == "gifsicle" and fmt != "gif":
        return default_backend(fmt)
    if backend == "pillow" and fmt not in IMAGE_EXTS:
        return default_backend(fmt)
    return backend


def compress(
    src: str,
    dst: str,
    fmt: str,
    quality: int = 95,
    backend: str | None = None,
    opts: dict | None = None,
    on_progress=None,
    cancel_event=None,
) -> bool:
    """压缩单个媒体文件，``src`` → ``dst``。返回 ``True`` 表示成功。

    Args:
        on_progress: 0..100 进度回调，**仅 ffmpeg 后端**会调用。图片后端都是
            一次性调用外部程序，中途没有可上报的进度。
        cancel_event: 需实现 ``is_set()``；置位后 ffmpeg 会被终止。
    """
    fmt = (fmt or "").lower().lstrip(".")
    opts = dict(opts or {})

    backend = resolve_backend(fmt, backend)
    backend = fallback_to_pillow(backend, src)

    handlers = {
        "oxipng": _compress_oxipng,
        "jpegoptim": _compress_jpegoptim,
        "gifsicle": _compress_gifsicle,
        "pillow": _compress_pillow,
    }
    # Pillow 兜底只对图片有意义；音视频失败就是失败
    can_fallback = not is_av(fmt) and fmt in IMAGE_EXTS

    try:
        if backend == "ffmpeg":
            ok = _compress_ffmpeg(src, dst, fmt, quality, opts, on_progress, cancel_event)
        else:
            ok = handlers[backend](src, dst, fmt, quality, opts)
        # gifsicle 失败直接失败（Pillow 压 GIF 会丢帧，兜底无意义）
        if not ok and backend not in ("pillow", "gifsicle") and can_fallback:
            log.warning("%s 压缩未成功，已自动切换到 Pillow：%s", backend, Path(src).name)
            return _compress_pillow(src, dst, fmt, quality, opts)
        return ok
    except Exception:
        log.exception("%s 压缩异常：%s", backend, Path(src).name)
        if backend == "gifsicle" or not can_fallback:
            return False
        try:
            return _compress_pillow(src, dst, fmt, quality, opts)
        except Exception:
            log.exception("Pillow 兜底压缩同样失败：%s", Path(src).name)
            return False


# --- ffmpeg ---
# 最近一次 ffmpeg 压缩的失败摘要。ffmpeg 的错误信息（缺编码器、容器不支持……）
# 对用户是有价值的，但 :func:`compress` 的契约是返回 bool，只能靠这个单元格把
# detail 捎给 :func:`compress_auto`。压缩任务在 task_pool 里是串行进入本函数的
# ——同一个 src 从 compress_auto 调进来、立刻在同一线程读走——不存在跨任务串味。
_last_ffmpeg_error: str = ""


def _compress_ffmpeg(
    src: str,
    dst: str,
    fmt: str,
    quality: int,
    opts: dict,
    on_progress=None,
    cancel_event=None,
) -> bool:
    """FFmpeg 压缩（视频 / 音频 / 图片）。参数拼装全在 core/ffmpeg_compress。"""
    global _last_ffmpeg_error
    _last_ffmpeg_error = ""

    kind = ffmpeg_compress.media_kind(dst) or ffmpeg_compress.media_kind(src)
    ok, detail = ffmpeg_compress.run(
        src,
        dst,
        opts=opts,
        kind=kind,
        on_progress=on_progress,
        cancel_event=cancel_event,
    )
    if not ok:
        _last_ffmpeg_error = detail
        log.warning("FFmpeg 压缩失败：%s（%s）", Path(src).name, detail)
    return ok


# --- oxipng ---
def _compress_oxipng(src: str, dst: str, fmt: str, quality: int, opts: dict) -> bool:
    """oxipng PNG 无损压缩（内置二进制，原地优化临时副本）。"""
    ox = find_tool("oxipng")
    if not ox:
        return False

    tmp = dst + TMP_SUFFIX_OXIPNG
    try:
        shutil.copy2(src, tmp)
    except Exception:
        log.exception("oxipng: 复制临时文件失败")
        _rm(tmp)  # RISK-10：copy2 中途失败也可能已经落了半个文件
        return False

    args = [ox, "--quiet"]
    level = int(opts.get("level", 3) or 0)
    if 0 <= level <= 6:
        args.append(f"--opt={level}")
    if opts.get("interlace"):
        args.append("--interlace=1")
    strip_val = opts.get("strip", "safe")
    if strip_val in ("safe", "all"):
        args.append(f"--strip={strip_val}")
    filt = opts.get("filter")
    if filt is not None and 0 <= int(filt) <= 5:
        args.append(f"--filters={int(filt)}")
    zc = opts.get("zc")
    if zc is not None and 1 <= int(zc) <= 9:
        args.extend(["--zc", str(int(zc))])
    if opts.get("alpha"):
        args.append("--alpha")
    args.append(tmp)

    try:
        proc = run_silent(
            args, check=False, timeout=180, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            # oxipng 在文件已优化时返回 2，此时临时副本可视为最终结果
            if proc.returncode == 2 and ("already" in err.lower() or "optimized" in err.lower()):
                log.info("oxipng: 文件已是最优，跳过重新压缩")
            else:
                log.warning("oxipng exit=%d: %s", proc.returncode, err[:200])
                return False
        shutil.move(tmp, dst)
        return True
    except Exception:
        log.exception("oxipng 执行失败")
        return False
    finally:
        # RISK-10：成功路径上 move 已经把 tmp 拿走了，这里是 no-op；失败/异常路径
        # 则统一在这一处清理，不再靠每条 return 前手写一遍 _rm。
        _rm(tmp)


# --- jpegoptim ---
def _compress_jpegoptim(src: str, dst: str, fmt: str, quality: int, opts: dict) -> bool:
    """jpegoptim JPEG 压缩（内置二进制，原地优化临时副本）。

    jpegoptim 默认覆盖输入文件，因此先把源复制成临时文件再原地优化，
    最后移动到目标路径，避免污染源文件。
    """
    jo = find_tool("jpegoptim")
    if not jo:
        return False
    if fmt not in _JPG:
        return False

    tmp = dst + TMP_SUFFIX_JPEGOPTIM
    try:
        shutil.copy2(src, tmp)
    except Exception:
        log.exception("jpegoptim: 复制临时文件失败")
        _rm(tmp)  # RISK-10
        return False

    args = [jo, "--quiet"]

    mode = str(opts.get("jo_mode", "lossless")).lower()
    if mode == "lossy":
        jmax = opts.get("jo_max", opts.get("quality", quality))
        try:
            jmax = int(jmax)
        except (TypeError, ValueError):
            jmax = 85
        jmax = max(0, min(100, jmax))
        args.append(f"--max={jmax}")
        # 有损模式下即便体积没变小也要落盘，否则用户设定的质量上限不会生效
        args.append("--force")

    strip = str(opts.get("jo_strip", "none")).lower()
    if strip == "all":
        args.append("--strip-all")
    elif strip == "exif":
        args.append("--strip-exif")
    elif strip == "icc":
        args.append("--strip-icc")
    elif strip == "com":
        args.append("--strip-com")
    elif strip == "meta":
        args.extend(["--strip-exif", "--strip-iptc", "--strip-xmp", "--strip-com"])
    else:
        args.append("--strip-none")

    prog = str(opts.get("jo_progressive", "auto")).lower()
    if prog == "progressive":
        args.append("--all-progressive")
    elif prog == "normal":
        args.append("--all-normal")
    elif prog == "auto":
        args.append("--auto-mode")

    thr = opts.get("jo_threshold")
    try:
        thr = int(thr) if thr is not None else 0
    except (TypeError, ValueError):
        thr = 0
    if 0 < thr <= 99:
        args.append(f"--threshold={thr}")

    if opts.get("jo_preserve", True):
        args.append("--preserve")
    if opts.get("jo_retry"):
        args.append("--retry")

    args.append(tmp)

    try:
        run_silent(args, check=True, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if not os.path.isfile(tmp):
            return False
        shutil.move(tmp, dst)
        return True
    except Exception:
        log.exception("jpegoptim 执行失败")
        return False
    finally:
        _rm(tmp)  # RISK-10：统一清理点


# --- Gifsicle ---
def _compress_gifsicle(src: str, dst: str, fmt: str, quality: int, opts: dict) -> bool:
    """Gifsicle 动图压缩（v0.7.28）。

    Pillow 压缩 GIF 只保留第一帧，会丢失动画；Gifsicle 专为 GIF 优化：
    用法 ``gifsicle -O<level> -l <loop> [--lossy=n] -o out.gif in.gif``。
    """
    gs = find_tool("gifsicle")
    if not gs:
        return False

    tmp = dst + TMP_SUFFIX_GIFSICLE
    try:
        args = [gs]
        optimize = int(opts.get("gs_optimize", 3) or 3)
        if 1 <= optimize <= 3:
            args.append(f"-O{optimize}")
        loop = int(opts.get("gs_loop", 0) or 0)
        if 0 <= loop <= 100:
            # gifsicle 的 -l 参数可选，`-l 0` 会把 0 当输入文件 → 用 --loopcount=
            args.append(f"--loopcount={loop}")
        lossy = int(opts.get("gs_lossy", 0) or 0)
        if 0 < lossy <= 200:
            args.append(f"--lossy={lossy}")
        args += ["-o", tmp, src]

        proc = run_silent(
            args, check=False, timeout=180, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            log.warning("gifsicle exit=%d: %s", proc.returncode, err[:200])
            return False
        if not os.path.isfile(tmp):
            return False
        os.replace(tmp, dst)
        return True
    except Exception:
        log.exception("gifsicle 压缩异常：%s", Path(src).name)
        return False
    finally:
        # RISK-10：成功路径已 os.replace 走人，这里只会命中失败/异常残留。
        _rm(tmp)


# --- Pillow ---
_SUBSAMPLING_MAP = {"4:4:4": 0, "4:2:2": 1, "4:2:0": 2}


def _compress_pillow(src: str, dst: str, fmt: str, quality: int, opts: dict) -> bool:
    """Pillow 通用压缩（v0.7.0：quality / optimize / progressive / subsampling）。"""
    try:
        from PIL import Image
    except Exception:
        log.exception("Pillow 不可用")
        return False

    try:
        img = Image.open(src)
        img.load()

        save_fmt = (fmt or "").upper()
        if save_fmt in ("JPG", "JPEG"):
            save_fmt = "JPEG"
        elif save_fmt == "TIF":
            save_fmt = "TIFF"
        if save_fmt not in ("PNG", "JPEG", "WEBP", "BMP", "TIFF", "GIF"):
            save_fmt = (img.format or "PNG").upper()

        # quality：优先取 pil_quality，其次沿用调用方传入的 quality
        q = opts.get("pil_quality", opts.get("quality", quality))
        try:
            q = int(q)
        except (TypeError, ValueError):
            q = 95
        q = max(0, min(95, q))

        optimize = bool(opts.get("pil_optimize", True))
        progressive = bool(opts.get("pil_progressive", True))
        sub_key = str(opts.get("pil_subsampling", "4:4:4"))
        subsampling = _SUBSAMPLING_MAP.get(sub_key, 0)

        kw: dict = {}
        if save_fmt == "JPEG":
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            kw.update(
                quality=q, optimize=optimize, progressive=progressive, subsampling=subsampling
            )
        elif save_fmt == "PNG":
            kw["optimize"] = optimize
            lvl = opts.get("level")
            if lvl is not None:
                kw["compress_level"] = max(0, min(9, int(lvl)))
        elif save_fmt == "WEBP":
            kw["quality"] = q
            if opts.get("lossless"):
                kw["lossless"] = True
        elif save_fmt == "TIFF":
            kw["compression"] = "tiff_lzw"
        elif save_fmt == "GIF":
            kw["optimize"] = optimize

        if save_fmt == "JPEG" and (optimize or progressive):
            # optimize/progressive 需要把整帧塞进一个块，Pillow 默认 MAXBLOCK
            # (64KB) 对大图或高噪声图不够，会抛 "broken data stream"。
            try:
                from PIL import ImageFile

                w, h = img.size
                ImageFile.MAXBLOCK = max(ImageFile.MAXBLOCK, w * h * 4, 1 << 20)
            except Exception:
                log.debug(
                    "调整 PIL 解码块大小失败，使用默认值"
                )  # 静默原因：块大小为性能优化项，失败无关正确性

        try:
            img.save(dst, format=save_fmt, **kw)
        except OSError:
            # 极端情况下仍然溢出：退掉 optimize/progressive 保证任务不失败
            if save_fmt == "JPEG" and (optimize or progressive):
                log.warning("Pillow: optimize/progressive 编码溢出，已降级重试：%s", Path(src).name)
                kw["optimize"] = False
                kw["progressive"] = False
                _rm(dst)
                img.save(dst, format=save_fmt, **kw)
            else:
                raise
        return True
    except Exception:
        log.exception("Pillow 压缩失败：%s", Path(src).name)
        return False


def _rm(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:  # 静默原因：临时文件清理失败非致命，交给操作系统回收
        pass


# =============================================================================
# 高层 API（供压缩模块 UI 使用）
# =============================================================================
def needs_conversion(src_ext: str, target_ext: str) -> bool:
    """判断压缩前是否需要先做格式转码。"""
    s = (src_ext or "").lower().lstrip(".")
    t = (target_ext or "").lower().lstrip(".")
    if not t or t in ("same", "none"):
        return False
    if s == t:
        return False
    if {s, t} == {"jpg", "jpeg"} or {s, t} == {"tif", "tiff"}:
        return False
    return True


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def compress_auto(
    src: str,
    out: str,
    mode: str = "lossless",
    quality: int = 95,
    opts: dict | None = None,
    preferred: str | None = None,
    on_progress=None,
    cancel_event=None,
) -> tuple[bool, str, int]:
    """压缩 ``src`` 到 ``out``，自动挑选后端。

    返回 ``(ok, detail, saved_bytes)``。压缩后反而变大时保留原文件内容，
    ``saved_bytes`` 记 0。

    ``on_progress`` / ``cancel_event`` 只有 ffmpeg 后端会用到（视频动辄几分钟，
    没有进度条的等待体验等同于卡死）。
    """
    opts = dict(opts or {})
    fmt = Path(src).suffix.lower().lstrip(".")
    backend = best_backend(fmt, mode, preferred)
    backend = fallback_to_pillow(backend, src)

    before = _size(src)
    # ffmpeg 靠输出扩展名挑复用器，中间文件必须留真后缀，否则直接报
    # 「Unable to find a suitable output format for xxx.cmp.tmp」。
    tmp = f"{out}{TMP_SUFFIX_COMPRESS}.{fmt}" if backend == "ffmpeg" and fmt else (
        f"{out}{TMP_SUFFIX_COMPRESS}"
    )
    try:
        ok = compress(
            src,
            tmp,
            fmt,
            quality,
            backend=backend,
            opts=opts,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        if not ok or not os.path.isfile(tmp):
            reason = _last_ffmpeg_error if backend == "ffmpeg" and _last_ffmpeg_error else ""
            if reason == "canceled":
                return False, "已取消", 0
            return False, (f"{backend} 压缩失败：{reason}" if reason else f"{backend} 压缩失败"), 0

        after = _size(tmp)
        if after >= before > 0:
            # 没压小就别换，直接把原文件放到目标位置
            if os.path.abspath(src) != os.path.abspath(out):
                shutil.copy2(src, out)
            return True, f"{backend}: {human_size(before)}（无可压缩空间）", 0

        os.replace(tmp, out)
        saved = before - after
        pct = (saved / before * 100) if before else 0
        return (True, f"{backend}: {human_size(before)} → {human_size(after)} (-{pct:.1f}%)", saved)
    except Exception as exc:
        log.exception("compress_auto 失败：%s", Path(src).name)
        return False, str(exc), 0
    finally:
        # RISK-10：任何一条返回路径都不再各自 _rm，统一在这里收尾；
        # 成功路径的 tmp 已被 os.replace 消费掉，_rm 是幂等的空操作。
        _rm(tmp)


def _ffmpeg_transcode_compress(
    src: str,
    out: str,
    target_fmt: str,
    opts: dict,
    on_progress=None,
    cancel_event=None,
) -> tuple[bool, str, int]:
    """音视频：换容器 + 重编码在一趟 ffmpeg 里做完。

    返回值与 :func:`transcode_and_compress` 一致。
    """
    tf = (target_fmt or "").lower().lstrip(".") or Path(src).suffix.lower().lstrip(".")
    before = _size(src)
    tmp = f"{out}{TMP_SUFFIX_COMPRESS}.{tf}"
    try:
        ok, detail = ffmpeg_compress.run(
            src,
            tmp,
            opts=opts,
            kind=ffmpeg_compress.media_kind(tmp) or ffmpeg_compress.media_kind(src),
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        if not ok or not os.path.isfile(tmp):
            if detail == "canceled":
                return False, "已取消", 0
            return False, detail or "FFmpeg 压缩失败", 0

        after = _size(tmp)
        os.replace(tmp, out)
        saved = max(0, before - after)
        pct = (saved / before * 100) if before else 0
        return True, f"{tf}: {human_size(before)} → {human_size(after)} (-{pct:.1f}%)", saved
    except Exception as exc:
        log.exception("FFmpeg 转码压缩失败：%s", Path(src).name)
        return False, str(exc), 0
    finally:
        _rm(tmp)


def transcode_and_compress(
    src: str,
    out: str,
    target_fmt: str,
    mode: str = "lossless",
    quality: int = 95,
    opts: dict | None = None,
    preferred: str | None = None,
    on_progress=None,
    cancel_event=None,
) -> tuple[bool, str, int]:
    """先把 ``src`` 转成 ``target_fmt``，再压缩到 ``out``。

    图片走「Pillow 转码 → compress_auto」两段式；音视频交给 ffmpeg 一趟做完。
    返回 ``(ok, detail, saved_bytes)``，``saved_bytes`` 相对原始文件计算。
    """
    opts = dict(opts or {})
    tf = (target_fmt or "").lower().lstrip(".")
    before = _size(src)
    sf = Path(src).suffix.lower().lstrip(".")

    # V0.8.16：音视频不走「Pillow 转码 → 再压缩」两段式。一来 Pillow 根本打不开
    # mp4/mp3，二来 ffmpeg 本来就能在一趟里同时换容器和重编码，多转一次只会白掉
    # 一代画质。
    if is_av(sf) or is_av(tf):
        return _ffmpeg_transcode_compress(src, out, tf, opts, on_progress, cancel_event)

    # 中转文件必须带真实格式后缀，Pillow 与后续 compress_auto 都靠后缀识别格式。
    stage = f"{out}{TMP_SUFFIX_TRANSCODE}.{tf or 'png'}"

    try:
        try:
            from PIL import Image

            img = Image.open(src)
            img.load()

            save_fmt = "JPEG" if tf in _JPG else tf.upper()
            if save_fmt == "TIF":
                save_fmt = "TIFF"
            if save_fmt == "JPEG" and img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")

            kw: dict = {}
            if save_fmt == "JPEG":
                kw.update(quality=max(0, min(95, int(quality))), optimize=True)
            elif save_fmt == "WEBP":
                kw["quality"] = max(0, min(100, int(quality)))
            img.save(stage, format=save_fmt, **kw)
        except Exception as exc:
            log.exception("转码失败：%s → %s", Path(src).name, tf)
            return False, f"转码失败: {exc}", 0

        ok, detail, _ = compress_auto(stage, out, mode, quality, opts, preferred)
        if not ok:
            return False, detail, 0

        after = _size(out)
        saved = max(0, before - after)
        pct = (saved / before * 100) if before else 0
        return (True, f"{tf}: {human_size(before)} → {human_size(after)} (-{pct:.1f}%)", saved)
    finally:
        # RISK-10：中转文件不属于用户产物，无论走哪条路都要删干净。
        _rm(stage)
