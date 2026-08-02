"""Hardware-acceleration detection for ffmpeg.

We probe the available ffmpeg *encoders* (not just ``-hwaccels``) to know which
GPU codecs we can actually use, then expose a small map the converter consults
when GPU mode is enabled. CPU is always the fallback.

v0.7.27: NVIDIA NVENC additionally requires the runtime driver DLL
(``nvcuda.dll``); the encoder may be compiled into ffmpeg but unusable on
machines without an NVIDIA GPU/driver — probing it would make H.264/H.265
conversions fail with "Cannot load nvcuda.dll".
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from .ffmpeg import get_encoders


def _nvidia_runtime_available() -> bool:
    """True 表示 NVENC 运行时可用（Windows: nvcuda.dll 存在）。"""
    if sys.platform == "win32":
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        return os.path.exists(os.path.join(root, "System32", "nvcuda.dll"))
    return False


def _probe_encoder(ffmpeg_path: str, encoder: str) -> bool:
    """v0.7.28：实际跑一个最小编码，验证硬件编码器运行时可用。

    仅查 -encoders 不够：nvenc 需 NVIDIA 驱动、qsv 需 Intel GPU/驱动、amf 需
    AMD GPU —— 编译进 ffmpeg 不等于机器可用。失败返回 False。
    """
    import subprocess
    try:
        cmd = [ffmpeg_path, "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", "color=black:s=128x128:d=0.1",
               "-frames:v", "1", "-c:v", encoder, "-f", "null", "-"]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0
    except Exception:
        return False


def detect_hw_accel(ffmpeg_path: Optional[str]) -> dict[str, Optional[str]]:
    """Return ``{"h264": encoder|None, "hevc": encoder|None}``.

    ``None`` means "no suitable GPU encoder found -> use CPU".
    """
    result: dict[str, Optional[str]] = {"h264": None, "hevc": None}
    if not ffmpeg_path:
        return result

    encoders = get_encoders(ffmpeg_path)
    nvenc_ok = _nvidia_runtime_available()

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

    def _usable(candidate: str) -> bool:
        # v0.7.27：NVENC 需 NVIDIA 驱动运行时（nvcuda.dll）
        if "nvenc" in candidate and not nvenc_ok:
            return False
        # v0.7.28：实际编码探测，排除 qsv/amf 等"编译了但机器不可用"的情况
        return _probe_encoder(ffmpeg_path, candidate)

    for candidate in h264_candidates:
        if candidate in encoders and _usable(candidate):
            result["h264"] = candidate
            break
    for candidate in hevc_candidates:
        if candidate in encoders and _usable(candidate):
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
