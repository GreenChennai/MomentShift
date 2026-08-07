"""FFmpeg 抽帧工具（v0.8.25 新增，供「放大前后对比」窗口播放视频 / GIF）。

职责边界：
- 做：把视频 / GIF 用 ffmpeg 抽帧成一组临时 PNG，返回 (帧路径列表, 帧率)。
  帧率用于播放节奏；GIF 以原始帧率播放，视频按需降采样避免帧数爆炸。
- 不做：不负责播放（播放由 GUI 的帧序列驱动）；不解析媒体元数据。

被依赖：gui/compare_window。

设计要点：
- 全部用 **临时文件** 承载帧，不把解码结果留在内存里——长视频抽 60 帧
  每帧 1080p 也才几百 MB 内存量级，但 GUI 线程逐帧读图 + 换图更稳。
- 抽帧在**工作线程**调用（subprocess 阻塞式），GUI 线程只收结果列表。
- 帧数上限：视频默认 48 帧（约 2 秒 @24fps），GIF 全抽（帧数一般不多）。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .ffmpeg import find_ffmpeg
from .platform import run_silent

# 视频抽帧帧数上限。再长的视频也只抽这么多帧循环播放，避免临时文件爆炸。
_VIDEO_MAX_FRAMES = 48
# GIF 抽帧不受限（帧数通常几十），但设个保险上限防畸形 GIF。
_GIF_MAX_FRAMES = 200


def extract_frames(path: str, fps: float | None = None) -> tuple[list[str], float]:
    """把视频 / GIF 抽帧成 PNG 列表。

    Args:
        path: 输入媒体文件。
        fps: 目标帧率；``None`` 时视频用「≤48 帧」反推（至少 2fps），
            GIF 用原帧率（不指定 fps，ffmpeg 按 GIF 自带节奏抽全部帧）。

    Returns:
        ``(frame_paths, actual_fps)``。失败时返回 ``([], 0)``。

    Notes:
        **只能在工作线程调用**（内部 subprocess 阻塞）。帧是临时 PNG 文件，
        调用方负责在展示结束后清理（见 :func:`cleanup_frames`）。
    """
    if not path or not Path(path).exists():
        return [], 0
    ff = find_ffmpeg()
    if not ff:
        return [], 0

    suffix = Path(path).suffix.lower()
    is_gif = suffix == ".gif"

    tmpdir = tempfile.mkdtemp(prefix="ms_frames_")
    out_pattern = str(Path(tmpdir) / "f_%03d.png")

    # 视频：按 fps 抽帧并设上限；GIF：全抽。
    # 用 -vf fps=X 让 ffmpeg 按目标帧率采样，再用 -frames:v 封顶。
    if is_gif:
        cmd = [ff, "-y", "-i", path, "-vsync", "0", out_pattern]
        real_fps = float(fps or 10.0)
    else:
        target_fps = fps or 10.0
        cmd = [
            ff, "-y", "-i", path,
            "-vf", f"fps={target_fps}",
            "-frames:v", str(_VIDEO_MAX_FRAMES),
            out_pattern,
        ]
        real_fps = target_fps

    try:
        run_silent(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        _cleanup_dir(tmpdir)
        return [], 0

    frames = sorted(str(p) for p in Path(tmpdir).glob("f_*.png"))
    if not frames:
        _cleanup_dir(tmpdir)
        return [], 0
    if is_gif and len(frames) > _GIF_MAX_FRAMES:
        frames = frames[:: max(1, len(frames) // _GIF_MAX_FRAMES)]
    return frames, real_fps


def cleanup_frames(frames: list[str]) -> None:
    """删除抽帧产生的临时文件与目录（幂等，可在 GUI 线程调用）。"""
    if not frames:
        return
    root = Path(frames[0]).parent
    for f in frames:
        try:
            Path(f).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def _cleanup_dir(tmpdir: str) -> None:
    for f in Path(tmpdir).glob("f_*.png"):
        try:
            f.unlink()
        except OSError:
            pass
    try:
        os.rmdir(tmpdir)
    except OSError:
        pass
