"""Download the optional image-compression tools (oxipng / jpegoptim)
into the unified ``tools/`` folder next to the executable.

These binaries are *small* (unlike ffmpeg) so the app can manage them itself
via a one-click download button, keeping them in one tidy place instead of
expecting the user to drop files next to the exe. Pillow always covers the
baseline, so a missing tool only disables that one high-end backend.

v0.7.0：移除 OptiPNG / MozJPEG（已被内置的 jpegoptim 取代）。

Pure stdlib networking + Qt worker plumbing — no pip dependencies.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import urllib.request
import zipfile

from .qt_compat import QObject, Signal, QRunnable


class DownloadSignals(QObject):
    started = Signal(str)            # tool id being downloaded
    finished = Signal(str, bool, str)  # (tool_id, ok, message)


# --- source resolution -----------------------------------------------------
# Each entry describes how to fetch a Windows build of the tool. We prefer the
# official GitHub "latest release" asset and fall back to a pinned URL.
_TOOLS: dict[str, dict] = {
    "oxipng": {
        "repo": "oxipng/oxipng",
        "asset": "x86_64-pc-windows-msvc.zip",
        "binaries": ["oxipng.exe"],
        "fallback": "https://github.com/oxipng/oxipng/releases/download/v10.1.1/oxipng-10.1.1-x86_64-pc-windows-msvc.zip",
    },
    "jpegoptim": {
        "repo": "tjko/jpegoptim",
        "asset": "x64-windows.zip",
        "binaries": ["jpegoptim.exe"],
        "fallback": "https://github.com/tjko/jpegoptim/releases/download/v1.5.6/jpegoptim-1.5.6-x64-windows.zip",
    },
}


def _github_latest_asset_url(repo: str, asset_substr: str) -> str | None:
    """Resolve the download URL of the latest GitHub release asset containing
    ``asset_substr`` (case-insensitive). Returns ``None`` on any failure."""
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "MomentShift"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for a in data.get("assets", []):
            name = (a.get("name") or "").lower()
            if asset_substr.lower() in name:
                return a.get("browser_download_url")
    except Exception as exc:  # network / rate-limit / parse — caller falls back
        print(f"[tools_download] github resolve failed for {repo}: {exc}")
    return None


def _download_url(url: str, dest_dir: str, wanted: list[str]) -> list[str]:
    """Download ``url`` (zip) and extract ``wanted`` binaries into ``dest_dir``.
    Returns the list of extracted file names."""
    req = urllib.request.Request(url, headers={"User-Agent": "MomentShift"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = resp.read()
    extracted: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.namelist():
            base = os.path.basename(member)
            if base.lower() in (w.lower() for w in wanted):
                with zf.open(member) as src, open(os.path.join(dest_dir, base), "wb") as out:
                    shutil.copyfileobj(src, out)
                extracted.append(base)
                print(f"[tools_download] extracted {base}")
    return extracted


def download_tool(tool_id: str, dest_dir: str) -> tuple[bool, str]:
    """Download one tool into ``dest_dir``. Returns ``(ok, message)``."""
    spec = _TOOLS.get(tool_id)
    if not spec:
        return False, f"unknown tool {tool_id}"
    os.makedirs(dest_dir, exist_ok=True)
    wanted = spec["binaries"]

    url = None
    if spec.get("repo"):
        url = _github_latest_asset_url(spec["repo"], spec["asset"])
    if not url:
        url = spec.get("url") or spec.get("fallback")
    if not url:
        return False, f"no download source for {tool_id}"

    try:
        got = _download_url(url, dest_dir, wanted)
        if not got:
            return False, f"no matching binaries ({','.join(wanted)}) in archive"
        return True, f"downloaded {', '.join(got)}"
    except Exception as exc:  # surface network/extract failure to the UI
        return False, str(exc)


def download_all_tools(dest_dir: str) -> dict[str, tuple[bool, str]]:
    """Download every known tool. Returns ``{tool_id: (ok, message)}``."""
    return {tid: download_tool(tid, dest_dir) for tid in _TOOLS}


class ToolsDownloadWorker(QRunnable):
    """Runs :func:`download_tool` off the UI thread for a single tool."""

    def __init__(self, tool_id: str, dest_dir: str):
        super().__init__()
        self.setAutoDelete(True)
        self.tool_id = tool_id
        self.dest_dir = dest_dir
        self.signals = DownloadSignals()

    def run(self) -> None:
        self.signals.started.emit(self.tool_id)
        ok, msg = download_tool(self.tool_id, self.dest_dir)
        self.signals.finished.emit(self.tool_id, ok, msg)


class AllDownloadSignals(QObject):
    started = Signal()
    finished = Signal(dict)  # {tool_id: (ok, message)}


class ToolsDownloadAllWorker(QRunnable):
    """Downloads every known tool into ``dest_dir`` off the UI thread."""

    def __init__(self, dest_dir: str):
        super().__init__()
        self.setAutoDelete(True)
        self.dest_dir = dest_dir
        self.signals = AllDownloadSignals()

    def run(self) -> None:
        self.signals.started.emit()
        result = download_all_tools(self.dest_dir)
        self.signals.finished.emit(result)
