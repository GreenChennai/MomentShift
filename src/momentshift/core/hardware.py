"""ffmpeg 硬件加速（GPU 编码）能力探测。

职责边界：
- 做：探测 ffmpeg 实际可用的硬件编码器（不仅是 -hwaccels），给出 converter 在
  GPU 模式下可用的编码器映射；CPU 始终作为回退。
- 不做：不执行转码；不决定用哪个编码器（由 converter 按用户开关选择）。

依赖：core/platform、core/logger；被依赖：core/converter。

注意：NVIDIA NVENC 还需运行时驱动 DLL（nvcuda.dll），编码器即便编进 ffmpeg
也可能在无 N 卡/驱动的机器上不可用——探测到会直接让 H.264/H.265 转码报
"Cannot load nvcuda.dll"，因此这里做实际编码探测而非只看列表。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

from .ffmpeg import get_encoders
from .logger import get_logger
from .platform import run_silent

log = get_logger("hardware")

# 编码器可用性缓存，键为 ``(ffmpeg 路径, 编码器名)``（ODD-05）。
# 为什么必须缓存：``_probe_encoder`` 是真的去跑一次最小编码，单次最长 8 秒。
# 而 ``detect_hw_accel`` 一次调用最多要探 9 个候选，转换队列里每条任务又各调
# 一次——一个 20 条的队列光探测就可能烧掉几分钟，全是重复劳动。硬件在程序运行
# 期间不会变，进程内缓存一次即可；用户换了显卡驱动重启软件就重新探测。
_PROBE_CACHE: dict[tuple[str, str], bool] = {}
_PROBE_LOCK = threading.Lock()


def _nvidia_runtime_available() -> bool:
    """True 表示 NVENC 运行时可用（Windows: nvcuda.dll 存在）。"""
    if sys.platform == "win32":
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        return os.path.exists(os.path.join(root, "System32", "nvcuda.dll"))
    return False


def _encoder_declared(ffmpeg_path: str, encoder: str) -> bool:
    """轻量检查：这个 ffmpeg 是否**声明**支持该编码器。

    ``ffmpeg -h encoder=<name>`` 只解析内部表、不碰硬件，毫秒级返回。用它做第一
    道筛子，能把「这个 ffmpeg 根本没编译该编码器」的情况挡在昂贵的实编码探测之外。
    """
    try:
        proc = run_silent(
            [ffmpeg_path, "-hide_banner", "-h", f"encoder={encoder}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    output = f"{proc.stdout or ''}{proc.stderr or ''}".lower()
    if "is not recognized" in output or "unknown encoder" in output:
        return False
    return f"encoder {encoder}".lower() in output


def _probe_encoder(ffmpeg_path: str, encoder: str) -> bool:
    """实际跑一次最小编码，验证硬件编码器在**运行时**真的可用。

    Args:
        ffmpeg_path: ffmpeg 可执行文件路径。
        encoder: 待验证的编码器名，如 ``h264_nvenc``。
    Returns:
        编码成功返回 True，其余一律 False。
    Notes:
        只查 ``-encoders`` 不够：nvenc 要 NVIDIA 驱动、qsv 要 Intel 核显与驱动、
        amf 要 AMD 显卡——「编译进 ffmpeg」不等于「这台机器能用」。

        v0.8.1 Bug2b：探测必须与真实转码用**同一套视频编码参数**。此前探测只
        用裸 ``-c:v h264_amf``，而真实转码会带 ``-rc vbr_quality -qv 23``——
        某些 ffmpeg 构建的 amf 不认 ``-qv``，于是「探测通过、转码报
        Unrecognized option 'qv' 且无 CPU 回退」。现在探测拼入
        :func:`~momentshift.core.presets._gpu_video_encode_args` 产出的参数
        （与 ``presets._gpu_video_args`` 的视频编码部分完全一致；音频参数
        ``-c:a aac -b:a 192k`` 不含在内，因为 lavfi color 输入没有音频流），
        参数不被支持时探测直接判 False，走 CPU 回退。
    """
    from .presets import _gpu_video_encode_args

    try:
        cmd = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=128x128:d=0.1",
            "-frames:v",
            "1",
            *_gpu_video_encode_args(encoder),
            "-f",
            "null",
            "-",
        ]
        proc = run_silent(cmd, capture_output=True, text=True, timeout=8)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        # 探测失败一律视为"该编码器不可用"，回退 CPU 编码即可，无需打断调用方。
        return False


def encoder_usable(ffmpeg_path: str | None, encoder: str | None) -> bool:
    """该 ffmpeg 能否真正使用 ``encoder``（带进程内缓存）。

    两级探测：先问 ffmpeg「你认识这个编码器吗」（毫秒级），认识才真的跑一次最小
    编码验证硬件（秒级）。结果按 ``(ffmpeg, 编码器)`` 缓存。

    Args:
        ffmpeg_path: ffmpeg 可执行文件路径；为空直接判不可用。
        encoder: 编码器名，例如 ``h264_nvenc``；为空直接判不可用。
    Returns:
        True 表示可用。任何探测异常都返回 False —— 回退 CPU 永远是安全的。
    """
    if not ffmpeg_path or not encoder:
        return False
    key = (ffmpeg_path, encoder)
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(key)
    if cached is not None:
        return cached

    # 注意：探测本身放在锁外。它最长要 13 秒，占着锁会把整个转换队列堵死；
    # 并发重复探测同一个编码器最多浪费一次，比串行阻塞划算得多。
    if "nvenc" in encoder and not _nvidia_runtime_available():
        usable = False
    elif not _encoder_declared(ffmpeg_path, encoder):
        usable = False
    else:
        usable = _probe_encoder(ffmpeg_path, encoder)

    with _PROBE_LOCK:
        _PROBE_CACHE[key] = usable
    log.info("编码器探测：%s → %s", encoder, "可用" if usable else "不可用")
    return usable


def clear_probe_cache() -> None:
    """清空编码器探测缓存（换 ffmpeg 路径或测试时用）。"""
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()


def detect_hw_accel(ffmpeg_path: str | None) -> dict[str, str | None]:
    """探测本机可用的硬件编码器。

    Args:
        ffmpeg_path: ffmpeg 可执行文件路径；为空直接返回全 ``None``。
    Returns:
        形如 ``{"h264": 编码器名或 None, "hevc": 编码器名或 None}``；
        取值为 ``None`` 表示「没有可用的 GPU 编码器，请回退 CPU」。
    """
    result: dict[str, str | None] = {"h264": None, "hevc": None}
    if not ffmpeg_path:
        return result

    encoders = get_encoders(ffmpeg_path)

    # 按「兼容面广 + 画质好」排序，命中第一个可用的就停
    h264_candidates = [
        "h264_nvenc",  # NVIDIA
        "h264_qsv",  # Intel
        "h264_amf",  # AMD
        "h264_videotoolbox",  # Apple
        "h264_v4l2m2m",  # Linux V4L2
    ]
    hevc_candidates = [
        "hevc_nvenc",
        "hevc_qsv",
        "hevc_amf",
        "hevc_videotoolbox",
    ]

    # NVENC 驱动检查与实编码探测都收进 encoder_usable()，并带缓存。
    for candidate in h264_candidates:
        if candidate in encoders and encoder_usable(ffmpeg_path, candidate):
            result["h264"] = candidate
            break
    for candidate in hevc_candidates:
        if candidate in encoders and encoder_usable(ffmpeg_path, candidate):
            result["hevc"] = candidate
            break
    return result


def best_available(ffmpeg_path: str | None) -> str:
    """把硬件加速探测结果整理成给界面/日志看的可读摘要。

    Returns:
        无可用 GPU 编码器时返回 ``"CPU"``，否则形如 ``"GPU (h264_nvenc)"``。
    """
    hw = detect_hw_accel(ffmpeg_path)
    found = [v for v in hw.values() if v]
    if not found:
        return "CPU"
    return "GPU (" + ", ".join(sorted(found)) + ")"
