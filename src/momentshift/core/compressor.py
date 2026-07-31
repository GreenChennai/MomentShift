"""图像压缩引擎（v0.4.0 重构：内置 oxipng + imagecodecs，删除 OptiPNG/MozJPEG）。

后端：
- ``pillow``   : 始终可用（已安装依赖）。基础兜底。
- ``oxipng``   : 内置在 resources/oxipng.exe（v10.1.1）。PNG 无损压缩。
- ``imagecodecs`` : Python 库（2026.6.26）。JPG/PNG/WebP 等高质量压缩。
- ``ffmpeg``   : 转换后默认压缩（无损重新编码）。
"""

from __future__ import annotations

import io, os, shutil, subprocess
from pathlib import Path
from typing import Optional

from .logger import get_logger

log = get_logger("compressor")

WIN_SILENT = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 格式集合
_PNG  = {"png"}
_JPG  = {"jpg", "jpeg"}
_WEBP = {"webp"}
_RASTER = {"bmp", "tiff", "gif"}


# -- 内置工具路径 --------------------------------------------------------
def _bundled_oxipng() -> Optional[str]:
    """返回内置 oxipng.exe 的路径（v0.4.0 内置）。"""
    import sys
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS) / "momentshift" / "resources"
    else:
        base = Path(__file__).parent.parent / "resources"
    p = base / "oxipng.exe"
    return str(p) if p.is_file() else None


# -- 后端发现 ------------------------------------------------------------
def available_backends() -> dict[str, dict]:
    """返回可用后端映射。"""
    out: dict[str, dict] = {}

    # Pillow（始终可用）
    out["pillow"] = {
        "name": "Pillow", "formats": _PNG | _JPG | _WEBP | _RASTER,
        "lossless": True, "lossy": True, "builtin": True,
    }

    # oxipng（v0.4.0 内置）
    ox = _bundled_oxipng()
    if ox:
        out["oxipng"] = {
            "name": "oxipng", "formats": _PNG,
            "lossless": True, "lossy": False, "builtin": True, "path": ox,
        }

    # imagecodecs（v0.4.0）
    try:
        import imagecodecs
        ok = True
    except ImportError:
        ok = False
    if ok:
        out["imagecodecs"] = {
            "name": "imagecodecs", "formats": _PNG | _JPG | _WEBP,
            "lossless": True, "lossy": True, "builtin": True, "path": None,
        }

    return out


def best_backend(fmt: str, mode: str, preferred: Optional[str] = None) -> Optional[str]:
    """选择最佳可用后端。"""
    fmt = fmt.lower().lstrip(".")
    backs = available_backends()

    def supports(bid: str) -> bool:
        b = backs.get(bid)
        if not b: return False
        if fmt not in b["formats"]: return False
        if mode == "lossless" and not b["lossless"]: return False
        if mode == "lossy" and not b["lossy"]: return False
        return True

    if preferred and supports(preferred):
        return preferred

    order = {
        ("png", "lossless"): ["oxipng", "imagecodecs", "pillow"],
        ("png", "lossy"): ["imagecodecs", "pillow"],
        ("jpg", "lossless"): ["imagecodecs", "pillow"],
        ("jpg", "lossy"): ["imagecodecs", "pillow"],
        ("webp", "lossless"): ["imagecodecs", "pillow"],
        ("webp", "lossy"): ["imagecodecs", "pillow"],
    }
    for bid in order.get((fmt, mode), ["pillow"]):
        if supports(bid):
            return bid
    return "pillow"


# -- 压缩执行 ------------------------------------------------------------
def compress(src: str, dst: str, fmt: str, quality: int = 95,
             backend: Optional[str] = None, opts: Optional[dict] = None) -> bool:
    """压缩单张图片。返回 True 表示成功。"""
    fmt = fmt.lower().lstrip(".")
    if backend is None:
        backend = best_backend(fmt, "lossless" if quality >= 95 else "lossy")

    handlers = {
        "oxipng": _compress_oxipng,
        "imagecodecs": _compress_imagecodecs,
        "pillow": _compress_pillow,
    }

    if backend not in ("oxipng", "imagecodecs", "pillow"):
        backend = "pillow"

    try:
        return handlers[backend](src, dst, fmt, quality, opts or {})
    except Exception:
        log.exception("compress(%s) failed, falling back to pillow", backend)
        try:
            return _compress_pillow(src, dst, fmt, quality, opts or {})
        except Exception:
            log.exception("pillow fallback failed")
            return False


# -- 各后端实现 ----------------------------------------------------------
def _compress_oxipng(src: str, dst: str, fmt: str, quality: int, opts: dict) -> bool:
    """oxipng 无损压缩（v0.4.0：内置）。"""
    ox = _bundled_oxipng()
    if not ox:
        return False
    tmp = dst + ".oxi.tmp"
    if src != tmp:
        shutil.copy2(src, tmp)
    args = [ox, "--quiet"]
    level = int(opts.get("level", 3))
    if 0 <= level <= 6:
        args.append(f"--opt={level}")
    if opts.get("interlace"):
        args.append("--interlace=1")
    if opts.get("strip") == "all":
        args.append("--strip=all")
    elif opts.get("strip", "safe") == "safe":
        args.append("--strip=safe")
    args.append(tmp)
    try:
        subprocess.run(args, check=True, creationflags=WIN_SILENT, timeout=120)
        if tmp != dst:
            shutil.move(tmp, dst)
        return True
    except Exception:
        log.exception("oxipng failed")
        return False


def _compress_imagecodecs(src: str, dst: str, fmt: str, quality: int, opts: dict) -> bool:
    """imagecodecs 压缩（v0.6.4：jpg→jpeg 规范化）。"""
    try:
        from PIL import Image
        import numpy
        from imagecodecs import imwrite
        img = Image.open(src)
        arr = numpy.array(img)
        fmt_norm = (fmt or "jpg").lower().lstrip(".")
        if fmt_norm == "jpg": fmt_norm = "jpeg"
        # imagecodecs 2026.6.26 无 jpeg8_encode，fallback to pillow
        kw = {}
        if fmt_norm == "jpeg":
            kw["quality"] = quality
        try:
            imwrite(dst, arr, codec=fmt_norm, **kw)
        except Exception:
            log.warning("imagecodecs '%s' failed, falling back to pillow", fmt_norm)
            _compress_pillow(src, dst, fmt, quality, opts)
        return True
    except Exception:
        log.exception("imagecodecs failed")
        return False


def _compress_pillow(src: str, dst: str, fmt: str, quality: int, opts: dict) -> bool:
    """Pillow 兜底压缩。"""
    try:
        from PIL import Image
        img = Image.open(src)
        save_fmt = fmt.upper()
        if save_fmt == "JPG": save_fmt = "JPEG"
        if save_fmt not in ("PNG", "JPEG", "WEBP", "BMP", "TIFF"): save_fmt = "PNG"
        kw = {}
        if save_fmt == "JPEG":
            kw["quality"] = quality
            kw["optimize"] = True
        elif save_fmt == "PNG":
            kw["optimize"] = True
        elif save_fmt == "WEBP":
            kw["quality"] = quality
        img.save(dst, format=save_fmt, **kw)
        return True
    except Exception:
        log.exception("pillow failed")
        return False
