"""Hardware-acceleration detection for ffmpeg.

We probe the available ffmpeg *encoders* (not just ``-hwaccels``) to know which
GPU codecs we can actually use, then expose a small map the converter consults
when GPU mode is enabled. CPU is always the fallback.
"""

from __future__ import annotations

from typing import Optional

from .ffmpeg import get_encoders


def detect_hw_accel(ffmpeg_path: Optional[str]) -> dict[str, Optional[str]]:
    """Return ``{"h264": encoder|None, "hevc": encoder|None}``.

    ``None`` means "no suitable GPU encoder found -> use CPU".
    """
    result: dict[str, Optional[str]] = {"h264": None, "hevc": None}
    if not ffmpeg_path:
        return result

    encoders = get_encoders(ffmpeg_path)

    # Ordered by broad compatibility / quality.
    h264_candidates = [
        "h264_nvenc",      # NVIDIA
        "h264_qsv",        # Intel
        "h264_amf",        # AMD
        "h264_videotoolbox",  # Apple
        "h264_v4l2m2m",    # Linux V4L2
    ]
    hevc_candidates = [
        "hevc_nvenc",
        "hevc_qsv",
        "hevc_amf",
        "hevc_videotoolbox",
    ]

    for candidate in h264_candidates:
        if candidate in encoders:
            result["h264"] = candidate
            break
    for candidate in hevc_candidates:
        if candidate in encoders:
            result["hevc"] = candidate
            break
    return result


def best_available(ffmpeg_path: Optional[str]) -> str:
    """Human-readable summary of detected acceleration (for the UI/logs)."""
    hw = detect_hw_accel(ffmpeg_path)
    found = [v for v in hw.values() if v]
    if not found:
        return "CPU"
    return "GPU (" + ", ".join(sorted(found)) + ")"
