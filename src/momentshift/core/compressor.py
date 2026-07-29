"""Image compression engine for MomentShift.

Two integration points use this module:

1. **Post-step after an ffmpeg image conversion** — when the user enabled
   "compress" in the advanced image settings, the conversion worker runs the
   compressor on the produced image before reporting success.
2. **The dedicated "Compress" interface** — a second major feature block for
   batch, multi-threaded image compression (lossless / lossy) that is
   independent of format conversion.

Backends (detected at runtime, never force-bundled — same philosophy as ffmpeg):

- ``pillow``  : always available (installed dependency). The native Python path.
                 Lossless via ``optimize``/metadata strip; lossy via ``quality``
                 (JPEG/WebP) or palette quantization (PNG).
- ``oxipng``  : external ``oxipng`` binary (Rust) — best-in-class PNG lossless.
- ``optipng`` : external ``optipng`` binary — industry-standard PNG lossless.
- ``mozjpeg`` : external ``cjpeg`` / ``jpegtran`` from Mozilla's mozjpeg — best
                 JPEG (lossless ``jpegtran`` re-optimization, lossy ``cjpeg``).

Strategy (per spec): we *wrap the open-source components via their CLI* (a
Python script calls the binary). This is the "small footprint" path — the app
stays compact and the heavy optimizers are supplied by the user (next to the
exe or on PATH), exactly like ffmpeg. Pillow is the native Python library used
for the always-available baseline.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .logger import get_logger

log = get_logger("compressor")


# --------------------------------------------------------------------------
# Tool discovery
# --------------------------------------------------------------------------
def app_dir() -> Path:
    """Directory that should hold user-supplied tools (next to the exe)."""
    from .config import app_base_dir

    return app_base_dir()


def find_tool(name: str) -> Optional[str]:
    """Locate an external tool by name.

    Searches the application directory (where the user drops ``oxipng.exe``,
    ``optipng.exe``, ``jpegtran.exe``, ``cjpeg.exe``) first, then ``PATH``.
    Returns the absolute path or ``None``.
    """
    candidates = [name, name + ".exe"]
    # 1) application folder (user-supplied, like ffmpeg)
    base = app_dir()
    for cand in candidates:
        p = base / cand
        if p.is_file():
            return str(p)
    # 2) system PATH
    for cand in candidates:
        loc = shutil.which(cand)
        if loc:
            return loc
    return None


# --------------------------------------------------------------------------
# Backend metadata
# --------------------------------------------------------------------------
# fmt -> set of backends that can handle it.
_PNG = {"png"}
_JPG = {"jpg", "jpeg"}
_WEBP = {"webp"}
_RASTER = {"bmp", "tiff", "gif"}


def available_backends() -> dict[str, dict]:
    """Return a mapping of backend_id -> metadata for backends that are usable.

    A backend is "available" if it is Pillow (always) or its external binary is
    found on disk.
    """
    out: dict[str, dict] = {}

    # Pillow is always present (installed dependency).
    out["pillow"] = {
        "name": "Pillow",
        "formats": _PNG | _JPG | _WEBP | _RASTER,
        "lossless": True,
        "lossy": True,
        "builtin": True,
    }

    ox = find_tool("oxipng")
    if ox:
        out["oxipng"] = {
            "name": "oxipng",
            "formats": _PNG,
            "lossless": True,
            "lossy": False,
            "builtin": False,
            "path": ox,
        }

    op = find_tool("optipng")
    if op:
        out["optipng"] = {
            "name": "OptiPNG",
            "formats": _PNG,
            "lossless": True,
            "lossy": False,
            "builtin": False,
            "path": op,
        }

    jt = find_tool("jpegtran")
    cj = find_tool("cjpeg")
    if jt or cj:
        out["mozjpeg"] = {
            "name": "Mozilla JPEG",
            "formats": _JPG,
            "lossless": bool(jt),
            "lossy": bool(cj),
            "builtin": False,
            "path": jt or cj,
        }

    return out


def best_backend(fmt: str, mode: str, preferred: Optional[str] = None) -> Optional[str]:
    """Pick the best available backend for ``fmt`` + ``mode``.

    ``preferred`` (e.g. ``"oxipng"``) is honoured when it supports the request;
    otherwise we fall back to a sensible default (oxipng/optipng for PNG
    lossless, mozjpeg for JPG, Pillow otherwise).
    """
    fmt = fmt.lower().lstrip(".")
    backs = available_backends()

    def supports(bid: str) -> bool:
        b = backs.get(bid)
        if not b:
            return False
        if fmt not in b["formats"]:
            return False
        if mode == "lossless" and not b["lossless"]:
            return False
        if mode == "lossy" and not b["lossy"]:
            return False
        return True

    if preferred and supports(preferred):
        return preferred

    order = {
        ("png", "lossless"): ["oxipng", "optipng", "pillow"],
        ("png", "lossy"): ["pillow"],
        ("jpg", "lossless"): ["mozjpeg", "pillow"],
        ("jpg", "lossy"): ["mozjpeg", "pillow"],
        ("webp", "lossless"): ["pillow"],
        ("webp", "lossy"): ["pillow"],
    }
    for bid in order.get((fmt, mode), ["pillow"]):
        if supports(bid):
            return bid
    # last resort
    return "pillow" if "pillow" in backs else None


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def compress_image(
    input_path: str,
    output_path: str,
    backend: str,
    mode: str = "lossless",
    quality: int = 100,
    opts: Optional[dict] = None,
) -> tuple[bool, str, int]:
    """Compress one image. Returns ``(ok, detail, saved_bytes)``.

    ``saved_bytes`` is ``src_size - dst_size`` (positive = space saved).
    """
    opts = opts or {}
    src = Path(input_path)
    dst = Path(output_path)
    if not src.is_file():
        return False, "input missing", 0
    src_size = src.stat().st_size

    try:
        if backend == "pillow":
            ok, detail = _pillow(input_path, output_path, mode, quality, opts)
        elif backend == "oxipng":
            ok, detail = _oxipng(input_path, output_path, opts)
        elif backend == "optipng":
            ok, detail = _optipng(input_path, output_path, opts)
        elif backend == "mozjpeg":
            ok, detail = _mozjpeg(input_path, output_path, mode, quality, opts)
        else:
            ok, detail = False, f"unknown backend {backend}"
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("compression failed (%s -> %s)", input_path, output_path)
        return False, f"{type(exc).__name__}: {exc}", 0

    if not ok:
        return False, detail, 0

    try:
        dst_size = dst.stat().st_size
    except OSError:
        dst_size = 0
    return True, detail, src_size - dst_size


def compress_auto(
    input_path: str,
    output_path: str,
    mode: str = "lossless",
    quality: int = 100,
    opts: Optional[dict] = None,
    preferred: Optional[str] = None,
) -> tuple[bool, str, int]:
    """Convenience wrapper: pick the best backend and compress (Pillow fallback)."""
    fmt = Path(input_path).suffix.lower().lstrip(".")
    backend = best_backend(fmt, mode, preferred) or "pillow"
    return compress_image(input_path, output_path, backend, mode, quality, opts)


def needs_conversion(src_fmt: str, dst_fmt: str) -> bool:
    """Whether a compress-only run must also transcode the format."""
    return src_fmt.lower().lstrip(".") != dst_fmt.lower().lstrip(".")


def transcode_and_compress(
    input_path: str,
    output_path: str,
    fmt: str,
    mode: str = "lossless",
    quality: int = 100,
    opts: Optional[dict] = None,
    preferred: Optional[str] = None,
) -> tuple[bool, str, int]:
    """Transcode ``input`` into ``fmt`` and then run the compressor on it.

    Used by the dedicated Compress interface when the user picks a target format
    different from the source (e.g. a folder of BMPs -> optimized PNGs).
    """
    from PIL import Image

    fmt = fmt.lower().lstrip(".")
    tmp = str(output_path) + ".tc.tmp"
    img = Image.open(input_path)
    if fmt in ("jpg", "jpeg"):
        img.convert("RGB").save(tmp, "JPEG", quality=quality, subsampling=0, optimize=True)
    elif fmt == "png":
        img.save(tmp, "PNG")
    elif fmt == "webp":
        img.save(tmp, "WEBP", quality=quality)
    elif fmt == "tiff":
        img.save(tmp, "TIFF", compression="deflate")
    else:
        img.save(tmp, fmt.upper())

    ok, detail, saved = compress_auto(tmp, output_path, mode, quality, opts, preferred=preferred)
    if os.path.exists(tmp):
        os.remove(tmp)
    return ok, detail, saved


# --------------------------------------------------------------------------
# Backend implementations
# --------------------------------------------------------------------------
def _pillow(input_path, output_path, mode, quality, opts) -> tuple[bool, str]:
    from PIL import Image

    img = Image.open(input_path)
    fmt = Path(output_path).suffix.lower().lstrip(".")
    quality = max(1, min(100, int(quality)))

    if fmt == "png":
        if mode == "lossless":
            out = img.copy()
            out.info = {}  # strip metadata
            out.save(output_path, "PNG", optimize=True)
            return True, "Pillow PNG (optimize)"
        # lossy: palette quantization (lossy)
        colors = max(2, round(quality / 100 * 255) + 1)
        if img.mode in ("RGBA", "LA", "P"):
            out = img.convert("RGBA").quantize(colors=colors)
        else:
            out = img.convert("RGB").quantize(colors=colors)
        out.save(output_path, "PNG", optimize=True)
        return True, f"Pillow PNG (quantize {colors})"

    if fmt in ("jpg", "jpeg"):
        rgb = img.convert("RGB")
        if mode == "lossless":
            rgb.save(output_path, "JPEG", quality=100, optimize=True, subsampling=0)
            return True, "Pillow JPEG (quality 100)"
        rgb.save(
            output_path,
            "JPEG",
            quality=quality,
            optimize=True,
            subsampling=0,
            progressive=bool(opts.get("progressive", False)),
        )
        return True, f"Pillow JPEG (q{quality})"

    if fmt == "webp":
        if mode == "lossless":
            img.save(output_path, "WEBP", lossless=True)
        else:
            img.save(output_path, "WEBP", quality=quality)
        return True, f"Pillow WebP ({mode})"

    if fmt == "tiff":
        comp = "deflate" if mode == "lossless" else "jpeg"
        img.save(output_path, "TIFF", compression=comp)
        return True, f"Pillow TIFF ({comp})"

    # bmp / gif: no real lossless recompression; pass through unchanged.
    img.save(output_path, fmt.upper())
    return True, f"Pillow copy ({fmt})"


def _oxipng(input_path, output_path, opts) -> tuple[bool, str]:
    tool = find_tool("oxipng")
    if not tool:
        return False, "oxipng not found"
    level = int(opts.get("level", 2))
    level = max(0, min(6, level))
    tmp = str(output_path) + ".oxipng.tmp"
    shutil.copy(input_path, tmp)
    args = [tool, "-o", str(level), "--strip", opts.get("strip", "safe")]
    if opts.get("interlace"):
        args += ["--interlace", "adam7"]
    args.append(tmp)
    try:
        subprocess.run(args, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        return False, f"oxipng error: {exc.stderr[:200]}"
    os.replace(tmp, output_path)
    return True, f"oxipng -o{level}"


def _optipng(input_path, output_path, opts) -> tuple[bool, str]:
    tool = find_tool("optipng")
    if not tool:
        return False, "optipng not found"
    level = int(opts.get("level", 2))
    level = max(0, min(7, level))
    args = [
        tool,
        f"-o{level}",
        "-strip", opts.get("strip", "all"),
        "-out", str(output_path),
        "-quiet",
        str(input_path),
    ]
    try:
        subprocess.run(args, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        return False, f"optipng error: {exc.stderr[:200]}"
    return True, f"optipng -o{level}"


def _mozjpeg(input_path, output_path, mode, quality, opts) -> tuple[bool, str]:
    from PIL import Image

    if mode == "lossless":
        jt = find_tool("jpegtran")
        if not jt:
            return False, "jpegtran not found"
        args = [jt, "-optimize", "-copy", "none", "-outfile", str(output_path), str(input_path)]
        if opts.get("progressive"):
            args.insert(1, "-progressive")
        try:
            subprocess.run(args, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            return False, f"jpegtran error: {exc.stderr[:200]}"
        return True, "mozjpeg jpegtran -optimize"

    # lossy: decode to PPM via Pillow, pipe to cjpeg.
    cj = find_tool("cjpeg")
    if not cj:
        return False, "cjpeg not found"
    img = Image.open(input_path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PPM")
    cargs = [
        cj, "-quality", str(max(1, min(100, int(quality)))), "-optimize",
        *(["-progressive"] if opts.get("progressive") else []),
        *(["-arithmetic"] if opts.get("arithmetic") else []),
    ]
    try:
        proc = subprocess.run(cargs, input=buf.getvalue(), capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        return False, f"cjpeg error: {exc.stderr[:200]}"
    Path(output_path).write_bytes(proc.stdout)
    return True, f"mozjpeg cjpeg -quality {quality}"
