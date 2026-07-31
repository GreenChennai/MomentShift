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
        ok = handlers[backend](src, dst, fmt, quality, opts or {})
        if not ok and backend != "pillow":
            log.warning("compress(%s) returned False, falling back to pillow", backend)
            return _compress_pillow(src, dst, fmt, quality, opts or {})
        return ok
    except Exception:
        log.exception("compress(%s) crashed, falling back to pillow", backend)
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
    # 压缩等级 0-6
    level = int(opts.get("level", 3))
    if 0 <= level <= 6:
        args.append(f"--opt={level}")
    if opts.get("interlace"):
        args.append("--interlace=1")
    # 元数据清除
    strip_val = opts.get("strip", "safe")
    if strip_val in ("safe", "all"):
        args.append(f"--strip={strip_val}")
    # v0.6.8：过滤器 0-5
    filt = opts.get("filter")
    if filt is not None and 0 <= int(filt) <= 5:
        args.append(f"--filter={int(filt)}")
    # v0.6.8：zlib 压缩级别 1-9
    zc = opts.get("zc")
    if zc is not None and 1 <= int(zc) <= 9:
        args.extend(["--zc", str(int(zc))])
    # v0.6.8：alpha 优化
    if opts.get("alpha"):
        args.append("--alpha")
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
    """v0.6.8：imagecodecs 压缩（逐格式专用编码器 + 高级参数）。"""
    try:
        from PIL import Image
        import numpy
        img = Image.open(src)
        arr = numpy.array(img)
        fmt_norm = (fmt or "jpg").lower().lstrip(".")
        if fmt_norm == "jpg":
            fmt_norm = "jpeg"

        if fmt_norm == "jpeg":
            return _ic_jpeg(dst, arr, quality, opts)
        elif fmt_norm == "png":
            return _ic_png(dst, arr, quality, opts)
        elif fmt_norm == "webp":
            return _ic_webp(dst, arr, quality, opts)
        else:
            # bmp/tiff: fallback to pillow directly
            return _compress_pillow(src, dst, fmt, quality, opts)
    except Exception:
        log.exception("imagecodecs failed")
        return False


def _ic_jpeg(dst: str, arr, quality: int, opts: dict) -> bool:
    """v0.6.9：jpeg_encode 返回 bytes，手动写盘。"""
    from imagecodecs import jpeg_encode
    kw = {"level": quality}  # imagecodecs 用 level 不是 quality
    if opts.get("subsampling"):
        kw["subsampling"] = opts["subsampling"]
    if opts.get("optimize"):
        kw["optimize"] = True
    if opts.get("smoothing"):
        kw["smoothing"] = True
    if opts.get("progressive"):
        kw["lossless"] = False  # JPEG-lossless mode if True, else standard
        # progressive via libjpeg-turbo: standard DCT
    try:
        data = jpeg_encode(arr, **kw)
        with open(dst, "wb") as f:
            f.write(data)
        return True
    except Exception:
        log.warning("jpeg_encode failed, falling back to pillow")
        return False


def _ic_png(dst: str, arr, quality: int, opts: dict) -> bool:
    from imagecodecs import spng_encode
    kw = {"level": opts.get("level", 6)}
    if opts.get("filter") is not None:
        kw["filter"] = int(opts["filter"])
    try:
        data = spng_encode(arr, **kw)
        with open(dst, "wb") as f:
            f.write(data)
        return True
    except Exception:
        log.warning("spng_encode failed, falling back to pillow")
        return False


def _ic_webp(dst: str, arr, quality: int, opts: dict) -> bool:
    from imagecodecs import webp_encode
    kw = {"level": quality}
    if opts.get("lossless"):
        kw["lossless"] = True
    try:
        data = webp_encode(arr, **kw)
        with open(dst, "wb") as f:
            f.write(data)
        return True
    except Exception:
        log.warning("webp_encode failed, falling back to pillow")
        return False


def _compress_pillow(src: str, dst: str, fmt: str, quality: int, opts: dict) -> bool:
    """v0.6.9：Pillow 兜底压缩（支持高级参数）。"""
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
            if opts.get("progressive"):
                kw["progressive"] = True
            sub = opts.get("subsampling", "4:2:0")
            if sub == "4:4:4": kw["subsampling"] = 0
            elif sub == "4:2:2": kw["subsampling"] = 1
            else: kw["subsampling"] = 2
        elif save_fmt == "PNG":
            kw["optimize"] = True
            if opts.get("level") is not None:
                kw["compress_level"] = int(opts["level"])
            if opts.get("filter") is not None:
                kw["filter"] = int(opts["filter"])
        elif save_fmt == "WEBP":
            kw["quality"] = quality
            if opts.get("lossless"):
                kw["lossless"] = True
        img.save(dst, format=save_fmt, **kw)
        return True
    except Exception:
        log.exception("pillow failed")
        return False
