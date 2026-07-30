"""Image / video upscaling engine for MomentShift.

Wraps the open-source **realesrgan-ncnn-vulkan** binary (Vulkan-based, cross
platform, GPU or CPU) — the same engine that powers Upscayl and is inspired by
Real-ESRGAN / Waifu2x-Extension-GUI.

Design notes (per product spec):
- The engine binary and its ncnn models are *not* bundled with the installer
  (they would bloat it massively). They are kept in a unified ``tools/realesrgan/``
  folder and fetched on demand via a one-click in-app download.
- The Windows engine zip from the Real-ESRGAN release already bundles the four
  standard ncnn models, so a single download gives the user both the engine and
  the default models — exactly the "download models on demand" flow requested.
- Still images are upscaled by the binary directly. GIF / video are processed
  with a frame pipeline: ``ffmpeg`` extracts the frames, the binary upscales the
  whole frame folder in one invocation, then ``ffmpeg`` recombines the upscaled
  frames (preserving audio when present).

Pure stdlib networking + Qt worker plumbing — no pip dependencies.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
# Suppress the per-task console window on Windows (no cmd popups for the upscaler engine).
WIN_SILENT = getattr(subprocess, "CREATE_NO_WINDOW", 0)
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, Tuple

from .qt_compat import QObject, Signal, QRunnable
from .config import tools_dir
from .logger import get_logger

log = get_logger("upscaler")


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------
def realesrgan_dir() -> Path:
    """Unified folder for the upscaling engine (``tools/realesrgan``)."""
    directory = tools_dir() / "realesrgan"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def engine_exe() -> Path:
    """Absolute path of the realesrgan-ncnn-vulkan executable."""
    return realesrgan_dir() / "realesrgan-ncnn-vulkan.exe"


def models_dir() -> Path:
    """Folder that holds the ncnn ``.bin``/``.param`` model files."""
    directory = realesrgan_dir() / "models"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def find_upscaler() -> Optional[str]:
    """Locate the realesrgan-ncnn-vulkan binary.

    Prefers the managed ``tools/realesrgan`` folder, then the system ``PATH``.
    """
    p = engine_exe()
    if p.is_file():
        return str(p)
    return shutil.which("realesrgan-ncnn-vulkan") or shutil.which("realesrgan-ncnn-vulkan.exe")


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
# The four models bundled in the engine zip. ``scale`` is the model's native
# output scale; the binary also accepts a final ``-s`` of 2/3/4 (it resizes
# after inference) but we default to the native scale for best quality.
MODELS: dict[str, dict] = {
    "realesrgan-x4plus": {
        "label": "Real-ESRGAN x4+",
        "scale": 4,
        "kind": "photo",
        "note": "通用照片 / 写实图像 (4x)",
    },
    "realesrgan-x4plus-anime": {
        "label": "Real-ESRGAN x4+ Anime",
        "scale": 4,
        "kind": "anime",
        "note": "动漫插画 (4x)",
    },
    "realesrnet-x4plus": {
        "label": "Real-ESRNet x4+",
        "scale": 4,
        "kind": "photo",
        "note": "去模糊 / 通用 (4x)",
    },
    "realesr-animevideov3": {
        "label": "AnimeVideo v3",
        "scale": 4,
        "kind": "video",
        "note": "动漫视频 (4x)",
    },
}

# Image / animated inputs handled directly; everything else goes through the
# ffmpeg frame pipeline.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
ANIM_EXTS = {".gif"}


def model_present(name: str) -> bool:
    """Whether a model's ``.bin``/``.param`` files are on disk."""
    return (models_dir() / f"{name}.bin").is_file() and (models_dir() / f"{name}.param").is_file()


def available_models() -> list[str]:
    """Model ids that are both defined and present on disk."""
    return [mid for mid in MODELS if model_present(mid)]


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def _run(cmd: list[str], timeout: int = 3600) -> Tuple[bool, str]:
    log.info("upscaler cmd: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            creationflags=WIN_SILENT,
        )
    except subprocess.TimeoutExpired:
        return False, "处理超时（超过 %d 秒）" % timeout
    except OSError as exc:
        return False, f"启动失败: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        err = err[-3:] if err else ["未知错误"]
        return False, " · ".join(err)[:400]
    return True, ""


def upscale_image(
    input_path: str,
    output_path: str,
    model: str,
    scale: int = 4,
    tile: int = 0,
    gpu: str = "auto",
) -> Tuple[bool, str]:
    """Upscale a single image (or a directory of images in one call).

    Returns ``(ok, message)``.
    """
    exe = find_upscaler()
    if not exe:
        return False, "未找到 realesrgan-ncnn-vulkan 引擎，请先下载"
    if not model_present(model):
        return False, f"模型缺失: {model}（请下载引擎以获取标准模型）"

    cmd = [
        exe,
        "-i", input_path,
        "-o", output_path,
        "-n", model,
        "-s", str(scale),
        "-m", str(models_dir()),
    ]
    if tile:
        cmd += ["-t", str(tile)]
    if gpu == "cpu":
        cmd += ["-g", "-1"]
    elif gpu not in ("auto", ""):
        cmd += ["-g", str(gpu)]

    ext = Path(output_path).suffix.lower().lstrip(".")
    if ext in ("jpg", "jpeg", "png", "webp"):
        cmd += ["-f", "jpg" if ext == "jpeg" else ext]

    return _run(cmd)


def _probe_fps(ffmpeg: str, input_path: str) -> float:
    """Best-effort frame-rate probe via ffprobe (fallback 25)."""
    ffprobe = shutil.which("ffprobe") or str(Path(ffmpeg).parent / "ffprobe.exe")
    if not ffprobe or not Path(ffprobe).is_file():
        return 25.0
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "default=noprint_wrappers=1:nokey=1",
             input_path],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
            creationflags=WIN_SILENT,
        )
        val = proc.stdout.strip()
        if "/" in val:
            a, b = val.split("/")
            return float(a) / float(b) if float(b) else 25.0
        if val:
            return float(val)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return 25.0


def upscale_frames(
    input_path: str,
    output_path: str,
    model: str,
    scale: int = 4,
    tile: int = 0,
    gpu: str = "auto",
) -> Tuple[bool, str]:
    """Upscale an animated GIF or a video via an ffmpeg frame pipeline.

    Extract frames -> upscale the whole folder in one binary call -> recombine
    the upscaled frames (with audio when present). Returns ``(ok, message)``.
    """
    from .ffmpeg import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, "需要 ffmpeg 来处理视频 / GIF（请先安装或下载 ffmpeg）"
    exe = find_upscaler()
    if not exe:
        return False, "未找到 realesrgan-ncnn-vulkan 引擎，请先下载"
    if not model_present(model):
        return False, f"模型缺失: {model}（请下载引擎以获取标准模型）"

    out_ext = Path(output_path).suffix.lower().lstrip(".")
    tmp = tempfile.mkdtemp(prefix="ms_up_")
    frames_in = Path(tmp) / "in"
    frames_out = Path(tmp) / "out"
    frames_in.mkdir(parents=True, exist_ok=True)
    frames_out.mkdir(parents=True, exist_ok=True)
    try:
        # 1) extract frames
        ok, msg = _run(
            [ffmpeg, "-y", "-i", input_path, str(frames_in / "%06d.png")],
            timeout=600,
        )
        if not ok:
            return False, f"抽帧失败: {msg}"
        in_frames = sorted(frames_in.glob("*.png"))
        if not in_frames:
            return False, "未从源文件抽取到任何帧"

        # 2) upscale the whole folder in one invocation
        ok, msg = upscale_image(
            str(frames_in), str(frames_out), model, scale, tile, gpu
        )
        if not ok:
            return False, f"放大失败: {msg}"

        # 3) renormalise frame names so ffmpeg can recombine sequentially
        out_frames = sorted(frames_out.glob("*.png"))
        if not out_frames:
            return False, "放大后未生成帧文件"
        for i, f in enumerate(out_frames, 1):
            dst = frames_out / f"{i:06d}.png"
            if dst != f:
                os.replace(f, dst)

        fps = _probe_fps(ffmpeg, input_path) or 25.0

        # 4) recombine
        if out_ext == "gif":
            ok, msg = _run(
                [ffmpeg, "-y", "-framerate", f"{fps:g}", "-i", str(frames_out / "%06d.png"),
                 "-vf", "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                 output_path],
                timeout=600,
            )
        else:
            cmd = [ffmpeg, "-y", "-framerate", f"{fps:g}", "-i", str(frames_out / "%06d.png"),
                   "-i", input_path, "-map", "0:v:0", "-map", "1:a?", "-c:v", "libx264",
                   "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", output_path]
            ok, msg = _run(cmd, timeout=900)
        if not ok:
            return False, f"合成失败: {msg}"
        return True, ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def upscale_media(
    input_path: str,
    output_path: str,
    model: str,
    scale: int = 4,
    tile: int = 0,
    gpu: str = "auto",
) -> Tuple[bool, str]:
    """Upscale any supported input (image / GIF / video)."""
    ext = Path(input_path).suffix.lower()
    if ext in IMAGE_EXTS:
        return upscale_image(input_path, output_path, model, scale, tile, gpu)
    if ext in ANIM_EXTS or ext in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}:
        return upscale_frames(input_path, output_path, model, scale, tile, gpu)
    return False, f"不支持的输入格式: {ext}"


# --------------------------------------------------------------------------
# Engine download (binary + standard ncnn models in one zip)
# --------------------------------------------------------------------------
ENGINE_REPO = "xinntao/Real-ESRGAN"
ENGINE_ASSET = "realesrgan-ncnn-vulkan-20220424-windows.zip"
ENGINE_FALLBACK = (
    "https://github.com/xinntao/Real-ESRGAN/releases/download/"
    "v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip"
)
ENGINE_PAGE = "https://github.com/xinntao/Real-ESRGAN/releases"


class DownloadSignals(QObject):
    started = Signal()
    finished = Signal(bool, str)  # (ok, message)


def _github_latest_asset_url(repo: str, asset_substr: str) -> Optional[str]:
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "MomentShift"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for a in data.get("assets", []):
            name = (a.get("name") or "").lower()
            if asset_substr.lower() in name:
                return a.get("browser_download_url")
    except Exception as exc:  # network / rate-limit / parse
        print(f"[upscaler] github resolve failed for {repo}: {exc}")
    return None


def download_upscaler(dest_dir: str) -> Tuple[bool, str]:
    """Download the realesrgan-ncnn-vulkan engine + bundled models into ``dest_dir``.

    The zip is extracted in place, so ``dest_dir/realesrgan-ncnn-vulkan.exe`` and
    ``dest_dir/models/*.bin`` end up where :func:`find_upscaler` / :func:`model_present`
    expect them. Returns ``(ok, message)``.
    """
    os.makedirs(dest_dir, exist_ok=True)
    url = _github_latest_asset_url(ENGINE_REPO, ENGINE_ASSET) or ENGINE_FALLBACK
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MomentShift"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(dest_dir)
        print(f"[upscaler] extracted engine into {dest_dir}")
        return True, "引擎与模型已下载"
    except Exception as exc:
        return False, str(exc)


class UpscalerDownloadWorker(QRunnable):
    """Runs :func:`download_upscaler` off the UI thread."""

    def __init__(self, dest_dir: str):
        super().__init__()
        self.setAutoDelete(True)
        self.dest_dir = dest_dir
        self.signals = DownloadSignals()

    def run(self) -> None:
        self.signals.started.emit()
        ok, msg = download_upscaler(self.dest_dir)
        self.signals.finished.emit(ok, msg)
