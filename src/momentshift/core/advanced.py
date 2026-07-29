"""Advanced, per-category conversion options exposed in the UI.

These map directly onto ffmpeg parameters (inspired by FFmpegFreeUI's approach of
giving the user concrete, high-level knobs instead of raw flags). Each category has
its own sub-panel:

- image : output quality (lossless by default; for lossy targets a quality slider).
- video : resolution, frame rate, video bitrate, plus "merge into one file".
- audio : audio bitrate, plus "merge into one file".

The values are stored in a single module-level dict (``adv``) that the staging panel
mutates and that ``presets.build_args`` / ``converter`` read when building a command.
"""

from __future__ import annotations

from typing import Optional

# Resolution / fps / bitrate presets offered in the dropdowns. "original" means
# "leave ffmpeg's default (copy / auto)".
RESOLUTIONS = ["original", "3840x2160", "1920x1080", "1280x720", "854x480"]
FPS_OPTIONS = ["original", "60", "30", "25", "24"]
VIDEO_BITRATES = ["original", "20M", "10M", "5M", "2M", "1M"]
AUDIO_BITRATES = ["original", "320k", "256k", "192k", "128k"]


def default_options() -> dict:
    """Return a fresh copy of the default advanced options for every category."""
    return {
        "image": {
            "quality": 95,        # 1..100, higher = better (for lossy targets)
            "lossless": True,     # png stays lossless
        },
        "video": {
            "resolution": "original",
            "fps": "original",
            "bitrate": "original",
            "crf": 18,
            "merge": False,
        },
        "audio": {
            "bitrate": "original",
            "merge": False,
        },
    }


# Live, mutable options (the UI panel writes here; the engine reads here).
adv: dict = default_options()


def reset() -> None:
    global adv
    adv = default_options()


def get(category: str) -> dict:
    return adv.get(category, {})


def is_merge_enabled(category: str) -> bool:
    return bool(adv.get(category, {}).get("merge", False))


def build_advanced_args(category: str, target: str, options: Optional[dict] = None) -> list[str]:
    """Return extra ffmpeg args for ``category``/``target`` from ``options``.

    ``options`` defaults to the live ``adv`` for that category. The returned list is
    meant to be inserted into the argument stream by ``presets.build_args``.
    """
    if options is None:
        options = get(category)
    if not options:
        return []

    extra: list[str] = []

    if category == "image":
        quality = int(options.get("quality", 95))
        if target == "jpg":
            # mjpeg: -q:v 1 (best) .. 31 (worst). Map 100->~2, 1->~31.
            q = max(2, min(31, round(31 - quality / 100 * 29)))
            extra += ["-q:v", str(q)]
        elif target == "webp":
            extra += ["-quality", str(quality)]
        elif target == "png":
            # lossless; allow compression level 0..9 from quality.
            lvl = max(0, min(9, round(quality / 100 * 9)))
            extra += ["-compression_level", str(lvl)]
        elif target == "tiff":
            extra += ["-compression_algo", "deflate"]
        # bmp: no tuning.

    elif category == "video":
        vf_parts: list[str] = []
        res = options.get("resolution", "original")
        if res and res != "original":
            vf_parts.append(f"scale={res.split('x')[0]}:{res.split('x')[1]}")
        fps = options.get("fps", "original")
        if fps and fps != "original":
            vf_parts.append(f"fps={fps}")
        if vf_parts:
            extra += ["-vf", ",".join(vf_parts)]
        bitrate = options.get("bitrate", "original")
        if bitrate and bitrate != "original":
            extra += ["-b:v", str(bitrate), "-maxrate", str(bitrate),
                      "-bufsize", str(int(int(bitrate.rstrip("Mk")) * 2)) + bitrate[-1]]

    elif category == "audio":
        bitrate = options.get("bitrate", "original")
        if bitrate and bitrate != "original":
            extra += ["-b:a", str(bitrate)]

    return extra


def build_merge_args(category: str, input_paths: list[str], output_path: str,
                     options: Optional[dict] = None) -> list[str]:
    """Build a concat command that merges ``input_paths`` into ``output_path``.

    Video uses the concat filter with audio; audio-only uses ``v=0:a=1``.
    """
    if options is None:
        options = get(category)
    n = len(input_paths)
    cmd = ["-hide_banner", "-nostats", "-y"]
    for p in input_paths:
        cmd += ["-i", p]

    if category == "video":
        # [0:v][0:a][1:v][1:a]...concat=n=N:v=1:a=1[outv][outa]
        ins = "".join(f"[{i}:v][{i}:a]" for i in range(n))
        cmd += ["-filter_complex", f"{ins}concat=n={n}:v=1:a=1[outv][outa]",
                "-map", "[outv]", "-map", "[outa]",
                "-c:v", "libx264", "-crf", str(options.get("crf", 18)),
                "-preset", "slow", "-c:a", "aac", "-b:a", "192k"]
    else:  # audio
        ins = "".join(f"[{i}:a]" for i in range(n))
        cmd += ["-filter_complex", f"{ins}concat=n={n}:v=0:a=1[outa]",
                "-map", "[outa]", "-c:a", "aac", "-b:a", "192k"]

    cmd.append(output_path)
    return cmd
