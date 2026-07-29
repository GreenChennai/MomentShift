#!/usr/bin/env python
"""Thin CLI wrapper around :func:`momentshift.core.ffmpeg_download.download_ffmpeg`.

Kept for CI / local use so the download logic lives in one place (the package)
and stays available to the in-app "one-click download" feature too.

Usage::

    python tools/download_ffmpeg.py <destination_dir>
"""

import argparse
import os
import sys

# Make the package importable when run standalone (e.g. in CI before install).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from momentshift.core.ffmpeg_download import download_ffmpeg  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Download ffmpeg/ffprobe binaries.")
    parser.add_argument("dest", help="Destination directory")
    args = parser.parse_args()

    ok, msg = download_ffmpeg(args.dest)
    if not ok:
        print(f"[download_ffmpeg] FAILED: {msg}", file=sys.stderr)
        sys.exit(1)
    print("[download_ffmpeg] done.")


if __name__ == "__main__":
    main()
