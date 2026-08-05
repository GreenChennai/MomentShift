"""格式预设：内置「高保真」ffmpeg 参数。

职责边界：
- 做：为每个目标格式提供调优过的高质量默认参数；按扩展名识别源分类、按分类
  归组目标，避免无意义的跨类转换（如 图片 → mp3）。
- 不做：不执行命令；不读取运行时配置。

依赖：标准库；被依赖：core/converter、core/queue。

设计目标：
- 用户从不用手调 ffmpeg 参数，每种目标格式都自带高质量默认值。
- CPU 是安全默认；GPU（NVENC / QSV / AMF / VideoToolbox）在可用且用户开启硬件
  模式时自动选用。
"""

from __future__ import annotations

from pathlib import Path

from .advanced import build_advanced_args, build_merge_args
from .advanced import snapshot as adv_snapshot

# --- 扩展名映射（用于推断源文件分类）---
IMAGE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".webp",
    ".tiff",
    ".tif",
    ".gif",
    ".ico",
    ".tga",
    ".ppm",
    ".pgm",
    ".dds",
    ".heic",
    ".heif",
}
AUDIO_EXTS = {
    ".mp3",
    ".wav",
    ".flac",
    ".aac",
    ".m4a",
    ".ogg",
    ".oga",
    ".wma",
    ".opus",
    ".ac3",
    ".aiff",
    ".ape",
}
VIDEO_EXTS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".avi",
    ".flv",
    ".wmv",
    ".mpeg",
    ".mpg",
    ".ts",
    ".m4v",
    ".3gp",
    ".vob",
}

# 可选的目标格式，按源分类归组，避免出现「图片 → mp3」这类无意义转换
TARGET_GROUPS: dict[str, list[str]] = {
    "image": ["jpg", "png", "webp", "bmp", "tiff", "gif"],
    "audio": ["mp3", "wav", "flac", "aac", "m4a", "ogg"],
    "video": ["mp4", "mkv", "mov", "webm", "avi", "gif"],
}

# 每个预设含：输出扩展名、逻辑分类、CPU 编码参数。
# 视频类预设在运行时可能被升级为 GPU 参数（见 build_args）。
PROFILES: dict[str, dict] = {    # ----- 图片 -----
    # -q:v 是反向刻度，1 最好 31 最差，取 2 已接近「视觉无损」
    "jpg": {"ext": ".jpg", "category": "image", "params": ["-q:v", "2"]},
    "png": {"ext": ".png", "category": "image", "params": ["-compression_level", "9"]},  # 无损
    "webp": {"ext": ".webp", "category": "image", "params": ["-quality", "90"]},
    "bmp": {"ext": ".bmp", "category": "image", "params": []},
    "tiff": {"ext": ".tiff", "category": "image", "params": ["-compression_algo", "deflate"]},
    "gif": {"ext": ".gif", "category": "video", "params": [], "is_gif": True},
    # ----- 音频 -----
    # mp3：libmp3lame 的 VBR 刻度 -q:a（0 最好 9 最差），2 接近「透明」且体积远小于 320k CBR
    "mp3": {"ext": ".mp3", "category": "audio", "params": ["-c:a", "libmp3lame", "-q:a", "2"]},
    "wav": {"ext": ".wav", "category": "audio", "params": ["-c:a", "pcm_s16le"]},
    "flac": {"ext": ".flac", "category": "audio", "params": ["-compression_level", "8"]},
    # aac：内置 aac 编码器在 >256k 后收益递减且易引入瑕疵，256k 已属高码率安全值
    "aac": {"ext": ".aac", "category": "audio", "params": ["-c:a", "aac", "-b:a", "256k"]},
    "m4a": {"ext": ".m4a", "category": "audio", "params": ["-c:a", "aac", "-b:a", "256k"]},
    "ogg": {"ext": ".ogg", "category": "audio", "params": ["-c:a", "libvorbis", "-q:a", "6"]},
    # ----- 视频 -----
    "mp4": {
        "ext": ".mp4",
        "category": "video",
        # yuv420p + high：浏览器 / QuickTime / 旧设备全兼容的「黄金组合」
        "pix_fmt": "yuv420p",
        "profile": "high",
        "params": [
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "slow",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
        ],
    },
    "mkv": {
        "ext": ".mkv",
        "category": "video",
        "pix_fmt": "yuv420p",
        "profile": "high",
        "params": [
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "slow",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ],
    },
    "mov": {
        "ext": ".mov",
        "category": "video",
        "pix_fmt": "yuv420p",
        "profile": "high",
        "params": [
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "slow",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
        ],
    },
    "webm": {
        "ext": ".webm",
        "category": "video",
        "pix_fmt": "yuv420p",
        "params": [
            "-c:v",
            "libvpx-vp9",
            "-crf",
            "30",
            "-b:v",
            "0",
            # VP9 提速：row-mt 开多线程、cpu-used 抬高到 2（good 档）大幅缩短编码耗时
            "-row-mt",
            "1",
            "-deadline",
            "good",
            "-cpu-used",
            "2",
            "-c:a",
            "libopus",
            "-b:a",
            "128k",
        ],
    },
    "avi": {
        "ext": ".avi",
        "category": "video",
        "pix_fmt": "yuv420p",
        "params": ["-c:v", "mpeg4", "-q:v", "3", "-c:a", "libmp3lame", "-q:a", "2"],
    },
}

# v0.8.3「仅提取音频」的音频编码器映射（键为 advanced.AUDIO_FORMATS 里的格式）。
# 与 PROFILES 里的音频预设保持一致：m4a 容器同样用 aac 编码。
EXTRACT_AUDIO_ENCODERS: dict[str, list[str]] = {
    "mp3": ["-c:a", "libmp3lame"],
    "wav": ["-c:a", "pcm_s16le"],
    "flac": ["-c:a", "flac"],
    "aac": ["-c:a", "aac"],
    "m4a": ["-c:a", "aac"],
    "ogg": ["-c:a", "libvorbis"],
}


def extract_audio_encoder(audio_format: str) -> list[str]:
    """返回「仅提取音频」的音频编码参数；未知格式回退 mp3。"""
    fmt = (audio_format or "mp3").lower().lstrip(".")
    return list(EXTRACT_AUDIO_ENCODERS.get(fmt, EXTRACT_AUDIO_ENCODERS["mp3"]))


# --- 公开辅助函数 ---
def guess_category(path: str) -> str | None:
    """按扩展名推断文件分类。

    Args:
        path: 文件路径，只看扩展名，不要求文件真实存在。
    Returns:
        ``image`` / ``audio`` / ``video`` 之一；无法识别返回 ``None``。
    Notes:
        判定顺序是 视频 → 音频 → 图片。``.gif`` 同时出现在图片与视频语义中，
        这里归为图片，是否走动图管线由目标格式与实际帧数另行决定。
    """
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def target_extension(target_format: str) -> str:
    """返回目标格式对应的输出扩展名（含点号），如 ``mp4`` → ``.mp4``。"""
    return PROFILES[target_format]["ext"]


def is_valid_target(source_category: str, target_format: str) -> bool:
    """判断某目标格式对该源分类是否是允许的转换目标。"""
    return target_format in TARGET_GROUPS.get(source_category, [])


def _gpu_video_encode_args(encoder: str) -> list[str]:
    """拼出硬件编码器的**视频编码**参数（``-c:v`` + 各家质量旋钮）。

    Args:
        encoder: 硬件编码器名，如 ``h264_nvenc`` / ``h264_qsv`` / ``h264_amf``。
    Returns:
        ffmpeg 参数列表，**不含**音频与容器参数。

    Notes:
        各家硬件编码器的「质量」旋钮互不相通：NVENC 用 ``-cq``、QSV 用
        ``-global_quality``、AMF 用 ``-qv``、VideoToolbox 用 ``-q:v``，
        所以只能逐家分支，不能抽成统一参数。

        v0.8.1 Bug2b：硬件探测（``hardware._probe_encoder``）必须与真实转码
        用**同一套视频编码参数**，否则会出现「探测用裸 ``-c:v`` 通过、实际用
        ``-rc vbr_quality -qv 23`` 时 ffmpeg 构建不认 ``-qv`` 而直接失败」的
        错位。这个函数把「视频编码参数」单独抽出来供探测复用，保证两边一致。
    """
    args = ["-c:v", encoder]
    if "nvenc" in encoder:
        # -tune hq 让 NVENC 在同等码率下提升主观质量（牺牲少量速度）
        args += ["-rc", "vbr", "-cq", "19", "-preset", "p4", "-tune", "hq"]
    elif "qsv" in encoder:
        args += ["-global_quality", "25", "-preset", "medium"]
    elif "amf" in encoder:
        args += ["-rc", "vbr_quality", "-qv", "23"]
    elif "videotoolbox" in encoder:
        args += ["-q:v", "65"]
    else:
        args += ["-b:v", "0", "-cq", "23"]
    return args


def _gpu_video_args(encoder: str, target: str) -> list[str]:
    """为探测到的硬件编码器拼出对应的 GPU 编码参数。

    Args:
        encoder: 硬件编码器名，如 ``h264_nvenc`` / ``h264_qsv`` / ``h264_amf``。
        target: 目标格式，用于决定是否追加 ``+faststart``。
    Returns:
        ffmpeg 参数列表。
    Notes:
        视频编码参数由 :func:`_gpu_video_encode_args` 单点产出，这里再叠加
        音频（``-c:a aac -b:a 192k``）与容器（``+faststart``）参数。
    """
    args = _gpu_video_encode_args(encoder)
    args += ["-c:a", "aac", "-b:a", "192k"]
    # 硬件编码同样强制 yuv420p，避免 10-bit 源产出浏览器/QuickTime 无法播放的文件
    if target in ("mp4", "mkv", "mov", "webm"):
        args += ["-pix_fmt", "yuv420p"]
    if target in ("mp4", "mov"):
        args += ["-movflags", "+faststart"]
    return args


def build_args(task, hw: dict | None = None) -> list[str]:
    """为一个任务拼出完整的 ffmpeg 参数列表（不含二进制本身）。

    Args:
        task: 至少要有 ``input_path`` / ``output_path`` / ``target_format`` /
            ``category`` / ``use_gpu``；可选带 ``adv``（高级参数快照）、
            ``merge`` 与 ``input_paths``（多文件合并时用）。
        hw: 硬件编码器探测结果，形如 ``{"h264": 编码器名或 None}``；
            传 ``None`` 等价于「无硬件加速」。
    Returns:
        ffmpeg 参数列表，可直接接在二进制路径之后执行。
    """
    hw = hw or {}
    profile = PROFILES[task.target_format]
    target = task.target_format

    # Q3：正常链路上 Task.adv 一定是入队时的快照；这条兜底只在手工构造
    # Task（测试 / 老调用方）时走到，此时才回退到实时快照。
    opt = task.adv if task.adv else adv_snapshot(task.category)

    # --- v0.8.3 仅提取音频：优先级最高，完全走音频提取分支 ---
    # 启用后不再转换视频格式：丢弃全部视频参数（-c:v / -vf / 分辨率 / 帧率 /
    # 码率 / GPU 编码器），只输出所选格式的音频流（-vn + 音频编码器）。
    if task.category == "video" and opt and opt.get("extract_audio"):
        return [
            "-hide_banner",
            "-nostats",
            "-progress",
            "pipe:1",
            "-y",
            "-i",
            task.input_path,
            "-vn",
            *extract_audio_encoder(opt.get("audio_format", "mp3")),
            task.output_path,
        ]

    # --- 多文件合并为单一输出 ---
    if getattr(task, "merge", False) and getattr(task, "input_paths", None):
        return build_merge_args(task.category, task.input_paths, task.output_path, opt)

    args = ["-hide_banner", "-nostats", "-progress", "pipe:1", "-y", "-i", task.input_path]

    if target == "gif":
        # 视频转 GIF 走两遍调色板（palettegen + paletteuse）：GIF 只有 256 色，
        # 不先统计全局调色板会出现严重色带
        if task.category == "video":
            # v0.8.2 Bug2：scale=iw:-1 保持原始分辨率。旧实现硬编码 640 宽，
            # 720×1280 的视频转 GIF 后变成 640×1138，分辨率静默丢失。
            # fps=15 是动图帧率控制，保持不变。
            args += [
                "-vf",
                "fps=15,scale=iw:-1:flags=lanczos,"
                "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                "-loop",
                "0",
            ]
        # 图片源转 GIF 不需要滤镜
    elif target in ("mp4", "mkv", "mov"):
        if task.use_gpu and hw.get("h264"):
            args += _gpu_video_args(hw["h264"], target)
        else:
            args += profile["params"]
    else:
        # webm / avi / 图片 / 音频一律走 CPU 预设：这些格式的硬件编码器覆盖面差，
        # 强行上 GPU 反而容易直接失败
        args += profile["params"]

    # --- 统一像素格式 / 编码 profile（仅视频预设带这两个字段，GIF 不含）---
    # -pix_fmt yuv420p：保证浏览器 / QuickTime / 旧设备全兼容（10-bit 源也会落回 8-bit）
    # -profile:v high：H.264 高规格，配合 yuv420p 即「黄金兼容组合」
    if profile.get("pix_fmt"):
        args += ["-pix_fmt", profile["pix_fmt"]]
    if profile.get("profile"):
        args += ["-profile:v", profile["profile"]]

    # --- 分类级高级参数微调 ---
    if opt:
        args += build_advanced_args(task.category, target, opt)

    # --- 静态图片目标：动图转静态图时强制只输出一帧 ---
    # v0.8.1 Bug2a：GIF(116帧) → PNG 时，image2 muxer 因没有单帧约束而试图把
    # 每一帧都写进同一个文件名，报 "Cannot write more than one file with the
    # same name" / "does not contain an image sequence pattern"，rc=4294967274。
    # ``-frames:v 1`` 取**首帧**（用户从 GIF 取静态图的直觉语义）；静态图互转
    # （单帧输入）加它也无害。只对静态图片分类生效：GIF（category=video）要
    # 保留多帧，视频/音频目标不受影响。
    if PROFILES[target].get("category") == "image":
        args += ["-frames:v", "1"]

    args.append(task.output_path)
    return args
