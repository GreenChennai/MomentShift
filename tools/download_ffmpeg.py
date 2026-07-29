#!/usr/bin/env python
"""Download ffmpeg + ffprobe static binaries for the current platform.

Used by CI (and locally) to bundle ffmpeg next to the PyInstaller output so the
app works out-of-the-box. Pure stdlib — no pip dependencies.

Usage::

    python tools/download_ffmpeg.py <destination_dir>

Sources (static, no installer required):
- Windows : gyan.dev essentials build
- macOS   : evermeet.cx releases
- Linux   : johnvansickle.com static builds
"""

import argparse
import io
import os
import shutil
import sys
import tarfile
import urllib.request
import zipfile


def _platform_sources():
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


def _download(url, dest_dir):
    print(f"[download_ffmpeg] {url}")
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
                    print(f"[download_ffmpeg] extracted {out_name}")
    else:  # tar.xz
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as tf:
            for member in tf.getmembers():
                if _is_wanted(member.name, is_windows):
                    base = os.path.basename(member.name)
                    f = tf.extractfile(member)
                    with open(os.path.join(dest_dir, base), "wb") as out:
                        shutil.copyfileobj(f, out)
                    print(f"[download_ffmpeg] extracted {base}")


def main():
    parser = argparse.ArgumentParser(description="Download ffmpeg/ffprobe binaries.")
    parser.add_argument("dest", help="Destination directory")
    args = parser.parse_args()

    os.makedirs(args.dest, exist_ok=True)
    for url, _kind in _platform_sources():
        _download(url, args.dest)

    # Ensure executables are runnable on POSIX systems.
    if not sys.platform.startswith("win"):
        for name in ("ffmpeg", "ffprobe"):
            path = os.path.join(args.dest, name)
            if os.path.exists(path):
                os.chmod(path, 0o755)

    print("[download_ffmpeg] done.")


if __name__ == "__main__":
    main()
