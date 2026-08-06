"""FFmpeg 压缩后端：视频 / 音频 / 图片三类媒体的体积优化。

职责边界：
- 做：按媒体类别挑选编码器、依据参数规格拼 ffmpeg 命令、执行并上报进度。
- 不做：不做并发调度（core/task_pool）；不决定输出路径（core/output_path）；
  不做格式转换的「预设表」（那是 core/presets 的活，服务于「转换」模块）。

依赖：core/ffmpeg、core/logger、core/platform；被依赖：core/compressor、
gui/compress_interface、gui/upscale_interface。

与 core/presets 的分工
----------------------
``presets.PROFILES`` 面向**转换**：用户明确要换容器/编码，参数偏保守高保真。
本模块面向**压缩**：容器通常不变（目标格式 = same），目标是「肉眼几乎无损但
体积显著变小」，所以默认值直接取压缩笔记里的「默认交付」档，而不是存档档。

参数键约定
----------
统一 ``ff_`` 前缀，按媒体类别再分三段，与 oxipng/jpegoptim/pillow 的键互不冲突：

- 视频 : ``ff_v_*``
- 音频 : ``ff_a_*``
- 图片 : ``ff_i_*``

参数取值依据《FFmpeg 压缩转换笔记》：
- 视频默认 ``libx264 -crf 23 -preset slow``（默认交付档），H.265 对应 CRF 28，
  AV1 对应 CRF 30——**跨编码器的 CRF 不能照搬**，这是笔记里点名的头号坑。
- 音频默认 ``libopus -b:a 96k``（音乐甜点），MP3 走 VBR ``-q:a 2`` 而不是
  320k CBR。
- 图片默认 ``libwebp -quality 80``（75~82 甜点区），AVIF 走
  ``libaom-av1 -crf 32 -cpu-used 6``。
- 优先级恒定：**编码器升级 > 提高 CRF > 调慢 preset**。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from . import proc_control
from .ffmpeg import find_ffmpeg
from .ffmpeg_progress import FFmpegProgressParser, probe_duration_ms
from .logger import get_logger
from .platform import popen_silent

log = get_logger("ffmpeg_compress")

ProgressCallback = Callable[[int], None]

# =============================================================================
# 媒体类别
# =============================================================================
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KIND_IMAGE = "image"
KINDS: tuple[str, ...] = (KIND_VIDEO, KIND_AUDIO, KIND_IMAGE)

# 扩展名一律**不带点**，与 compressor.IMAGE_EXTS 的风格保持一致
# （core/presets 里的那三份集合是带点的，别混用）。
VIDEO_EXTS: frozenset[str] = frozenset(
    {
        "mp4",
        "mkv",
        "mov",
        "webm",
        "avi",
        "flv",
        "wmv",
        "mpeg",
        "mpg",
        "ts",
        "m4v",
        "3gp",
        "vob",
    }
)
AUDIO_EXTS: frozenset[str] = frozenset(
    {
        "mp3",
        "wav",
        "flac",
        "aac",
        "m4a",
        "ogg",
        "oga",
        "opus",
        "wma",
        "ac3",
        "aiff",
        "ape",
    }
)
IMAGE_EXTS: frozenset[str] = frozenset(
    {
        "png",
        "jpg",
        "jpeg",
        "webp",
        "bmp",
        "tiff",
        "tif",
        "gif",
        "avif",
    }
)
MEDIA_EXTS: frozenset[str] = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS


def media_kind(path_or_ext: str) -> str | None:
    """判断媒体类别，返回 ``video`` / ``audio`` / ``image``，未知返回 ``None``。

    入参可以是完整路径、``.mp4`` 或 ``mp4``，一律归一化后再查表。
    """
    s = (path_or_ext or "").strip()
    if not s:
        return None
    # 注意不能无脑走 Path().suffix：``.png`` 会被当成「无后缀的隐藏文件」，
    # suffix 返回空串。先按裸后缀试一次，不中再当路径解析。
    ext = s.lower().lstrip(".")
    if ext not in MEDIA_EXTS:
        ext = Path(s).suffix.lower().lstrip(".")
    if ext in VIDEO_EXTS:
        return KIND_VIDEO
    if ext in AUDIO_EXTS:
        return KIND_AUDIO
    if ext in IMAGE_EXTS:
        return KIND_IMAGE
    return None


def available() -> bool:
    """ffmpeg 二进制是否就位。"""
    return bool(find_ffmpeg())


# =============================================================================
# 参数规格（供 UI 生成控件 / 校验 / 帮助气泡）
# =============================================================================
# 说明格式与 compressor.JPEGOPTIM_PARAMS 保持一致：
# ``type`` / ``values`` / ``default`` / ``min`` / ``max`` / ``desc``。

# 编码 preset 的统一命名。x264/x265 直接用；SVT-AV1 与 NVENC 在
# :func:`_map_preset` 里换算成各自的数字档 / pN 档。
PRESET_NAMES: tuple[str, ...] = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
)

# 下拉选项的双语展示名（中文 + 技术值）。UI 渲染时作为显示文案，
# 数据值仍用原始技术名写入 opts。冲突值（copy / none / keep 在不同参数里
# 含义不同）由各 spec 自己的 ``labels`` 字段单独覆盖，这里只放无歧义的值。
FFMPEG_VALUE_LABELS: dict[str, str] = {
    # 视频 / 音频 / 图片 编码器里的「auto」
    "auto": "自动 (auto)",
    # 视频编码器
    "libx264": "H.264 (libx264)",
    "libx265": "H.265 (libx265)",
    "libsvtav1": "AV1 (libsvtav1)",
    "libvpx-vp9": "VP9 (libvpx-vp9)",
    "h264_nvenc": "H.264 显卡加速 (h264_nvenc)",
    "hevc_nvenc": "H.265 显卡加速 (hevc_nvenc)",
    # 编码速度档
    "ultrafast": "最快 (ultrafast)",
    "superfast": "超级快 (superfast)",
    "veryfast": "非常快 (veryfast)",
    "faster": "很快 (faster)",
    "fast": "快 (fast)",
    "medium": "中等 (medium)",
    "slow": "慢 (slow)",
    "slower": "很慢 (slower)",
    "veryslow": "非常慢 (veryslow)",
    # 视频调优
    "film": "实拍影片 (film)",
    "animation": "动画 (animation)",
    "grain": "胶片颗粒 (grain)",
    "stillimage": "静帧图片 (stillimage)",
    "fastdecode": "快速解码 (fastdecode)",
    # 像素格式
    "yuv420p": "兼容全平台 (yuv420p)",
    # 音频编码器
    "aac": "AAC (aac)",
    "libopus": "Opus (libopus)",
    "libmp3lame": "MP3 (libmp3lame)",
    "flac": "FLAC 无损 (flac)",
    # 音频码率
    "48k": "48 kbps (48k)",
    "64k": "64 kbps (64k)",
    "96k": "96 kbps (96k)",
    "128k": "128 kbps (128k)",
    "160k": "160 kbps (160k)",
    "192k": "192 kbps (192k)",
    "256k": "256 kbps (256k)",
    "320k": "320 kbps (320k)",
    # 声道
    "2": "立体声 (2)",
    "1": "单声道 (1)",
    # 采样率
    "48000": "48 kHz (48000)",
    "44100": "44.1 kHz (44100)",
    "32000": "32 kHz (32000)",
    "24000": "24 kHz (24000)",
    # 图片编码器
    "libwebp": "WebP (libwebp)",
    "libaom-av1": "AV1 (libaom-av1)",
    "mjpeg": "JPEG (mjpeg)",
    "png": "PNG (png)",
}


FFMPEG_VIDEO_PARAMS: dict[str, dict] = {
    "ff_v_profile": {
        "type": "choice",
        "values": ["balanced", "archive", "small", "tiny", "custom"],
        "default": "balanced",
        "desc": "压缩预设：balanced 默认交付 / archive 高保真存档 / small 体积优先 / tiny 极限压缩",
    },
    "ff_v_encoder": {
        "type": "choice",
        "values": [
            "auto",
            "libx264",
            "libx265",
            "libsvtav1",
            "libvpx-vp9",
            "h264_nvenc",
            "hevc_nvenc",
            "copy",
        ],
        "labels": {"copy": "仅重封装不重编码 (copy)"},
        "default": "auto",
        "desc": (
            "视频编码器。auto 按容器自动选（webm→VP9，其余→H.264）；"
            "H.265/AV1 同画质体积更小但更慢；copy 不重编码只重封装"
        ),
    },
    "ff_v_crf": {
        "type": "int",
        "min": 0,
        "max": 63,
        "default": 23,
        "desc": (
            "画质系数，越大体积越小。**不同编码器的刻度不通用**："
            "H.264≈23、H.265≈28、AV1≈30、VP9≈31 才是同一画质"
        ),
    },
    "ff_v_preset": {
        "type": "choice",
        "values": list(PRESET_NAMES),
        "default": "slow",
        "desc": "编码速度档，越慢同画质体积越小。slow 是性价比甜点，veryslow 常只多省 1~2%",
    },
    "ff_v_tune": {
        "type": "choice",
        "values": ["none", "film", "animation", "grain", "stillimage", "fastdecode"],
        "labels": {"none": "保持原样 (none)"},
        "default": "none",
        "desc": "内容调优。录屏/动画选 animation 可再省 10~30%；实拍保持 none 或 film",
    },
    "ff_v_pixfmt": {
        "type": "choice",
        "values": ["yuv420p", "keep"],
        "labels": {"keep": "保持原格式 (keep)"},
        "default": "yuv420p",
        "desc": "像素格式。yuv420p 兼容所有播放器与浏览器；keep 保留源格式（可能无法在手机上播）",
    },
    "ff_v_maxwidth": {
        "type": "int",
        "min": 0,
        "max": 7680,
        "default": 0,
        "desc": "限制最大宽度，超出则等比缩小（高度自动取偶数）。0 = 保持原分辨率",
    },
    "ff_v_fps": {
        "type": "int",
        "min": 0,
        "max": 240,
        "default": 0,
        "desc": "限制最大帧率，超出才降帧。0 = 保持原帧率",
    },
    "ff_v_audio": {
        "type": "choice",
        "values": ["auto", "aac", "libopus", "copy", "none"],
        "labels": {"copy": "原样复制 (copy)", "none": "移除音轨 (none)"},
        "default": "auto",
        "desc": "视频里的音轨如何处理。auto 按容器选（webm→Opus，其余→AAC）；none 表示丢弃音轨",
    },
    "ff_v_abr": {
        "type": "choice",
        "values": ["64k", "96k", "128k", "160k", "192k", "256k"],
        "default": "128k",
        "desc": "音轨码率。128k AAC 对视频伴音足够，追求听感可上 192k",
    },
    "ff_v_faststart": {
        "type": "bool",
        "default": True,
        "desc": "把索引挪到文件头（+faststart），让 MP4/MOV 边下边播。仅对 MP4 系容器生效",
    },
}

FFMPEG_AUDIO_PARAMS: dict[str, dict] = {
    "ff_a_profile": {
        "type": "choice",
        "values": ["transparent", "balanced", "small", "custom"],
        "default": "balanced",
        "desc": "压缩预设：transparent 听感无损 / balanced 音乐甜点 / small 语音播客",
    },
    "ff_a_encoder": {
        "type": "choice",
        "values": ["auto", "libopus", "aac", "libmp3lame", "flac", "copy"],
        "labels": {"copy": "原样复制 (copy)"},
        "default": "auto",
        "desc": (
            "音频编码器。auto 按容器选；同码率下 Opus > AAC > MP3；"
            "flac 为无损（只压体积不损音质）"
        ),
    },
    "ff_a_bitrate": {
        "type": "choice",
        "values": ["48k", "64k", "96k", "128k", "160k", "192k", "256k", "320k"],
        "default": "96k",
        "desc": "有损编码码率。Opus 96k 已是音乐甜点、192k 基本透明；AAC 建议 128k~192k",
    },
    "ff_a_mp3q": {
        "type": "int",
        "min": 0,
        "max": 9,
        "default": 2,
        "desc": "MP3 的 VBR 质量档 (-q:a)，0 最好 9 最差。V2 已接近透明，别再用 320k CBR",
    },
    "ff_a_flac_level": {
        "type": "int",
        "min": 0,
        "max": 12,
        "default": 8,
        "desc": "FLAC 压缩级别，越大越慢体积越小。8 是收益拐点，12 只多省不到 1%",
    },
    "ff_a_channels": {
        "type": "choice",
        "values": ["keep", "2", "1"],
        "labels": {"keep": "保持原样 (keep)"},
        "default": "keep",
        "desc": "声道数。纯语音转单声道可直接省一半码率",
    },
    "ff_a_samplerate": {
        "type": "choice",
        "values": ["keep", "48000", "44100", "32000", "24000"],
        "labels": {"keep": "保持原样 (keep)"},
        "default": "keep",
        "desc": "采样率。Opus 内部固定 48k；语音降到 24k 还能再省一截",
    },
}

FFMPEG_IMAGE_PARAMS: dict[str, dict] = {
    "ff_i_profile": {
        "type": "choice",
        "values": ["high", "balanced", "small", "custom"],
        "default": "balanced",
        "desc": "压缩预设：high 近乎无损 / balanced 网页甜点 / small 缩略图优先",
    },
    "ff_i_encoder": {
        "type": "choice",
        "values": ["auto", "libwebp", "libaom-av1", "mjpeg", "png"],
        "default": "auto",
        "desc": "图片编码器。auto 按输出后缀选；WebP 比 JPEG 小 25~35%，AVIF 更小但编码慢",
    },
    "ff_i_quality": {
        "type": "int",
        "min": 1,
        "max": 100,
        "default": 80,
        "desc": "WebP / JPEG 质量。75~82 是肉眼难辨的甜点区，90 以上收益迅速衰减",
    },
    "ff_i_crf": {
        "type": "int",
        "min": 0,
        "max": 63,
        "default": 32,
        "desc": "AVIF 画质系数，越大体积越小。32 约等于 WebP 质量 80",
    },
    "ff_i_effort": {
        "type": "int",
        "min": 0,
        "max": 6,
        "default": 4,
        "desc": "WebP 编码耗时档 (-compression_level)，越大越慢体积越小",
    },
    "ff_i_lossless": {
        "type": "bool",
        "default": False,
        "desc": "无损模式。对 WebP 生效；截图/线稿类图片开无损常比有损还小",
    },
    "ff_i_maxwidth": {
        "type": "int",
        "min": 0,
        "max": 16384,
        "default": 0,
        "desc": "限制最大宽度，超出则等比缩小。0 = 保持原尺寸",
    },
}

# 三张表的并集，供 compressor.param_defaults 与 UI 统一查询。
FFMPEG_PARAMS: dict[str, dict] = {
    **FFMPEG_VIDEO_PARAMS,
    **FFMPEG_AUDIO_PARAMS,
    **FFMPEG_IMAGE_PARAMS,
}

PARAMS_BY_KIND: dict[str, dict[str, dict]] = {
    KIND_VIDEO: FFMPEG_VIDEO_PARAMS,
    KIND_AUDIO: FFMPEG_AUDIO_PARAMS,
    KIND_IMAGE: FFMPEG_IMAGE_PARAMS,
}


def param_defaults(kind: str | None = None) -> dict:
    """某一类（或全部）参数的默认值字典。"""
    table = PARAMS_BY_KIND.get(kind or "", FFMPEG_PARAMS)
    return {k: v["default"] for k, v in table.items() if v.get("default") is not None}


# =============================================================================
# 压缩预设
# =============================================================================
# 每个预设就是一组「参数覆盖值」。UI 侧选中预设后把这些值刷进控件，用户再动任何
# 一项就自动切到 custom——这样预设既是快捷入口，也不会锁死手动调参。
PRESETS: dict[str, dict[str, dict]] = {
    KIND_VIDEO: {
        # 笔记 §3「默认交付」：H.264 CRF 23 + slow，兼容性最好的通用档
        "balanced": {
            "ff_v_encoder": "auto",
            "ff_v_crf": 23,
            "ff_v_preset": "slow",
            "ff_v_pixfmt": "yuv420p",
            "ff_v_audio": "auto",
            "ff_v_abr": "128k",
            "ff_v_faststart": True,
        },
        # 笔记 §3「存档」：CRF 18，画质优先、体积其次
        "archive": {
            "ff_v_encoder": "auto",
            "ff_v_crf": 18,
            "ff_v_preset": "slow",
            "ff_v_pixfmt": "yuv420p",
            "ff_v_audio": "auto",
            "ff_v_abr": "192k",
            "ff_v_faststart": True,
        },
        # 编码器升级优先于抬 CRF：换 H.265 而不是把 x264 的 CRF 拉到 28
        "small": {
            "ff_v_encoder": "libx265",
            "ff_v_crf": 28,
            "ff_v_preset": "slow",
            "ff_v_pixfmt": "yuv420p",
            "ff_v_audio": "aac",
            "ff_v_abr": "128k",
            "ff_v_faststart": True,
        },
        # AV1，同画质再省一档，代价是编码时间
        "tiny": {
            "ff_v_encoder": "libsvtav1",
            "ff_v_crf": 32,
            "ff_v_preset": "slow",
            "ff_v_pixfmt": "yuv420p",
            "ff_v_audio": "libopus",
            "ff_v_abr": "96k",
            "ff_v_faststart": True,
        },
    },
    KIND_AUDIO: {
        "transparent": {
            "ff_a_encoder": "auto",
            "ff_a_bitrate": "192k",
            "ff_a_mp3q": 0,
            "ff_a_channels": "keep",
            "ff_a_samplerate": "keep",
        },
        "balanced": {
            "ff_a_encoder": "auto",
            "ff_a_bitrate": "96k",
            "ff_a_mp3q": 2,
            "ff_a_channels": "keep",
            "ff_a_samplerate": "keep",
        },
        # 语音/播客：单声道 + 低码率，Opus 在 48k 下仍可听
        "small": {
            "ff_a_encoder": "auto",
            "ff_a_bitrate": "48k",
            "ff_a_mp3q": 5,
            "ff_a_channels": "1",
            "ff_a_samplerate": "24000",
        },
    },
    KIND_IMAGE: {
        "high": {
            "ff_i_encoder": "auto",
            "ff_i_quality": 92,
            "ff_i_crf": 24,
            "ff_i_effort": 6,
            "ff_i_lossless": False,
        },
        "balanced": {
            "ff_i_encoder": "auto",
            "ff_i_quality": 80,
            "ff_i_crf": 32,
            "ff_i_effort": 4,
            "ff_i_lossless": False,
        },
        "small": {
            "ff_i_encoder": "auto",
            "ff_i_quality": 65,
            "ff_i_crf": 40,
            "ff_i_effort": 6,
            "ff_i_lossless": False,
        },
    },
}


def preset_values(kind: str, preset: str) -> dict:
    """取某个预设的参数覆盖表；``custom`` 或未知预设返回空字典。"""
    return dict(PRESETS.get(kind, {}).get(preset, {}))


# 各编码器「同画质」的推荐 CRF（笔记 §3 的换算表）。
# 用户切换编码器时 UI 用它顺手把 CRF 调到等效值，避免直接照搬上一个编码器的数字。
RECOMMENDED_CRF: dict[str, int] = {
    "libx264": 23,
    "libx265": 28,
    "libsvtav1": 30,
    "libvpx-vp9": 31,
    "h264_nvenc": 28,
    "hevc_nvenc": 33,
}


def recommended_crf(encoder: str) -> int:
    """换编码器时的等效 CRF 建议值。"""
    return RECOMMENDED_CRF.get((encoder or "").lower(), 23)


# =============================================================================
# 编码器选择
# =============================================================================
# 容器 → 可用视频编码器。写死是有意的：webm 塞 H.264 会直接被 ffmpeg 拒绝，
# 与其等运行时报错，不如在拼命令阶段就纠正回去。
_VIDEO_CONTAINER_DEFAULT: dict[str, str] = {
    "webm": "libvpx-vp9",
    "avi": "libx264",
    "flv": "libx264",
    "wmv": "libx264",
    "3gp": "libx264",
    "ts": "libx264",
    "mpg": "libx264",
    "mpeg": "libx264",
}
_WEBM_OK = {"libvpx-vp9", "libsvtav1"}
# MP4/MOV 系不收 VP9（技术上 ffmpeg 能写，但播放器普遍不认）
_MP4_BAD = {"libvpx-vp9"}

_AUDIO_CONTAINER_ENCODER: dict[str, str] = {
    "mp3": "libmp3lame",
    "flac": "flac",
    "wav": "pcm_s16le",
    "aiff": "pcm_s16be",
    "m4a": "aac",
    "aac": "aac",
    "ogg": "libvorbis",
    "oga": "libvorbis",
    "opus": "libopus",
    "wma": "wmav2",
    "ac3": "ac3",
}
# 容器只认这一个编码器时，用户选什么都得让路（否则 ffmpeg 直接报错）。
_AUDIO_CONTAINER_LOCKED = {"mp3", "flac", "wav", "aiff", "opus", "wma", "ac3", "aac"}
# 容器认几个编码器但不是全部。选了表外的一律回落到容器默认值。
_AUDIO_CONTAINER_ALLOWED: dict[str, frozenset[str]] = {
    "m4a": frozenset({"aac", "alac"}),
    "ogg": frozenset({"libvorbis", "libopus", "flac"}),
    "oga": frozenset({"libvorbis", "libopus", "flac"}),
}

_IMAGE_CONTAINER_ENCODER: dict[str, str] = {
    "webp": "libwebp",
    "avif": "libaom-av1",
    "jpg": "mjpeg",
    "jpeg": "mjpeg",
    "png": "png",
    "bmp": "bmp",
    "tiff": "tiff",
    "tif": "tiff",
    "gif": "gif",
}
# 由 image2 复用器承载的图片容器（只有它们认 ``-update``）。
_IMAGE2_CONTAINERS = frozenset({"png", "jpg", "jpeg", "bmp", "tiff", "tif"})


def _ext(path: str) -> str:
    return Path(path or "").suffix.lower().lstrip(".")


def _hw_encoder_ok(encoder: str) -> bool:
    """实测某个硬件编码器在本机能否真正打开。

    Args:
        encoder: 编码器名，如 ``h264_nvenc``。
    Returns:
        能用为 ``True``。探测本身出错时**保守返回 True**，把判断权交还给
        ffmpeg——宁可让它自己报错走降级重试，也不要因为探测环境异常就
        把本来可用的 GPU 编码器误杀。
    Notes:
        延迟导入 :mod:`core.hardware`：它会拉起 subprocess 探测，模块级导入
        会让所有引用 ffmpeg_compress 的地方都白等一次。结果有
        ``hardware._PROBE_CACHE`` 缓存，同一编码器只实测一次。
    """
    try:
        from .hardware import encoder_usable

        exe = find_ffmpeg()
        if not exe:
            return True
        return bool(encoder_usable(exe, encoder))
    except Exception:  # pragma: no cover - defensive
        log.debug("硬件编码器探测异常，交由 ffmpeg 自行判断：%s", encoder)
        return True


def pick_video_encoder(container: str, requested: str | None = None) -> str:
    """按容器与**本机硬件能力**纠正视频编码器选择。

    Args:
        container: 输出容器后缀，如 ``mp4`` / ``webm``。
        requested: 用户请求的编码器；``auto`` / 空 / 与容器不兼容时回落。
    Returns:
        最终使用的编码器名。
    Notes:
        V0.8.21 修复：此前只按容器判断，不问显卡。用户选了 ``h264_nvenc``
        但机器是 AMD 显卡时，参数会原样传给 ffmpeg，直到运行期才炸
        ``Could not open encoder``。现在先用
        :func:`core.hardware.encoder_usable` 实测（跑一帧 nullsrc 试编码，
        结果带缓存），不可用就直接降级到等价 CPU 编码器。
    """
    c = (container or "").lower().lstrip(".")
    req = (requested or "auto").lower()
    fallback = _VIDEO_CONTAINER_DEFAULT.get(c, "libx264")

    # ---- 硬件门禁：请求的是 GPU 编码器就先实测能不能用 ----
    if req in HW_TO_CPU_ENCODER:
        if not _hw_encoder_ok(req):
            cpu = HW_TO_CPU_ENCODER[req]
            log.warning("本机无法使用硬件编码器 %s，自动降级为 %s", req, cpu)
            req = cpu

    if req in ("", "auto"):
        return fallback
    if req == "copy":
        return "copy"
    if c == "webm" and req not in _WEBM_OK:
        log.info("webm 容器不支持 %s，改用 %s", req, fallback)
        return fallback
    if c in ("mp4", "mov", "m4v", "3gp") and req in _MP4_BAD:
        log.info("%s 容器不适合 %s，改用 libx264", c, req)
        return "libx264"
    if c in ("avi", "flv", "wmv", "ts", "mpg", "mpeg", "vob") and req not in (
        "libx264",
        "copy",
    ):
        # 这些老容器对新编码器支持很差，一律拉回 H.264
        log.info("%s 容器不支持 %s，改用 libx264", c, req)
        return "libx264"
    return req


def pick_audio_encoder(container: str, requested: str | None = None) -> str:
    """按容器纠正音频编码器选择（音频文件用）。"""
    c = (container or "").lower().lstrip(".")
    req = (requested or "auto").lower()
    locked = _AUDIO_CONTAINER_ENCODER.get(c)

    if req == "copy":
        return "copy"
    if c in _AUDIO_CONTAINER_LOCKED and locked:
        if req not in ("", "auto", locked):
            log.info("%s 容器只支持 %s，忽略 %s", c, locked, req)
        return locked
    if req in ("", "auto"):
        return locked or "libopus"
    allowed = _AUDIO_CONTAINER_ALLOWED.get(c)
    if allowed and req not in allowed:
        log.info("%s 容器不支持 %s，改用 %s", c, req, locked or "libopus")
        return locked or "libopus"
    return req


def pick_track_encoder(container: str, requested: str | None = None) -> str:
    """视频里**音轨**的编码器选择（与纯音频文件的规则不同）。"""
    c = (container or "").lower().lstrip(".")
    req = (requested or "auto").lower()
    if req in ("copy", "none"):
        return req
    if c == "webm":
        # webm 只收 Opus / Vorbis
        return "libopus"
    if req in ("", "auto"):
        return "aac"
    if req == "libopus" and c in ("avi", "flv", "wmv", "3gp"):
        return "aac"
    return req


def _map_preset(encoder: str, name: str) -> str:
    """把统一的 preset 名字换算成各编码器自己的档位表示。"""
    idx = PRESET_NAMES.index(name) if name in PRESET_NAMES else PRESET_NAMES.index("slow")
    if encoder == "libsvtav1":
        # SVT-AV1: 0(最慢) ~ 13(最快)。笔记推荐 6，正好对上 slow。
        return str([12, 11, 10, 9, 8, 7, 6, 4, 2][idx])
    if encoder.endswith("_nvenc"):
        # NVENC: p1(最快) ~ p7(最慢)
        return ["p1", "p2", "p3", "p3", "p4", "p5", "p6", "p7", "p7"][idx]
    return name


def _vp9_speed(name: str) -> str:
    """VP9 的 ``-speed``：0(最慢) ~ 5(最快)，笔记推荐 2。"""
    idx = PRESET_NAMES.index(name) if name in PRESET_NAMES else PRESET_NAMES.index("slow")
    return str([5, 5, 4, 4, 3, 3, 2, 1, 0][idx])


def _jpeg_qscale(quality: int) -> str:
    """质量 1~100 → mjpeg 的 ``-q:v`` 2~31（越小越好）。"""
    q = max(1, min(100, int(quality)))
    return str(max(2, min(31, round(31 - (q / 100.0) * 29))))


# =============================================================================
# 命令构建
# =============================================================================
def build_args(
    src: str,
    dst: str,
    opts: dict | None = None,
    kind: str | None = None,
    safe: bool = False,
) -> list[str]:
    """拼出一条完整的 ffmpeg 参数表（不含 ffmpeg 可执行文件本身）。

    Args:
        src: 输入文件。
        dst: 输出文件，其扩展名决定容器与默认编码器。
        opts: ``ff_*`` 参数字典；缺项按 :data:`FFMPEG_PARAMS` 的默认值补齐。
        kind: 强制指定媒体类别；``None`` 时按 ``src`` 推断。
        safe: 保守模式，只保留各编码器**一定存在**的核心选项，丢掉
            ``-tune`` ``-pred`` ``-huffman`` 这类私有调优项。用户机器上的
            ffmpeg 构建千奇百怪（有的裁过编码器、有的版本老），私有选项不认
            会让 ffmpeg 直接退出而不是忽略——:func:`run` 首次失败后会用这个
            模式再试一次，把「压缩失败」降级成「少省几个百分点」。
    Returns:
        ffmpeg 参数列表。
    """
    opts = dict(opts or {})
    k = kind or media_kind(src) or media_kind(dst) or KIND_IMAGE
    container = _ext(dst) or _ext(src)

    def opt(key, cast=None):
        spec = FFMPEG_PARAMS.get(key, {})
        val = opts.get(key, spec.get("default"))
        if cast is None:
            return val
        try:
            return cast(val)
        except (TypeError, ValueError):
            return cast(spec.get("default"))

    # ``-progress pipe:1`` 让 ffmpeg 往 stdout 吐 key=value 进度，供 _run 解析。
    args: list[str] = ["-hide_banner", "-nostats", "-progress", "pipe:1", "-y", "-i", src]

    if k == KIND_VIDEO:
        # 用户没显式指定 CRF 时，让它跟着**实际会用的**编码器走等效值。
        # 否则「auto + webm」会拿 H.264 的 23 去喂 VP9，画质档位直接错一大截
        # （笔记里点名的头号坑：跨编码器 CRF 不能照搬）。
        if "ff_v_crf" not in opts:
            opts["ff_v_crf"] = recommended_crf(pick_video_encoder(container, opt("ff_v_encoder")))
        args += _video_args(container, opt, safe)
    elif k == KIND_AUDIO:
        args += _audio_args(container, opt, safe)
    else:
        args += _image_args(container, opt, safe)

    args.append(dst)
    return args


def _scale_filter(max_width: int) -> list[str]:
    """限宽缩放滤镜。宽高都强制取偶数，否则 yuv420p 编码会直接失败。"""
    if max_width <= 0:
        return []
    return ["-vf", f"scale='min({max_width},iw)':-2:flags=lanczos"]


def _video_args(container: str, opt, safe: bool = False) -> list[str]:
    enc = pick_video_encoder(container, opt("ff_v_encoder"))
    args: list[str] = []

    if enc == "copy":
        args += ["-c:v", "copy"]
    else:
        crf = max(0, min(63, opt("ff_v_crf", int)))
        preset = opt("ff_v_preset") or "slow"
        tune = opt("ff_v_tune") or "none"

        args += _scale_filter(opt("ff_v_maxwidth", int))
        fps = opt("ff_v_fps", int)
        if fps > 0:
            args += ["-r", str(fps)]

        args += ["-c:v", enc]

        if enc in ("libx264", "libx265"):
            args += ["-crf", str(crf), "-preset", _map_preset(enc, preset)]
            if tune != "none" and not safe:
                args += ["-tune", tune]
            if enc == "libx264":
                if not safe:
                    args += ["-profile:v", "high"]
            else:
                # 笔记 §9：H.265 进 MP4/MOV 不打 hvc1 标签，苹果全家桶播不了。
                if container in ("mp4", "mov", "m4v"):
                    args += ["-tag:v", "hvc1"]
                if not safe:
                    args += ["-x265-params", "log-level=error"]
        elif enc == "libsvtav1":
            args += ["-crf", str(crf), "-preset", _map_preset(enc, preset)]
        elif enc == "libvpx-vp9":
            # 笔记 §9：VP9 的 -crf 必须与 -b:v 0 成对出现，否则会退化成
            # 「码率上限模式」，压出来又大又糊。这两个不能进 safe 白名单。
            args += ["-crf", str(crf), "-b:v", "0"]
            if not safe:
                args += ["-quality", "good", "-speed", _vp9_speed(preset), "-row-mt", "1"]
        elif enc.endswith("_nvenc"):
            # 笔记 §9：NVENC 不认 -crf，必须走 -rc vbr + -cq。
            args += ["-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
            if not safe:
                args += [
                    "-preset",
                    _map_preset(enc, preset),
                    "-tune",
                    "uhq" if enc.startswith("hevc") else "hq",
                ]
            if enc.startswith("hevc") and container in ("mp4", "mov", "m4v"):
                args += ["-tag:v", "hvc1"]
        else:
            args += ["-crf", str(crf)]

        if opt("ff_v_pixfmt") != "keep":
            args += ["-pix_fmt", "yuv420p"]

    # ---- 音轨 ----
    track = pick_track_encoder(container, opt("ff_v_audio"))
    if track == "none":
        args += ["-an"]
    elif track == "copy":
        args += ["-c:a", "copy"]
    else:
        args += ["-c:a", track, "-b:a", opt("ff_v_abr") or "128k"]

    if opt("ff_v_faststart") and container in ("mp4", "mov", "m4v"):
        args += ["-movflags", "+faststart"]

    return args


def _audio_args(container: str, opt, safe: bool = False) -> list[str]:
    enc = pick_audio_encoder(container, opt("ff_a_encoder"))
    args: list[str] = ["-vn"]

    if enc == "copy":
        return args + ["-c:a", "copy"]

    args += ["-c:a", enc]

    if enc == "libmp3lame":
        # 笔记 §4：MP3 用 VBR -q:a，320k CBR 是纯浪费。
        args += ["-q:a", str(max(0, min(9, opt("ff_a_mp3q", int))))]
    elif enc == "flac":
        args += ["-compression_level", str(max(0, min(12, opt("ff_a_flac_level", int))))]
    elif enc.startswith("pcm_"):
        pass  # PCM 无码率概念
    else:
        args += ["-b:a", opt("ff_a_bitrate") or "96k"]
        if enc == "libopus" and not safe:
            args += ["-vbr", "on", "-compression_level", "10"]

    ch = opt("ff_a_channels")
    if ch and ch != "keep":
        args += ["-ac", str(ch)]
    sr = opt("ff_a_samplerate")
    if sr and sr != "keep" and enc != "libopus":
        # Opus 内部固定 48kHz，显式改采样率只会多插一层重采样。
        args += ["-ar", str(sr)]

    return args


def _image_args(container: str, opt, safe: bool = False) -> list[str]:
    req = (opt("ff_i_encoder") or "auto").lower()
    enc = _IMAGE_CONTAINER_ENCODER.get(container, "png") if req in ("", "auto") else req
    quality = max(1, min(100, opt("ff_i_quality", int)))
    lossless = bool(opt("ff_i_lossless"))
    max_width = opt("ff_i_maxwidth", int)

    if container == "gif" or enc == "gif":
        return _gif_args(max_width)

    args: list[str] = _scale_filter(max_width)
    # 单帧输出。不加 -frames:v 1，ffmpeg 遇到多页 TIFF 会尝试按序列写多个文件。
    args += ["-frames:v", "1"]
    # ``-update`` 是 image2 复用器的私有选项：webp / avif 走的是各自的复用器，
    # 传过去会直接报 "Option update not found" 而不是被忽略。
    if container in _IMAGE2_CONTAINERS:
        args += ["-update", "1"]
    args += ["-c:v", enc]

    if enc == "libwebp":
        args += [
            "-lossless",
            "1" if lossless else "0",
            "-compression_level",
            str(max(0, min(6, opt("ff_i_effort", int)))),
        ]
        if not lossless:
            args += ["-quality", str(quality)]
        if not safe:
            args += ["-preset", "picture"]
    elif enc == "libaom-av1":
        # 笔记 §5：AVIF 静态图必须带 -still-picture，否则会被当单帧视频编。
        args += ["-crf", str(max(0, min(63, opt("ff_i_crf", int)))), "-b:v", "0"]
        if not safe:
            args += ["-cpu-used", "6", "-still-picture", "1"]
        args += ["-pix_fmt", "yuv420p"]
    elif enc == "mjpeg":
        args += ["-q:v", _jpeg_qscale(quality)]
        if not safe:
            args += ["-huffman", "optimal"]
    elif enc == "png":
        # png 编码器把 compression_level 直接当 zlib 级别用，范围 0~9。
        args += ["-compression_level", "9"]
        if not safe:
            args += ["-pred", "mixed"]
    elif enc == "tiff" and not safe:
        # TIFF 默认写的是未压缩数据，不指定算法等于白跑一趟。
        args += ["-compression_algo", "deflate"]

    return args


def _gif_args(max_width: int) -> list[str]:
    """GIF 走调色板滤镜链，而不是裸 ``-c:v gif``。

    直接重编码 GIF 会用默认 216 色安全调色板，出来又大又花。这里用
    ``palettegen``/``paletteuse`` 在一条 filtergraph 里做单趟自适应调色板——
    体积和观感都比裸编码强得多。
    （常规 GIF 压缩仍优先走 gifsicle 后端，这条路径是用户显式指定 ffmpeg 时的兜底。）
    """
    scale = f"scale='min({max_width},iw)':-2:flags=lanczos," if max_width > 0 else ""
    chain = (
        f"{scale}split[a][b];"
        "[a]palettegen=stats_mode=diff[p];"
        "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
    )
    return ["-filter_complex", chain, "-loop", "0"]


# =============================================================================
# 执行
# =============================================================================
_TERMINATE_GRACE_SEC = 2.0


def _stop(proc: subprocess.Popen, grace: float = _TERMINATE_GRACE_SEC) -> None:
    """尽快结束 ffmpeg，且保证不抛异常（与 converter._stop 同策略）。"""
    try:
        if grace > 0:
            proc.terminate()
            try:
                proc.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg 未在 %.1fs 内退出，强制 kill", grace)
        proc.kill()
        proc.wait(timeout=grace or 1.0)
    except (OSError, subprocess.SubprocessError):
        log.debug("终止 ffmpeg 时进程已不存在")


def run(
    src: str,
    dst: str,
    opts: dict | None = None,
    kind: str | None = None,
    on_progress: ProgressCallback | None = None,
    cancel_event: object | None = None,
    ffmpeg_path: str | None = None,
    on_stats=None,
    on_proc=None,
) -> tuple[bool, str]:
    """执行一次 FFmpeg 压缩。

    Args:
        src: 输入文件。
        dst: 输出文件（调用方负责保证目录存在）。
        opts: ``ff_*`` 参数字典。
        kind: 媒体类别；``None`` 时自动判断。
        on_progress: 0..100 的进度回调。图片没有时长，只会收到 0 与 100。
        cancel_event: 置位后中止（需实现 ``is_set()``）。
        ffmpeg_path: 显式指定 ffmpeg；``None`` 时自动查找。
        on_stats: 可选，收 :class:`ProgressSnapshot`（速度 / 剩余时间 / 帧率）。
        on_proc: 可选，子进程启动/退出时各回调一次，供队列真暂停。两级重试
            会各起一个新进程，因此可能被回调多轮。
    Returns:
        ``(ok, detail)``。``ok=False`` 时 ``detail`` 是可直接展示的错误摘要。
    Notes:
        两级重试，都只在命中特定错误特征时才走，避免真错误被白跑两遍：

        1. **硬件编码器打不开** → 剥掉 GPU 编码器换 CPU 重试（V0.8.21 新增）。
        2. **选项不被识别** → 换保守参数重试。
    """
    exe = ffmpeg_path or find_ffmpeg()
    if not exe:
        return False, "未找到 ffmpeg，请先在「关于」页下载"

    def _build(safe: bool, o: dict | None = None) -> list[str] | None:
        try:
            return build_args(src, dst, o if o is not None else opts, kind, safe=safe)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("构建 ffmpeg 压缩参数失败：%s", Path(src).name)
            nonlocal build_err
            build_err = f"参数构建失败: {exc}"
            return None

    build_err = ""
    args = _build(False)
    if args is None:
        return False, build_err

    # 分母只探一次，重试时复用，别让 ffprobe 白跑第二遍。
    duration_ms = probe_duration_ms(src, exe)

    ok, detail = _execute(
        exe, args, src, dst, on_progress, cancel_event, on_stats, duration_ms, on_proc
    )
    if ok or detail == "canceled":
        return ok, detail

    # ---- 重试一：硬件编码器打不开 → 降级 CPU ----
    # 典型场景：用户机器是 AMD/Intel 显卡却选了 h264_nvenc，或 NVIDIA 驱动
    # 版本过旧。ffmpeg 此时报 "Could not open encoder"，不是选项语法问题，
    # 保守重试救不了，必须换编码器。
    if _looks_like_encoder_error(detail):
        cpu_opts = _strip_hw_encoder(opts)
        if cpu_opts is not None:
            log.warning(
                "硬件编码器无法打开，降级 CPU 重试：%s（%s → %s）",
                Path(src).name,
                (opts or {}).get("ff_v_encoder"),
                cpu_opts.get("ff_v_encoder"),
            )
            args = _build(False, cpu_opts)
            if args is not None:
                ok, detail = _execute(
                    exe, args, src, dst, on_progress, cancel_event, on_stats, duration_ms, on_proc
                )
                if ok or detail == "canceled":
                    return ok, detail

    # ---- 重试二：保守参数 ----
    if _looks_like_option_error(detail):
        log.warning("ffmpeg 拒绝了某个调优选项，改用保守参数重试：%s", Path(src).name)
        args = _build(True)
        if args is not None:
            return _execute(
                exe, args, src, dst, on_progress, cancel_event, on_stats, duration_ms, on_proc
            )
    return False, detail


# ffmpeg 判定「这个选项我不认识」时会吐的几种串。命中才做保守重试，
# 真正的解码/写盘错误重试也是白搭，只会让用户多等一倍时间。
_OPTION_ERROR_MARKERS: tuple[str, ...] = (
    "unrecognized option",
    "option not found",
    "invalid argument",
    "error setting option",
    "unknown encoder",
    "no such option",
)

# ffmpeg 打不开硬件编码器时的典型措辞。命中后剥掉 GPU 编码器换 CPU 重试。
# 注意 "error while opening encoder" 后面常跟一句误导性的
# "maybe incorrect parameters such as bit_rate, rate, width or height"，
# 实际原因往往只是这台机器根本没有对应的 GPU。
_ENCODER_ERROR_MARKERS: tuple[str, ...] = (
    "could not open encoder",
    "error while opening encoder",
    "cannot load nvcuda",
    "cannot load libcuda",
    "no capable devices found",
    "openencodesessionex failed",
    "driver does not support",
    "function not implemented",
    "generic error in an external library",
)

# 硬件编码器 → 等价 CPU 编码器。降级重试时按这张表替换。
HW_TO_CPU_ENCODER: dict[str, str] = {
    "h264_nvenc": "libx264",
    "hevc_nvenc": "libx265",
    "h264_qsv": "libx264",
    "hevc_qsv": "libx265",
    "h264_amf": "libx264",
    "hevc_amf": "libx265",
    "h264_videotoolbox": "libx264",
    "hevc_videotoolbox": "libx265",
}


def _looks_like_encoder_error(detail: str) -> bool:
    """判断 ffmpeg 的失败摘要是否属于「硬件编码器打不开」。

    Args:
        detail: ffmpeg 的 stderr 尾巴。
    Returns:
        命中任一特征串即为 ``True``。
    """
    lowered = (detail or "").lower()
    return any(m in lowered for m in _ENCODER_ERROR_MARKERS)


def _strip_hw_encoder(opts: dict | None) -> dict | None:
    """把参数里的硬件编码器换成等价 CPU 编码器。

    Args:
        opts: 原始 ``ff_*`` 参数字典。
    Returns:
        替换后的**新字典**；原本就没用硬件编码器（无需降级）时返回 ``None``。
    Notes:
        同时要清掉 NVENC 专属的码率控制参数——``-rc vbr -cq N`` 这套
        libx264 不认识，留着会让降级重试也一起失败。
    """
    if not opts:
        return None
    enc = str(opts.get("ff_v_encoder") or "")
    if enc in HW_TO_CPU_ENCODER:
        new = dict(opts)
        new["ff_v_encoder"] = HW_TO_CPU_ENCODER[enc]
        return new
    if enc in ("auto", ""):
        # auto 由 pick_video_encoder 决定，它已带硬件门禁；真走到这里说明
        # 挑出来的仍打不开，强制钉死到最稳的 libx264。
        new = dict(opts)
        new["ff_v_encoder"] = "libx264"
        return new
    return None


def _looks_like_option_error(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(m in lowered for m in _OPTION_ERROR_MARKERS)


def _execute(
    exe: str,
    args: list[str],
    src: str,
    dst: str,
    on_progress: ProgressCallback | None,
    cancel_event: object | None,
    on_stats=None,
    duration_ms: int | None = None,
    on_proc=None,
) -> tuple[bool, str]:
    """跑一次 ffmpeg，解析 ``-progress`` 输出上报真实百分比。

    Args:
        exe: ffmpeg 路径。
        args: 已构建好的参数（含 ``-progress pipe:1``）。
        src: 输入文件，仅用于日志与时长预取。
        dst: 输出文件，用于收尾校验。
        on_progress: 0..100 的进度回调。
        cancel_event: 置位后中止。
        on_stats: 可选，收 :class:`ProgressSnapshot`，用于展示速度/剩余时间。
        duration_ms: 调用方已知的总时长；``None`` 时本函数自行用 ffprobe 预取。
        on_proc: 可选，子进程启动/退出时各回调一次（退出传 ``None``），
            供队列做 psutil 真暂停（V0.8.21 E4）。
    Returns:
        ``(ok, detail)``。

    Notes:
        V0.8.21 修复：原实现在等一个 ffmpeg **从不输出**的键 ``duration_ms``，
        导致分母恒为 None、中间进度一次都不上报（即所谓「假进度条」）。
        现在统一交给 :class:`FFmpegProgressParser`，分母走 ffprobe 预取 +
        ``Duration:`` 横幅兜底。
    """
    cmd = [exe, *args]
    log.info("ffmpeg 压缩命令：%s", " ".join(cmd))

    # 分母：ffmpeg 的 -progress 不输出总时长，必须外部取。图片没有时长，
    # 探测返回 None 时解析器只会在结束时给 100，与旧行为一致。
    if duration_ms is None:
        duration_ms = probe_duration_ms(src, exe)

    try:
        proc = popen_silent(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # 合并单流，避免双管道读取死锁
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        log.error("启动 ffmpeg 失败：%s", exc)
        return False, f"启动 ffmpeg 失败: {exc}"

    if on_proc:
        on_proc(proc)  # V0.8.21 E4：交出句柄，队列暂停时挂起它

    parser = FFmpegProgressParser(duration_ms=duration_ms)
    tail: list[str] = []

    with proc:
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    # 挂起态的进程收不到 terminate，先解挂再停（见 proc_control）
                    proc_control.resume_then(proc)
                    _stop(proc)
                    if on_proc:
                        on_proc(None)
                    return (False, "canceled")

                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                is_kv = "=" in line and not line.startswith(" ")
                snap = parser.feed(line)
                if snap is not None:
                    if on_progress and snap.pct is not None:
                        on_progress(snap.pct)
                    if on_stats:
                        on_stats(snap)
                if is_kv:
                    continue  # 进度行不进 tail，避免错误摘要被进度刷没

                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("读取 ffmpeg 输出失败：%s", exc)
            proc_control.resume_then(proc)
            _stop(proc, grace=0)
            if on_proc:
                on_proc(None)
            return False, f"读取 ffmpeg 输出失败: {exc}"

        rc = proc.wait()

    if on_proc:
        on_proc(None)  # 进程已退出，撤销队列侧的句柄登记

    if rc != 0:
        detail = "\n".join(tail[-12:]) or f"ffmpeg 退出码 {rc}"
        log.error("ffmpeg 压缩失败 (rc=%s)：%s\n%s", rc, Path(src).name, detail)
        return False, detail

    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        return False, "ffmpeg 未产出有效文件"

    if on_progress:
        on_progress(100)
    return True, ""
