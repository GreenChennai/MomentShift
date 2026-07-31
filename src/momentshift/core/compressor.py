"""图像压缩引擎（v0.7.0：内置 oxipng + jpegoptim，移除 imagecodecs）。

后端
----
- ``pillow``    : 始终可用（Pillow 依赖）。通用兜底，覆盖 PNG/JPEG/WebP/BMP/TIFF。
- ``oxipng``    : 内置 ``resources/oxipng.exe``（v10.1.1）。PNG 无损压缩。
- ``jpegoptim`` : 内置 ``resources/jpegoptim.exe``（v1.5.6）。JPG/JPEG 压缩。

默认路由（v0.7.0 需求 3）
------------------------
- ``png``        → oxipng
- ``jpg/jpeg``   → jpegoptim
- 其他图片类型   → pillow

当 oxipng / jpegoptim 的二进制缺失或调用失败时，写一条日志提示并自动切换到
Pillow 继续任务（需求 6），不会让任务失败。

参数键约定
----------
同一个 ``opts`` 字典同时承载三种后端的参数，按前缀区分，互不冲突：

- oxipng    : ``level`` ``interlace`` ``strip`` ``filter`` ``zc`` ``alpha``（历史键，无前缀）
- jpegoptim : ``jo_*``
- pillow    : ``pil_*``
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .logger import get_logger

log = get_logger("compressor")

WIN_SILENT = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 格式集合
_PNG = {"png"}
_JPG = {"jpg", "jpeg"}
_WEBP = {"webp"}
_RASTER = {"bmp", "tiff", "tif", "gif"}
IMAGE_EXTS = _PNG | _JPG | _WEBP | _RASTER


# =============================================================================
# 参数规格（供 UI 生成控件 / 校验）
# =============================================================================
#: jpegoptim 可设置参数（依据 jpegoptim v1.5.6 ``--help``）。
JPEGOPTIM_PARAMS: dict[str, dict] = {
    "jo_mode": {
        "type": "choice", "values": ["lossless", "lossy"], "default": "lossless",
        "desc": "lossless=纯无损重排哈夫曼表；lossy=按最高质量上限重编码(-m)",
    },
    "jo_max": {
        "type": "int", "min": 0, "max": 100, "default": 85,
        "desc": "有损模式下的最高质量因子 (-m)，仅 jo_mode=lossy 生效",
    },
    "jo_strip": {
        "type": "choice", "values": ["none", "all", "exif", "icc", "com", "meta"],
        "default": "none",
        "desc": "元数据剥离：none 全保留 / all 全剥离 / meta 剥离 EXIF+IPTC+XMP+注释（保留 ICC）",
    },
    "jo_progressive": {
        "type": "choice", "values": ["keep", "auto", "progressive", "normal"],
        "default": "auto",
        "desc": "keep 不改动 / auto 自动选更小者 / progressive 强制渐进 / normal 强制基线",
    },
    "jo_threshold": {
        "type": "int", "min": 0, "max": 99, "default": 0,
        "desc": "增益低于该百分比时保留原文件 (-T)，0 表示不设阈值",
    },
    "jo_preserve": {
        "type": "bool", "default": True,
        "desc": "保留原文件时间戳 (-p)",
    },
    "jo_retry": {
        "type": "bool", "default": False,
        "desc": "递归重试直到文件大小不再变化 (-r)，更慢但压得更狠",
    },
}

#: Pillow 可设置参数（v0.7.0 需求 4）。
PILLOW_PARAMS: dict[str, dict] = {
    "pil_quality": {
        "type": "int", "min": 0, "max": 95, "default": 95,
        "desc": "压缩质量，0=最差 95=最佳（Pillow 官方不建议超过 95）",
    },
    "pil_optimize": {
        "type": "bool", "default": True,
        "desc": "开启额外优化（多花一轮编码换更小体积）",
    },
    "pil_progressive": {
        "type": "bool", "default": True,
        "desc": "生成渐进式 JPEG",
    },
    "pil_subsampling": {
        "type": "choice", "values": ["4:4:4", "4:2:2", "4:2:0"], "default": "4:4:4",
        "desc": "色度采样：4:4:4 最佳画质 / 4:2:2 中等 / 4:2:0 最大压缩",
    },
}

#: oxipng 可设置参数（沿用 v0.6.8 键名，保持配置向后兼容）。
OXIPNG_PARAMS: dict[str, dict] = {
    "level": {"type": "int", "min": 0, "max": 6, "default": 3, "desc": "优化等级 (--opt)"},
    "interlace": {"type": "bool", "default": False, "desc": "生成隔行 PNG"},
    "strip": {"type": "choice", "values": ["none", "safe", "all"], "default": "safe",
              "desc": "元数据剥离"},
    "filter": {"type": "int", "min": 0, "max": 5, "default": None, "desc": "行过滤器"},
    "zc": {"type": "int", "min": 1, "max": 9, "default": None, "desc": "zlib 压缩级别"},
    "alpha": {"type": "bool", "default": False, "desc": "alpha 通道优化"},
}


def param_defaults(backend: str) -> dict:
    """返回某后端的参数默认值字典。"""
    table = {
        "jpegoptim": JPEGOPTIM_PARAMS,
        "pillow": PILLOW_PARAMS,
        "oxipng": OXIPNG_PARAMS,
    }.get(backend, {})
    return {k: v["default"] for k, v in table.items() if v.get("default") is not None}


# =============================================================================
# 工具路径解析
# =============================================================================
def _resources_dir() -> Path:
    """内置资源目录（打包后指向 _MEIPASS）。

    PyInstaller >= 6 的 onedir 构建会把包收集到 ``_internal/`` 子目录，
    因此同时探测 ``_MEIPASS/_internal/momentshift/resources`` 与旧式
    ``_MEIPASS/momentshift/resources`` 两种布局，确保内置 oxipng /
    jpegoptim 在发布版中可被正确定位。
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)
        candidates = [
            base / "_internal" / "momentshift" / "resources",
            base / "momentshift" / "resources",
        ]
        for c in candidates:
            if c.is_dir():
                return c
        return candidates[0]
    return Path(__file__).parent.parent / "resources"


def tools_dir() -> Path:
    """外部工具目录（exe 同级 ``tools/``），用于用户手动下载的补充工具。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parents[3]
    d = base / "tools"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _bundled(exe_name: str) -> Optional[str]:
    """在内置资源目录中查找可执行文件。"""
    p = _resources_dir() / exe_name
    return str(p) if p.is_file() else None


def _bundled_oxipng() -> Optional[str]:
    return _bundled("oxipng.exe")


def _bundled_jpegoptim() -> Optional[str]:
    return _bundled("jpegoptim.exe")


def find_tool(name: str) -> Optional[str]:
    """按 内置资源 → tools/ 目录 → 系统 PATH 的顺序查找工具。

    ``name`` 可带或不带 ``.exe``。找不到返回 ``None``。
    """
    stem = name[:-4] if name.lower().endswith(".exe") else name
    exe = f"{stem}.exe"

    p = _bundled(exe)
    if p:
        return p

    t = tools_dir() / exe
    if t.is_file():
        return str(t)

    return shutil.which(stem)


# =============================================================================
# 后端发现与路由
# =============================================================================
def available_backends() -> dict[str, dict]:
    """返回当前可用的压缩后端映射。"""
    out: dict[str, dict] = {
        "pillow": {
            "name": "Pillow",
            "formats": _PNG | _JPG | _WEBP | _RASTER,
            "lossless": True, "lossy": True, "builtin": True, "path": None,
        },
    }

    ox = _bundled_oxipng()
    if ox:
        out["oxipng"] = {
            "name": "oxipng", "formats": set(_PNG),
            "lossless": True, "lossy": False, "builtin": True, "path": ox,
        }

    jo = _bundled_jpegoptim()
    if jo:
        out["jpegoptim"] = {
            "name": "jpegoptim", "formats": set(_JPG),
            "lossless": True, "lossy": True, "builtin": True, "path": jo,
        }

    return out


def default_backend(fmt: str) -> str:
    """v0.7.0 默认路由：png→oxipng，jpg/jpeg→jpegoptim，其他→pillow。

    对应后端不可用时回落到 ``pillow``（调用方负责写日志提示）。
    """
    f = (fmt or "").lower().lstrip(".")
    backs = available_backends()
    if f in _PNG and "oxipng" in backs:
        return "oxipng"
    if f in _JPG and "jpegoptim" in backs:
        return "jpegoptim"
    return "pillow"


def best_backend(fmt: str, mode: str = "lossless",
                 preferred: Optional[str] = None) -> str:
    """在满足 ``mode`` 的前提下选择后端；``preferred`` 优先。"""
    f = (fmt or "").lower().lstrip(".")
    backs = available_backends()

    def supports(bid: str) -> bool:
        b = backs.get(bid)
        if not b:
            return False
        if f not in b["formats"]:
            return False
        if mode == "lossless" and not b["lossless"]:
            return False
        if mode == "lossy" and not b["lossy"]:
            return False
        return True

    if preferred and supports(preferred):
        return preferred

    routed = default_backend(f)
    if supports(routed):
        return routed
    return "pillow"


def _fallback_to_pillow(backend: str, src: str) -> str:
    """检查后端二进制是否就位；缺失则提示并切到 pillow（需求 6）。"""
    if backend == "oxipng" and not _bundled_oxipng():
        log.warning("oxipng 不可用（未找到内置二进制），已自动切换到 Pillow：%s",
                    Path(src).name)
        return "pillow"
    if backend == "jpegoptim" and not _bundled_jpegoptim():
        log.warning("jpegoptim 不可用（未找到内置二进制），已自动切换到 Pillow：%s",
                    Path(src).name)
        return "pillow"
    return backend


# =============================================================================
# 压缩执行
# =============================================================================
def compress(src: str, dst: str, fmt: str, quality: int = 95,
             backend: Optional[str] = None, opts: Optional[dict] = None) -> bool:
    """压缩单张图片，``src`` → ``dst``。返回 ``True`` 表示成功。"""
    fmt = (fmt or "").lower().lstrip(".")
    opts = dict(opts or {})

    if backend in (None, "", "auto"):
        backend = default_backend(fmt)
    if backend not in ("oxipng", "jpegoptim", "pillow"):
        backend = default_backend(fmt)

    # 后端与格式不匹配时纠正（例如对 webp 选了 oxipng）
    if backend == "oxipng" and fmt not in _PNG:
        backend = default_backend(fmt)
    elif backend == "jpegoptim" and fmt not in _JPG:
        backend = default_backend(fmt)

    backend = _fallback_to_pillow(backend, src)

    handlers = {
        "oxipng": _compress_oxipng,
        "jpegoptim": _compress_jpegoptim,
        "pillow": _compress_pillow,
    }

    try:
        ok = handlers[backend](src, dst, fmt, quality, opts)
        if not ok and backend != "pillow":
            log.warning("%s 压缩未成功，已自动切换到 Pillow：%s", backend, Path(src).name)
            return _compress_pillow(src, dst, fmt, quality, opts)
        return ok
    except Exception:
        log.exception("%s 压缩异常，已自动切换到 Pillow：%s", backend, Path(src).name)
        try:
            return _compress_pillow(src, dst, fmt, quality, opts)
        except Exception:
            log.exception("Pillow 兜底压缩同样失败：%s", Path(src).name)
            return False


# -- oxipng ------------------------------------------------------------------
def _compress_oxipng(src: str, dst: str, fmt: str, quality: int, opts: dict) -> bool:
    """oxipng PNG 无损压缩（内置二进制，原地优化临时副本）。"""
    ox = _bundled_oxipng()
    if not ox:
        return False

    tmp = dst + ".oxi.tmp"
    try:
        shutil.copy2(src, tmp)
    except Exception:
        log.exception("oxipng: 复制临时文件失败")
        return False

    args = [ox, "--quiet"]
    level = int(opts.get("level", 3) or 0)
    if 0 <= level <= 6:
        args.append(f"--opt={level}")
    if opts.get("interlace"):
        args.append("--interlace=1")
    strip_val = opts.get("strip", "safe")
    if strip_val in ("safe", "all"):
        args.append(f"--strip={strip_val}")
    filt = opts.get("filter")
    if filt is not None and 0 <= int(filt) <= 5:
        args.append(f"--filter={int(filt)}")
    zc = opts.get("zc")
    if zc is not None and 1 <= int(zc) <= 9:
        args.extend(["--zc", str(int(zc))])
    if opts.get("alpha"):
        args.append("--alpha")
    args.append(tmp)

    try:
        subprocess.run(args, check=True, creationflags=WIN_SILENT, timeout=180,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        shutil.move(tmp, dst)
        return True
    except Exception:
        log.exception("oxipng 执行失败")
        _rm(tmp)
        return False


# -- jpegoptim ---------------------------------------------------------------
def _compress_jpegoptim(src: str, dst: str, fmt: str, quality: int, opts: dict) -> bool:
    """jpegoptim JPEG 压缩（内置二进制，原地优化临时副本）。

    jpegoptim 默认覆盖输入文件，因此先把源复制成临时文件再原地优化，
    最后移动到目标路径，避免污染源文件。
    """
    jo = _bundled_jpegoptim()
    if not jo:
        return False
    if fmt not in _JPG:
        return False

    tmp = dst + ".jo.tmp"
    try:
        shutil.copy2(src, tmp)
    except Exception:
        log.exception("jpegoptim: 复制临时文件失败")
        return False

    args = [jo, "--quiet"]

    mode = str(opts.get("jo_mode", "lossless")).lower()
    if mode == "lossy":
        jmax = opts.get("jo_max", opts.get("quality", quality))
        try:
            jmax = int(jmax)
        except (TypeError, ValueError):
            jmax = 85
        jmax = max(0, min(100, jmax))
        args.append(f"--max={jmax}")
        # 有损模式下即便体积没变小也要落盘，否则用户设定的质量上限不会生效
        args.append("--force")

    strip = str(opts.get("jo_strip", "none")).lower()
    if strip == "all":
        args.append("--strip-all")
    elif strip == "exif":
        args.append("--strip-exif")
    elif strip == "icc":
        args.append("--strip-icc")
    elif strip == "com":
        args.append("--strip-com")
    elif strip == "meta":
        args.extend(["--strip-exif", "--strip-iptc", "--strip-xmp", "--strip-com"])
    else:
        args.append("--strip-none")

    prog = str(opts.get("jo_progressive", "auto")).lower()
    if prog == "progressive":
        args.append("--all-progressive")
    elif prog == "normal":
        args.append("--all-normal")
    elif prog == "auto":
        args.append("--auto-mode")

    thr = opts.get("jo_threshold")
    try:
        thr = int(thr) if thr is not None else 0
    except (TypeError, ValueError):
        thr = 0
    if 0 < thr <= 99:
        args.append(f"--threshold={thr}")

    if opts.get("jo_preserve", True):
        args.append("--preserve")
    if opts.get("jo_retry"):
        args.append("--retry")

    args.append(tmp)

    try:
        subprocess.run(args, check=True, creationflags=WIN_SILENT, timeout=180,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if not os.path.isfile(tmp):
            return False
        shutil.move(tmp, dst)
        return True
    except Exception:
        log.exception("jpegoptim 执行失败")
        _rm(tmp)
        return False


# -- Pillow ------------------------------------------------------------------
_SUBSAMPLING_MAP = {"4:4:4": 0, "4:2:2": 1, "4:2:0": 2}


def _compress_pillow(src: str, dst: str, fmt: str, quality: int, opts: dict) -> bool:
    """Pillow 通用压缩（v0.7.0：quality / optimize / progressive / subsampling）。"""
    try:
        from PIL import Image
    except Exception:
        log.exception("Pillow 不可用")
        return False

    try:
        img = Image.open(src)
        img.load()

        save_fmt = (fmt or "").upper()
        if save_fmt in ("JPG", "JPEG"):
            save_fmt = "JPEG"
        elif save_fmt == "TIF":
            save_fmt = "TIFF"
        if save_fmt not in ("PNG", "JPEG", "WEBP", "BMP", "TIFF", "GIF"):
            save_fmt = (img.format or "PNG").upper()

        # quality：优先取 pil_quality，其次沿用调用方传入的 quality
        q = opts.get("pil_quality", opts.get("quality", quality))
        try:
            q = int(q)
        except (TypeError, ValueError):
            q = 95
        q = max(0, min(95, q))

        optimize = bool(opts.get("pil_optimize", True))
        progressive = bool(opts.get("pil_progressive", True))
        sub_key = str(opts.get("pil_subsampling", "4:4:4"))
        subsampling = _SUBSAMPLING_MAP.get(sub_key, 0)

        kw: dict = {}
        if save_fmt == "JPEG":
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            kw.update(quality=q, optimize=optimize,
                      progressive=progressive, subsampling=subsampling)
        elif save_fmt == "PNG":
            kw["optimize"] = optimize
            lvl = opts.get("level")
            if lvl is not None:
                kw["compress_level"] = max(0, min(9, int(lvl)))
        elif save_fmt == "WEBP":
            kw["quality"] = q
            if opts.get("lossless"):
                kw["lossless"] = True
        elif save_fmt == "TIFF":
            kw["compression"] = "tiff_lzw"
        elif save_fmt == "GIF":
            kw["optimize"] = optimize

        if save_fmt == "JPEG" and (optimize or progressive):
            # optimize/progressive 需要把整帧塞进一个块，Pillow 默认 MAXBLOCK
            # (64KB) 对大图或高噪声图不够，会抛 "broken data stream"。
            try:
                from PIL import ImageFile
                w, h = img.size
                ImageFile.MAXBLOCK = max(ImageFile.MAXBLOCK, w * h * 4, 1 << 20)
            except Exception:
                pass

        try:
            img.save(dst, format=save_fmt, **kw)
        except OSError:
            # 极端情况下仍然溢出：退掉 optimize/progressive 保证任务不失败
            if save_fmt == "JPEG" and (optimize or progressive):
                log.warning("Pillow: optimize/progressive 编码溢出，已降级重试：%s",
                            Path(src).name)
                kw["optimize"] = False
                kw["progressive"] = False
                _rm(dst)
                img.save(dst, format=save_fmt, **kw)
            else:
                raise
        return True
    except Exception:
        log.exception("Pillow 压缩失败：%s", Path(src).name)
        return False


def _rm(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


# =============================================================================
# 高层 API（供压缩模块 UI 使用）
# =============================================================================
def needs_conversion(src_ext: str, target_ext: str) -> bool:
    """判断压缩前是否需要先做格式转码。"""
    s = (src_ext or "").lower().lstrip(".")
    t = (target_ext or "").lower().lstrip(".")
    if not t or t in ("same", "none"):
        return False
    if s == t:
        return False
    if {s, t} == {"jpg", "jpeg"} or {s, t} == {"tif", "tiff"}:
        return False
    return True


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} GB"


def compress_auto(src: str, out: str, mode: str = "lossless", quality: int = 95,
                  opts: Optional[dict] = None,
                  preferred: Optional[str] = None) -> tuple[bool, str, int]:
    """压缩 ``src`` 到 ``out``，自动挑选后端。

    返回 ``(ok, detail, saved_bytes)``。压缩后反而变大时保留原文件内容，
    ``saved_bytes`` 记 0。
    """
    opts = dict(opts or {})
    fmt = Path(src).suffix.lower().lstrip(".")
    backend = best_backend(fmt, mode, preferred)
    backend = _fallback_to_pillow(backend, src)

    before = _size(src)
    tmp = f"{out}.cmp.tmp"
    try:
        ok = compress(src, tmp, fmt, quality, backend=backend, opts=opts)
        if not ok or not os.path.isfile(tmp):
            _rm(tmp)
            return False, f"{backend} 压缩失败", 0

        after = _size(tmp)
        if after >= before > 0:
            # 没压小就别换，直接把原文件放到目标位置
            _rm(tmp)
            if os.path.abspath(src) != os.path.abspath(out):
                shutil.copy2(src, out)
            return True, f"{backend}: {_human(before)}（无可压缩空间）", 0

        os.replace(tmp, out)
        saved = before - after
        pct = (saved / before * 100) if before else 0
        return True, f"{backend}: {_human(before)} → {_human(after)} (-{pct:.1f}%)", saved
    except Exception as exc:
        _rm(tmp)
        log.exception("compress_auto 失败：%s", Path(src).name)
        return False, str(exc), 0


def transcode_and_compress(src: str, out: str, target_fmt: str,
                           mode: str = "lossless", quality: int = 95,
                           opts: Optional[dict] = None,
                           preferred: Optional[str] = None) -> tuple[bool, str, int]:
    """先用 Pillow 把 ``src`` 转成 ``target_fmt``，再压缩到 ``out``。

    返回 ``(ok, detail, saved_bytes)``，``saved_bytes`` 相对原始文件计算。
    """
    opts = dict(opts or {})
    tf = (target_fmt or "").lower().lstrip(".")
    before = _size(src)
    stage = f"{out}.tc.tmp.{tf or 'png'}"

    try:
        from PIL import Image
        img = Image.open(src)
        img.load()

        save_fmt = "JPEG" if tf in _JPG else tf.upper()
        if save_fmt == "TIF":
            save_fmt = "TIFF"
        if save_fmt == "JPEG" and img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")

        kw: dict = {}
        if save_fmt == "JPEG":
            kw.update(quality=max(0, min(95, int(quality))), optimize=True)
        elif save_fmt == "WEBP":
            kw["quality"] = max(0, min(100, int(quality)))
        img.save(stage, format=save_fmt, **kw)
    except Exception as exc:
        _rm(stage)
        log.exception("转码失败：%s → %s", Path(src).name, tf)
        return False, f"转码失败: {exc}", 0

    ok, detail, _ = compress_auto(stage, out, mode, quality, opts, preferred)
    _rm(stage)
    if not ok:
        return False, detail, 0

    after = _size(out)
    saved = max(0, before - after)
    pct = (saved / before * 100) if before else 0
    return True, f"{tf}: {_human(before)} → {_human(after)} (-{pct:.1f}%)", saved
