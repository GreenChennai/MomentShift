"""Format presets with *hardcoded optimal-quality* ffmpeg parameters.

Design goals (per the product spec):
- The user never tweaks ffmpeg flags — every target format ships with a tuned,
  high-quality default.
- CPU is the safe default; GPU (NVENC / QSV / AMF / VideoToolbox) is selected
  automatically when available and the user opts into hardware mode.

Source categories are detected from the file extension; targets are grouped by
category so invalid cross-category conversions (e.g. image -> mp3) are offered
only where they make sense.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .advanced import build_advanced_args, build_merge_args, get as get_adv

# --------------------------------------------------------------------------
# Extension maps (used to guess the source category)
# --------------------------------------------------------------------------
IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".tif", ".gif", ".ico",
    ".tga", ".ppm", ".pgm", ".dds", ".heic", ".heif",
}
AUDIO_EXTS = {
    ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".oga", ".wma", ".opus",
    ".ac3", ".aiff", ".ape",
}
VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".wmv", ".mpeg", ".mpg",
    ".ts", ".m4v", ".3gp", ".vob",
}

# Target formats offered, grouped by source category.
TARGET_GROUPS: dict[str, list[str]] = {
    "image": ["jpg", "png", "webp", "bmp", "tiff", "gif"],
    "audio": ["mp3", "wav", "flac", "aac", "m4a", "ogg"],
    "video": ["mp4", "mkv", "mov", "webm", "avi", "gif"],
}

# Each profile: output extension, logical category, and CPU encoding params.
# Video profiles may be upgraded to GPU params at runtime (see build_args).
PROFILES: dict[str, dict] = {
    # ----- images -----
    "jpg":  {"ext": ".jpg",  "category": "image", "params": ["-q:v", "2"]},       # ~visually lossless mjpeg
    "png":  {"ext": ".png",  "category": "image", "params": ["-compression_level", "9"]},  # lossless
    "webp": {"ext": ".webp", "category": "image", "params": ["-quality", "90"]},
    "bmp":  {"ext": ".bmp",  "category": "image", "params": []},
    "tiff": {"ext": ".tiff", "category": "image", "params": ["-compression_algo", "deflate"]},
    "gif":  {"ext": ".gif",  "category": "video", "params": [], "is_gif": True},

    # ----- audio -----
    "mp3":  {"ext": ".mp3",  "category": "audio", "params": ["-b:a", "320k"]},
    "wav":  {"ext": ".wav",  "category": "audio", "params": ["-c:a", "pcm_s16le"]},
    "flac": {"ext": ".flac", "category": "audio", "params": ["-compression_level", "8"]},
    "aac":  {"ext": ".aac",  "category": "audio", "params": ["-b:a", "320k"]},
    "m4a":  {"ext": ".m4a",  "category": "audio", "params": ["-c:a", "aac", "-b:a", "256k"]},
    "ogg":  {"ext": ".ogg",  "category": "audio", "params": ["-c:a", "libvorbis", "-q:a", "6"]},

    # ----- video -----
    "mp4":  {"ext": ".mp4",  "category": "video",
             "params": ["-c:v", "libx264", "-crf", "18", "-preset", "slow",
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]},
    "mkv":  {"ext": ".mkv",  "category": "video",
             "params": ["-c:v", "libx264", "-crf", "18", "-preset", "slow",
                        "-c:a", "aac", "-b:a", "192k"]},
    "mov":  {"ext": ".mov",  "category": "video",
             "params": ["-c:v", "libx264", "-crf", "18", "-preset", "slow",
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]},
    "webm": {"ext": ".webm", "category": "video",
             "params": ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0",
                        "-c:a", "libopus", "-b:a", "128k"]},
    "avi":  {"ext": ".avi",  "category": "video",
             "params": ["-c:v", "mpeg4", "-q:v", "3",
                        "-c:a", "libmp3lame", "-q:a", "2"]},
}


# --------------------------------------------------------------------------
# Public helpers
# --------------------------------------------------------------------------
def guess_category(path: str) -> Optional[str]:
    """Return ``image`` / ``audio`` / ``video`` for a file, or ``None``."""
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def target_extension(target_format: str) -> str:
    return PROFILES[target_format]["ext"]


def is_valid_target(source_category: str, target_format: str) -> bool:
    return target_format in TARGET_GROUPS.get(source_category, [])


def _gpu_video_args(encoder: str, target: str) -> list[str]:
    """Build GPU encoder args for the detected hardware encoder."""
    args = ["-c:v", encoder]
    if "nvenc" in encoder:
        args += ["-rc", "vbr", "-cq", "19", "-preset", "p4"]
    elif "qsv" in encoder:
        args += ["-global_quality", "25", "-preset", "medium"]
    elif "amf" in encoder:
        args += ["-rc", "vbr_quality", "-qv", "23"]
    elif "videotoolbox" in encoder:
        args += ["-q:v", "65"]
    else:
        args += ["-b:v", "0", "-cq", "23"]
    args += ["-c:a", "aac", "-b:a", "192k"]
    if target in ("mp4", "mov"):
        args += ["-movflags", "+faststart"]
    return args


def build_args(task, hw: Optional[dict] = None) -> list[str]:
    """Return the ffmpeg argument list (excluding the binary) for a task.

    ``task`` must expose: ``input_path``, ``output_path``, ``target_format``,
    ``category``, ``use_gpu``. It may also carry ``adv`` (advanced options dict),
    ``merge`` and ``input_paths`` (for multi-file merge).
    """
    hw = hw or {}
    profile = PROFILES[task.target_format]
    target = task.target_format

    # --- multi-file merge into one output -------------------------------
    if getattr(task, "merge", False) and getattr(task, "input_paths", None):
        opt = task.adv if task.adv else get_adv(task.category)
        return build_merge_args(task.category, task.input_paths, task.output_path, opt)

    args = ["-hide_banner", "-nostats", "-progress", "pipe:1", "-y", "-i", task.input_path]

    if target == "gif":
        # Animated GIF from video via a 2-pass palette; static GIF from image.
        if task.category == "video":
            args += [
                "-vf",
                "fps=15,scale=640:-1:flags=lanczos,"
                "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                "-loop", "0",
            ]
        # image source: no filter needed.
    elif target in ("mp4", "mkv", "mov"):
        if task.use_gpu and hw.get("h264"):
            args += _gpu_video_args(hw["h264"], target)
        else:
            args += profile["params"]
    else:
        # webm / avi / image / audio -> CPU presets (always safe).
        args += profile["params"]

    # --- advanced, per-category tuning ----------------------------------
    opt = task.adv if task.adv else get_adv(task.category)
    if opt:
        args += build_advanced_args(task.category, target, opt)

    args.append(task.output_path)
    return args
