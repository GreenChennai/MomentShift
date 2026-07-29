"""Download ffmpeg + ffprobe static binaries into a target directory.

This is the single source of truth used both by the in-app "one-click
download" button (via :class:`FfmpegDownloadWorker`) and by the thin CLI
wrapper ``tools/download_ffmpeg.py``. Pure stdlib networking — no pip deps.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tarfile
import urllib.request
import zipfile

from .qt_compat import QObject, Signal, QRunnable


class DownloadSignals(QObject):
    """Signals tunneled out of the background download worker."""

    started = Signal()
    finished = Signal(bool, str)  # (ok, message)


def _platform_sources():
    """Return [(url, kind)] for the current platform's static ffmpeg build."""
    if sys.platform.startswith("win"):
        return [("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip", "zip")]
    if sys.platform == "darwin":
        return [
            ("https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip", "zip"),
            ("https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip", "zip"),
        ]
    # Linux
    machine = "amd64"
    try:
        machine = os.uname().machine
    except AttributeError:
        pass
    arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
    return [(
        f"https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-{arch}-static.tar.xz",
        "txz",
    )]


def _is_wanted(name, is_windows):
    base = os.path.basename(name)
    if is_windows:
        return base in ("ffmpeg.exe", "ffprobe.exe")
    return os.path.basename(base) in ("ffmpeg", "ffprobe")


def _download(url: str, dest_dir: str) -> None:
    print(f"[ffmpeg_download] {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "MomentShift"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = resp.read()

    is_windows = sys.platform.startswith("win")
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                if _is_wanted(member, is_windows):
                    out_name = os.path.basename(member)
                    with zf.open(member) as src, open(os.path.join(dest_dir, out_name), "wb") as out:
                        shutil.copyfileobj(src, out)
                    print(f"[ffmpeg_download] extracted {out_name}")
    else:  # tar.xz
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as tf:
            for member in tf.getmembers():
                if _is_wanted(member.name, is_windows):
                    base = os.path.basename(member.name)
                    f = tf.extractfile(member)
                    with open(os.path.join(dest_dir, base), "wb") as out:
                        shutil.copyfileobj(f, out)
                    print(f"[ffmpeg_download] extracted {base}")


def download_ffmpeg(dest_dir: str) -> tuple[bool, str]:
    """Download ffmpeg + ffprobe into ``dest_dir``. Returns ``(ok, message)``."""
    os.makedirs(dest_dir, exist_ok=True)
    is_windows = sys.platform.startswith("win")
    try:
        for url, _kind in _platform_sources():
            _download(url, dest_dir)
        # Ensure executables are runnable on POSIX systems.
        if not is_windows:
            for name in ("ffmpeg", "ffprobe"):
                path = os.path.join(dest_dir, name)
                if os.path.exists(path):
                    os.chmod(path, 0o755)
        return True, ""
    except Exception as exc:  # surface any network/extract failure to the UI
        return False, str(exc)


class FfmpegDownloadWorker(QRunnable):
    """Runs :func:`download_ffmpeg` off the UI thread."""

    def __init__(self, dest_dir: str):
        super().__init__()
        self.setAutoDelete(True)
        self.dest_dir = dest_dir
        self.signals = DownloadSignals()

    def run(self) -> None:
        self.signals.started.emit()
        ok, msg = download_ffmpeg(self.dest_dir)
        self.signals.finished.emit(ok, msg)
