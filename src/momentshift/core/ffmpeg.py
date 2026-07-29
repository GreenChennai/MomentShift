"""ffmpeg discovery, version and capability probing.

MomentShift *bundles* ffmpeg next to the executable for distribution (the CI
downloads the right static build per platform and PyInstaller drops it in the
bundle). We therefore look in several places, in priority order, before falling
back to the system ``PATH``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path


def _binary_names() -> tuple[str, str]:
    """Return (ffmpeg, ffprobe) executable names for the current platform."""
    if sys.platform == "win32":
        return "ffmpeg.exe", "ffprobe.exe"
    return "ffmpeg", "ffprobe"


def bundled_locations() -> list[Path]:
    """Directories that may contain a bundled ffmpeg/ffprobe binary."""
    locations: list[Path] = []
    if getattr(sys, "frozen", False):
        # PyInstaller one-folder build: binaries sit next to the executable.
        locations.append(Path(sys.executable).parent)
    else:
        here = Path(__file__).resolve()
        # repo root is four levels up: core/ffmpeg.py -> momentshift/core -> src
        repo_root = here.parents[3]
        locations.append(repo_root / "tools" / "ffmpeg_bin")
        # also: a sibling folder next to the package (dev convenience)
        locations.append(here.parent.parent / "ffmpeg_bin")
    return locations


def find_ffmpeg(prefer_bundled: bool = True) -> str | None:
    """Locate the ffmpeg executable. Returns an absolute path or ``None``."""
    ffmpeg_name, _ = _binary_names()
    bundled = [p / ffmpeg_name for p in bundled_locations()]

    if prefer_bundled:
        for p in bundled:
            if p.exists():
                return str(p)
        path_version = shutil.which(ffmpeg_name)
        if path_version:
            return path_version
        for p in bundled:  # last resort: bundled even if we preferred path
            if p.exists():
                return str(p)
        return None

    path_version = shutil.which(ffmpeg_name)
    if path_version:
        return path_version
    for p in bundled:
        if p.exists():
            return str(p)
    return None


def find_ffprobe() -> str | None:
    """Locate the ffprobe executable (same search strategy as ffmpeg)."""
    _, ffprobe_name = _binary_names()
    bundled = [p / ffprobe_name for p in bundled_locations()]
    for p in bundled:
        if p.exists():
            return str(p)
    return shutil.which(ffprobe_name)


def get_version(ffmpeg_path: str) -> str | None:
    """Return the first line of ``ffmpeg -version``, or ``None`` on failure."""
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode == 0:
            return proc.stdout.splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def get_encoders(ffmpeg_path: str) -> set[str]:
    """Parse ``ffmpeg -encoders`` and return the set of encoder names."""
    encoders: set[str] = set()
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return encoders

    pattern = re.compile(r"^\s*[VAS]\.[.\w]*\s+(\S+)")
    for line in proc.stdout.splitlines():
        match = pattern.match(line)
        if match:
            encoders.add(match.group(1))
    return encoders


def get_hwaccels(ffmpeg_path: str) -> set[str]:
    """Parse ``ffmpeg -hwaccels`` and return the set of hardware methods."""
    accels: set[str] = set()
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-hwaccels"],
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return accels

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Hardware") or set(line) <= set("- "):
            continue
        accels.add(line)
    return accels
