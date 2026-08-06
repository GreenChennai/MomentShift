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
import threading

from .ffmpeg import get_encoders
from .logger import get_logger
from .platform import IS_LINUX, IS_MACOS, IS_WINDOWS, run_silent

log = get_logger("hardware")

# 编码器可用性缓存，键为 ``(ffmpeg 路径, 编码器名)``（ODD-05）。
# 为什么必须缓存：``_probe_encoder`` 是真的去跑一次最小编码，单次最长 8 秒。
# 而 ``detect_hw_accel`` 一次调用最多要探 9 个候选，转换队列里每条任务又各调
# 一次——一个 20 条的队列光探测就可能烧掉几分钟，全是重复劳动。硬件在程序运行
# 期间不会变，进程内缓存一次即可；用户换了显卡驱动重启软件就重新探测。
_PROBE_CACHE: dict[tuple[str, str], bool] = {}
_PROBE_LOCK = threading.Lock()


def _nvidia_runtime_available() -> bool:
    """True 表示 NVENC/CUDA 运行时可用。

    - Windows: ``nvcuda.dll`` 存在于 System32。
    - Linux:   ``libcuda.so`` 在标准库路径可定位（CUDA 驱动已装）。
    - macOS:   CUDA 不支持，返回 ``False``。
    """
    if IS_WINDOWS:
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        return os.path.exists(os.path.join(root, "System32", "nvcuda.dll"))
    if IS_LINUX:
        import ctypes.util

        return ctypes.util.find_library("cuda") is not None
    return False


def nvidia_cuda_available() -> bool:
    """本机是否具备 NVIDIA CUDA 能力（硬件 + 驱动层面）。

    v0.8.18 Bug1：这是「硬件能力」探测（有 N 卡 + CUDA 驱动即 True），与
    onnxruntime 是否内置 ``CUDAExecutionProvider`` 无关。模型清单里 ``hw_req``
    为 ``nvidia_cuda`` 的条目应据此判定，而不是看运行时 provider —— 否则
    RTX 5070 Ti 用户会因为随包 onnxruntime 是 CPU 版而被误报「硬件不支持」。
    """
    return _nvidia_runtime_available()


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


# =============================================================================
# ASR 推理设备探测（v0.8.5，功能 4/5）
# =============================================================================
# 官方语义：只有「NVIDIA 显卡 + CUDA 驱动 + onnxruntime 提供 CUDAExecutionProvider」
# 才用 GPU，其余（AMD/Intel/无卡/仅 CPU 版 onnxruntime）一律 CPU。
#
# 当前发布版打包的是 CPU 版 onnxruntime（``onnxruntime.get_available_providers()``
# 不含 CUDAExecutionProvider），所以实际都走 CPU；但策略代码必须正确——用户将来
# 换成 onnxruntime-gpu 即自动生效。检测结果按进程缓存（硬件在运行期不变）。
_ASR_DEVICE_CACHE: str | None = None
_ASR_DEVICE_LOCK = threading.Lock()

# 供测试注入的默认 provider 探测函数（默认取 onnxruntime.get_available_providers）。
def _default_ort_providers() -> list[str]:
    """返回当前 onnxruntime 可用的 provider 列表；导入失败返回空列表。"""
    try:
        from onnxruntime import get_available_providers  # 延迟导入，应用启动不拉 onnxruntime

        return list(get_available_providers())
    except Exception:  # noqa: BLE001 - 探测失败一律视为仅 CPU，安全回退
        return []


def asr_inference_device(providers: list[str] | None = None, nvidia_ok: bool | None = None) -> str:
    """纯函数：按硬件 + onnxruntime 能力决定 ASR 推理设备。

    Args:
        providers: onnxruntime 可用 provider 列表；None 时实时探测。
        nvidia_ok: NVIDIA 运行时是否可用（Windows 查 nvcuda.dll / Linux 查 libcuda.so）；None 时实时探测。

    Returns:
        ``"cuda"`` 仅当 NVIDIA 驱动可用且 providers 含 ``CUDAExecutionProvider``；
        其余一律 ``"cpu"``。
    """
    if nvidia_ok is None:
        nvidia_ok = _nvidia_runtime_available()
    if not nvidia_ok:
        return "cpu"
    if providers is None:
        providers = _default_ort_providers()
    return "cuda" if "CUDAExecutionProvider" in providers else "cpu"


def cached_asr_device() -> str:
    """带进程级缓存的 ASR 推理设备（一次检测多次用）。"""
    global _ASR_DEVICE_CACHE
    with _ASR_DEVICE_LOCK:
        if _ASR_DEVICE_CACHE is None:
            _ASR_DEVICE_CACHE = asr_inference_device()
        return _ASR_DEVICE_CACHE


def clear_asr_device_cache() -> None:
    """清空 ASR 设备缓存（测试 / 换 onnxruntime 后重探）。"""
    global _ASR_DEVICE_CACHE
    with _ASR_DEVICE_LOCK:
        _ASR_DEVICE_CACHE = None


def asr_device_label(device: str) -> str:
    """设备逻辑值 → 界面可读标签（与 i18n 无关，纯枚举）。"""
    if device == "cuda":
        return "NVIDIA CUDA"
    return "CPU"


def detect_ram_gb() -> float | None:
    """返回物理内存大小（GB）；探测失败返回 None。

    - Windows: ``GlobalMemoryStatusEx``。
    - Linux:   ``/proc/meminfo`` 的 ``MemTotal``。
    - macOS:   ``sysctl hw.memsize``。

    Returns:
        内存 GB 数（浮点）；无法探测时 None。
    """
    if IS_WINDOWS:
        try:
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MemoryStatus()
            stat.dwLength = ctypes.sizeof(_MemoryStatus)
            ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            if not ok:
                return None
            return stat.ullTotalPhys / (1024.0**3)
        except Exception:  # noqa: BLE001 - 探测失败按未知处理
            return None
    if IS_LINUX:
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return kb / (1024.0**3)
        except Exception:  # noqa: BLE001 - /proc/meminfo 不可读按未知处理
            return None
        return None
    if IS_MACOS:
        try:
            proc = run_silent(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0:
                return int(proc.stdout.strip()) / (1024.0**3)
        except Exception:  # noqa: BLE001 - sysctl 不可用按未知处理
            return None
        return None
    return None


# 模型硬件要求的可读原因（i18n key 前缀 ``asr.model.hw_reason`` 由界面拼装）。
def model_hw_satisfied(
    spec: dict,
    device: str = "cpu",
    ram_gb: float | None = None,
    nvidia_ok: bool | None = None,
) -> tuple[bool, str | None]:
    """判断模型清单条目是否满足本机硬件要求。

    Args:
        spec: 模型清单条目（``hw_req`` 字段为 ``"nvidia_cuda"`` 或
            ``{"min_ram_gb": N}``；缺省表示无要求）。
        device: 当前 ASR 推理设备（``"cuda"`` / ``"cpu"``）。
        ram_gb: 物理内存 GB；None 表示未知（按不满足 min_ram 处理）。
        nvidia_ok: 本机是否具备 NVIDIA CUDA 能力；None 时实时探测。

    Returns:
        ``(是否满足, 不满足原因或 None)``。原因用于界面展示，键值见
        ``asr.model.hw_reason.*``。

    Notes:
        v0.8.18 Bug1：``nvidia_cuda`` 要求按「硬件是否具备 NVIDIA CUDA 能力」
        判定（``nvidia_cuda_available()``），而不是按 onnxruntime 是否提供
        CUDA EP。随包 onnxruntime 是 CPU 版，若按后者判定，RTX 5070 Ti 这类
        真实 N 卡用户会被误报「硬件不支持」。``device == "cuda"`` 仍视为满足，
        兼容旧语义与单测（显式传入 cuda 设备时无需求证 nvidia_ok）。
    """
    hw_req = spec.get("hw_req")
    if not hw_req:
        return True, None
    if nvidia_ok is None:
        nvidia_ok = _nvidia_runtime_available()
    cuda_ok = device == "cuda" or bool(nvidia_ok)
    if isinstance(hw_req, str) and hw_req == "nvidia_cuda":
        if cuda_ok:
            return True, None
        return False, "nvidia_cuda"
    if isinstance(hw_req, dict):
        # v0.8.9：支持组合条件 {"nvidia_cuda": True, "min_ram_gb": N}
        if hw_req.get("nvidia_cuda") and not cuda_ok:
            return False, "nvidia_cuda"
        need = float(hw_req.get("min_ram_gb") or 0)
        if need > 0 and (ram_gb is None or ram_gb < need):
            return False, "min_ram_gb"
        return True, None
    # 未知硬件要求：保守放行
    return True, None
