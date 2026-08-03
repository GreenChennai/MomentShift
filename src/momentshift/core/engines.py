"""超分辨率 / 插帧引擎注册表。

职责边界：
- 做：维护引擎注册表与参数 schema、有界深度递归探测可执行文件、拼装命令行。
- 不做：不下载引擎（交给 core/engine_download）；不渲染设置行（交给 gui/engine_card）。

依赖：core/config、core/ffmpeg、core/logger、core/platform；被依赖：gui/engine_card、gui/upscale_interface、quick_runner。

本模块把「放大」从只支持 Real-ESRGAN 扩展为一张**可插拔的引擎表**：
新增引擎只需要往表里加一条记录，界面与命令行拼装都会自动跟上。

设计要点
--------
- **引擎不内置**。每个引擎在软件根目录 ``tools/<engine-id>/`` 下有自己的文件夹，
  用户自行前往官网下载解压后放入。软件只负责「检测 → 生成命令行 → 执行」。
- 解压出来的目录结构千奇百怪（很多 release zip 会多套一层同名目录），所以
  :func:`find_engine` 做**有界深度递归**（根目录 + 2 层子目录）来找可执行文件。
- 每个引擎自带一份 **参数 schema**（:class:`Param` 列表），UI 侧照着 schema
  动态生成设置行，核心侧照着 schema 拼命令行 —— 新增引擎只需要加一条记录。
- 算法 (algorithm) 与引擎 (engine) 是多对一：例如 Anime4KCPP 同时实现
  Anime4K 与 ACNet，通过 ``acnet`` 开关切换。

纯标准库，无 pip 依赖。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import tools_dir
from .logger import get_logger
from .platform import popen_silent, run_silent

log = get_logger("engines")


# ==========================================================================
# 数据结构定义
# ==========================================================================
@dataclass(frozen=True)
class Param:
    """一条引擎设置项。

    ``key``     内部键，存进设置字典（也是 i18n 键 ``engine.param.<key>``）
    ``kind``    "choice" | "int" | "float" | "bool"
    ``flag``    命令行开关；空字符串表示该项不直接映射为命令行参数
    ``style``   "value"（``flag value``）| "switch"（只在真值时追加 ``flag``）
    ``choices`` kind=="choice" 时的候选 ``((value, label), ...)``；
                label 以 ``@`` 开头表示这是 i18n 键，UI 需 ``tr()``
    """

    key: str
    kind: str
    default: Any
    flag: str = ""
    style: str = "value"
    choices: tuple[tuple[Any, str], ...] = ()
    minimum: float = 0
    maximum: float = 100
    step: float = 1
    suffix: str = ""

    @property
    def label_key(self) -> str:
        return f"engine.param.{self.key}"


@dataclass(frozen=True)
class Engine:
    """一个可执行的超分 / 插帧引擎。"""

    eid: str  # tools/<eid>/ 文件夹名，也是内部 id
    name: str  # 显示名
    algos: tuple[str, ...]  # 实现的算法（展示用）
    category: str  # "sr" 超分 | "interp" 插帧
    exe_names: tuple[str, ...]  # 可执行文件候选名
    homepage: str  # 官方下载页
    params: tuple[Param, ...] = ()
    model_flag: str = ""  # 模型参数的命令行开关（-m / --model_dir）
    model_style: str = "dir"  # "dir"=值是子目录名 | "name"=值是模型名
    models_root: str = ""  # model_style=="name" 时模型目录的子路径
    legacy_dirs: tuple[str, ...] = ()  # 兼容旧版本用过的文件夹名
    supports_dir: bool = True  # 能否一次处理整个帧目录
    cli: bool = True  # False = 无命令行，仅提示（如 RTX VSR）
    interp_count_flag: str = "-n"  # 插帧：目标帧数开关
    downloadable: bool = False  # 是否支持「一键下载引擎与模型」
    download_sources: tuple[tuple[str, str], ...] = ()  # 优先级下载源 (kind, value)
    download_reason_key: str = ""  # 不可下载时的原因 i18n 键

    @property
    def desc_key(self) -> str:
        return f"engine.desc.{self.eid}"

    @property
    def is_interp(self) -> bool:
        return self.category == "interp"


# --------------------------------------------------------------------------
# 复用的公共参数
# --------------------------------------------------------------------------
def _p_tile(default: int = 0) -> Param:
    return Param(
        "tile",
        "choice",
        default,
        "-t",
        choices=(
            (0, "@engine.opt.auto"),
            (32, "32"),
            (64, "64"),
            (128, "128"),
            (256, "256"),
            (400, "400"),
            (512, "512"),
        ),
    )


def _p_gpu() -> Param:
    return Param(
        "gpu",
        "choice",
        "auto",
        "-g",
        choices=(
            ("auto", "@engine.opt.auto"),
            ("cpu", "@engine.opt.cpu"),
            ("0", "GPU 0"),
            ("1", "GPU 1"),
            ("2", "GPU 2"),
        ),
    )


def _p_tta() -> Param:
    return Param("tta", "bool", False, "-x", style="switch")


def _p_jobs() -> Param:
    return Param(
        "jobs",
        "choice",
        "",
        "-j",
        choices=(
            ("", "@engine.opt.auto"),
            ("1:1:1", "1:1:1"),
            ("1:2:2", "1:2:2"),
            ("2:2:2", "2:2:2"),
            ("1:4:4", "1:4:4"),
        ),
    )


def _p_noise(default: int, lo: int = -1, hi: int = 3) -> Param:
    labels = {-1: "@engine.opt.noise_off", 0: "0", 1: "1", 2: "2", 3: "3"}
    ch = tuple((v, labels.get(v, str(v))) for v in range(lo, hi + 1))
    return Param("noise", "choice", default, "-n", choices=ch)


def _p_scale(values: Sequence[int], default: int) -> Param:
    return Param("scale", "choice", default, "-s", choices=tuple((v, f"{v}x") for v in values))


def _p_multiplier() -> Param:
    """插帧倍率（不直接映射命令行，由帧管线换算成目标帧数）。"""
    return Param(
        "multiplier",
        "choice",
        2,
        "",
        choices=(
            (2, "2x"),
            (3, "3x"),
            (4, "4x"),
            (8, "8x"),
        ),
    )


# ==========================================================================
# 引擎注册表
# ==========================================================================
ENGINES: tuple[Engine, ...] = (
    # ---------------- 超分辨率 ----------------
    Engine(
        eid="realesrgan-ncnn-vulkan",
        name="RealESRGAN-NCNN-Vulkan",
        algos=("Real-ESRGAN",),
        category="sr",
        exe_names=("realesrgan-ncnn-vulkan.exe", "realesrgan-ncnn-vulkan"),
        homepage="https://github.com/xinntao/Real-ESRGAN/releases",
        legacy_dirs=("realesrgan",),
        downloadable=True,
        download_sources=(("gh", "xinntao/Real-ESRGAN|windows.zip"),),
        model_flag="-n",
        model_style="name",
        models_root="models",
        params=(
            Param(
                "model",
                "choice",
                "realesrgan-x4plus",
                "-n",
                choices=(
                    ("realesrgan-x4plus", "Real-ESRGAN x4+"),
                    ("realesrgan-x4plus-anime", "Real-ESRGAN x4+ Anime"),
                    ("realesrnet-x4plus", "Real-ESRNet x4+"),
                    ("realesr-animevideov3", "AnimeVideo v3"),
                ),
            ),
            _p_scale((2, 3, 4), 4),
            _p_tile(),
            _p_gpu(),
            _p_tta(),
        ),
    ),
    Engine(
        eid="waifu2x-ncnn-vulkan",
        name="Waifu2x-ncnn-vulkan",
        algos=("Waifu2x",),
        category="sr",
        exe_names=("waifu2x-ncnn-vulkan.exe", "waifu2x-ncnn-vulkan"),
        homepage="https://github.com/nihui/waifu2x-ncnn-vulkan/releases",
        downloadable=True,
        download_sources=(("gh", "nihui/waifu2x-ncnn-vulkan|windows.zip"),),
        model_flag="-m",
        model_style="dir",
        params=(
            Param(
                "model",
                "choice",
                "models-cunet",
                "-m",
                choices=(
                    ("models-cunet", "CUnet (@engine.opt.balanced)"),
                    ("models-upconv_7_anime_style_art_rgb", "UpConv7 Anime"),
                    ("models-upconv_7_photo", "UpConv7 Photo"),
                ),
            ),
            _p_noise(0, -1, 3),
            _p_scale((1, 2, 4, 8, 16, 32), 2),
            _p_tile(),
            _p_gpu(),
            _p_jobs(),
            _p_tta(),
        ),
    ),
    Engine(
        eid="waifu2x-caffe",
        name="Waifu2x-caffe",
        algos=("Waifu2x",),
        category="sr",
        exe_names=("waifu2x-caffe-cui.exe", "waifu2x-caffe.exe"),
        homepage="https://github.com/lltcggie/waifu2x-caffe/releases",
        downloadable=True,
        download_sources=(("gh", "lltcggie/waifu2x-caffe|waifu2x-caffe"),),
        model_flag="--model_dir",
        model_style="dir",
        params=(
            Param(
                "model",
                "choice",
                "models/cunet",
                "--model_dir",
                choices=(
                    ("models/cunet", "CUnet"),
                    ("models/upconv_7_anime_style_art_rgb", "UpConv7 Anime"),
                    ("models/upconv_7_photo", "UpConv7 Photo"),
                    ("models/anime_style_art_rgb", "Anime Style Art RGB"),
                    ("models/photo", "Photo"),
                ),
            ),
            Param(
                "mode",
                "choice",
                "noise_scale",
                "-m",
                choices=(
                    ("noise_scale", "@engine.opt.noise_scale"),
                    ("scale", "@engine.opt.scale_only"),
                    ("noise", "@engine.opt.noise_only"),
                    ("auto_scale", "@engine.opt.auto"),
                ),
            ),
            Param("noise", "choice", 1, "-n", choices=((0, "0"), (1, "1"), (2, "2"), (3, "3"))),
            Param("scale", "choice", 2, "-s", choices=((1, "1x"), (2, "2x"), (3, "3x"), (4, "4x"))),
            Param(
                "process",
                "choice",
                "gpu",
                "-p",
                choices=(("gpu", "GPU"), ("cudnn", "cuDNN"), ("cpu", "CPU")),
            ),
            Param(
                "crop_size",
                "choice",
                128,
                "-c",
                choices=((64, "64"), (128, "128"), (256, "256"), (512, "512")),
            ),
            Param("tta", "bool", False, "-t", style="value"),
        ),
    ),
    Engine(
        eid="waifu2x-converter",
        name="Waifu2x-converter",
        algos=("Waifu2x",),
        category="sr",
        exe_names=(
            "waifu2x-converter-cpp.exe",
            "waifu2x-converter_x64.exe",
            "waifu2x-converter-cpp",
        ),
        homepage="https://github.com/DeadSix27/waifu2x-converter-cpp/releases",
        downloadable=True,
        download_sources=(("gh", "DeadSix27/waifu2x-converter-cpp|windows"),),
        model_flag="--model-dir",
        model_style="dir",
        params=(
            Param(
                "mode",
                "choice",
                "noise-scale",
                "-m",
                choices=(
                    ("noise-scale", "@engine.opt.noise_scale"),
                    ("scale", "@engine.opt.scale_only"),
                    ("noise", "@engine.opt.noise_only"),
                ),
            ),
            Param(
                "noise",
                "choice",
                1,
                "--noise-level",
                choices=((0, "0"), (1, "1"), (2, "2"), (3, "3")),
            ),
            Param("scale", "choice", 2, "--scale-ratio", choices=((2, "2x"), (3, "3x"), (4, "4x"))),
            Param(
                "block_size",
                "choice",
                0,
                "--block-size",
                choices=((0, "@engine.opt.auto"), (128, "128"), (256, "256"), (512, "512")),
            ),
            Param("gpu_off", "bool", False, "--disable-gpu", style="switch"),
        ),
    ),
    Engine(
        eid="srmd-ncnn-vulkan",
        name="SRMD-ncnn-vulkan",
        algos=("SRMD",),
        category="sr",
        exe_names=("srmd-ncnn-vulkan.exe", "srmd-ncnn-vulkan"),
        homepage="https://github.com/nihui/srmd-ncnn-vulkan/releases",
        downloadable=True,
        download_sources=(("gh", "nihui/srmd-ncnn-vulkan|windows.zip"),),
        model_flag="-m",
        model_style="dir",
        params=(
            Param("model", "choice", "models-srmd", "-m", choices=(("models-srmd", "SRMD"),)),
            Param(
                "noise",
                "choice",
                3,
                "-n",
                choices=tuple(
                    (v, "@engine.opt.noise_off" if v == -1 else str(v)) for v in range(-1, 11)
                ),
            ),
            _p_scale((2, 3, 4), 2),
            _p_tile(),
            _p_gpu(),
            _p_jobs(),
            _p_tta(),
        ),
    ),
    Engine(
        eid="srmd-cuda",
        name="SRMD-CUDA",
        algos=("SRMD",),
        category="sr",
        exe_names=("srmd-cuda.exe", "srmd_cuda.exe"),
        homepage="https://github.com/nihui/srmd-ncnn-vulkan",
        downloadable=False,
        download_reason_key="engine.reason.cuda",
        model_flag="-m",
        model_style="dir",
        params=(
            Param("model", "choice", "models-srmd", "-m", choices=(("models-srmd", "SRMD"),)),
            Param(
                "noise",
                "choice",
                3,
                "-n",
                choices=tuple(
                    (v, "@engine.opt.noise_off" if v == -1 else str(v)) for v in range(-1, 11)
                ),
            ),
            _p_scale((2, 3, 4), 2),
            _p_tile(),
        ),
    ),
    Engine(
        eid="realsr-ncnn-vulkan",
        name="RealSR-ncnn-vulkan",
        algos=("RealSR",),
        category="sr",
        exe_names=("realsr-ncnn-vulkan.exe", "realsr-ncnn-vulkan"),
        homepage="https://github.com/nihui/realsr-ncnn-vulkan/releases",
        downloadable=True,
        download_sources=(("gh", "nihui/realsr-ncnn-vulkan|windows.zip"),),
        model_flag="-m",
        model_style="dir",
        params=(
            Param(
                "model",
                "choice",
                "models-DF2K_JPEG",
                "-m",
                choices=(
                    ("models-DF2K_JPEG", "DF2K JPEG (@engine.opt.jpeg_friendly)"),
                    ("models-DF2K", "DF2K"),
                ),
            ),
            _p_scale((4,), 4),
            _p_tile(),
            _p_gpu(),
            _p_jobs(),
            _p_tta(),
        ),
    ),
    Engine(
        eid="realcugan-ncnn-vulkan",
        name="Real-CUGAN-ncnn-vulkan",
        algos=("Real-CUGAN",),
        category="sr",
        exe_names=("realcugan-ncnn-vulkan.exe", "realcugan-ncnn-vulkan"),
        homepage="https://github.com/nihui/realcugan-ncnn-vulkan/releases",
        downloadable=True,
        download_sources=(("gh", "nihui/realcugan-ncnn-vulkan|windows.zip"),),
        model_flag="-m",
        model_style="dir",
        params=(
            Param(
                "model",
                "choice",
                "models-se",
                "-m",
                choices=(
                    ("models-se", "SE (@engine.opt.standard)"),
                    ("models-pro", "Pro"),
                    ("models-nose", "No-Denoise"),
                ),
            ),
            _p_noise(-1, -1, 3),
            _p_scale((1, 2, 3, 4), 2),
            _p_tile(),
            Param(
                "syncgap",
                "choice",
                3,
                "-c",
                choices=((0, "@engine.opt.syncgap_off"), (1, "1"), (2, "2"), (3, "3")),
            ),
            _p_gpu(),
            _p_jobs(),
            _p_tta(),
        ),
    ),
    Engine(
        eid="anime4kcpp",
        name="Anime4KCPP",
        algos=("Anime4K", "ACNet"),
        category="sr",
        exe_names=("Anime4KCPP_CLI.exe", "ac_cli.exe", "Anime4KCPP_CLI"),
        homepage="https://github.com/TianZerL/Anime4KCPP/releases",
        downloadable=True,
        download_sources=(("gh", "TianZerL/Anime4KCPP|Windows"),),
        params=(
            Param("acnet", "bool", True, "-C", style="switch"),
            Param(
                "zoom",
                "choice",
                2.0,
                "-z",
                choices=((1.0, "1x"), (2.0, "2x"), (3.0, "3x"), (4.0, "4x")),
            ),
            Param("hdn", "bool", False, "-H", style="switch"),
            Param("hdn_level", "choice", 1, "-L", choices=((1, "1"), (2, "2"), (3, "3"))),
            Param("passes", "choice", 2, "-p", choices=((1, "1"), (2, "2"), (3, "3"), (4, "4"))),
            Param(
                "push_color", "choice", 2, "-n", choices=((1, "1"), (2, "2"), (3, "3"), (4, "4"))
            ),
            Param("strength_color", "float", 0.3, "-c", minimum=0.0, maximum=1.0, step=0.1),
            Param("strength_gradient", "float", 1.0, "-g", minimum=0.0, maximum=1.0, step=0.1),
            Param("gpu_on", "bool", True, "-G", style="switch"),
        ),
    ),
    Engine(
        eid="rtx-super-resolution",
        name="RTX Super Resolution",
        algos=("RTX VSR",),
        category="sr",
        exe_names=("UpscalePipelineApp.exe", "VideoEffectsApp.exe", "AigsEffectApp.exe"),
        homepage="https://developer.nvidia.com/maxine-getting-started",
        cli=False,
        downloadable=False,
        download_reason_key="engine.reason.driver",
        params=(Param("scale", "choice", 2, "", choices=((2, "2x"), (3, "3x"), (4, "4x"))),),
    ),
    # ---------------- 插帧 ----------------
    Engine(
        eid="rife-ncnn-vulkan",
        name="rife-ncnn-vulkan",
        algos=("RIFE",),
        category="interp",
        exe_names=("rife-ncnn-vulkan.exe", "rife-ncnn-vulkan"),
        homepage="https://github.com/nihui/rife-ncnn-vulkan/releases",
        downloadable=True,
        download_sources=(("gh", "nihui/rife-ncnn-vulkan|windows.zip"),),
        model_flag="-m",
        model_style="dir",
        params=(
            Param(
                "model",
                "choice",
                "rife-v4.6",
                "-m",
                choices=(
                    ("rife-v4.6", "RIFE v4.6"),
                    ("rife-v4", "RIFE v4"),
                    ("rife-v3.1", "RIFE v3.1"),
                    ("rife-v2.3", "RIFE v2.3"),
                    ("rife-anime", "RIFE Anime"),
                    ("rife-HD", "RIFE HD"),
                ),
            ),
            _p_multiplier(),
            Param("uhd", "bool", False, "-u", style="switch"),
            _p_gpu(),
            _p_jobs(),
            _p_tta(),
            Param("tta_temporal", "bool", False, "-z", style="switch"),
        ),
    ),
    Engine(
        eid="cain-ncnn-vulkan",
        name="cain-ncnn-vulkan",
        algos=("CAIN",),
        category="interp",
        exe_names=("cain-ncnn-vulkan.exe", "cain-ncnn-vulkan"),
        homepage="https://github.com/nihui/cain-ncnn-vulkan/releases",
        downloadable=True,
        download_sources=(("gh", "nihui/cain-ncnn-vulkan|windows.zip"),),
        model_flag="-m",
        model_style="dir",
        params=(
            Param("model", "choice", "cain", "-m", choices=(("cain", "CAIN"),)),
            _p_multiplier(),
            _p_gpu(),
            _p_jobs(),
        ),
    ),
    Engine(
        eid="dain-ncnn-vulkan",
        name="dain-ncnn-vulkan",
        algos=("DAIN",),
        category="interp",
        exe_names=("dain-ncnn-vulkan.exe", "dain-ncnn-vulkan"),
        homepage="https://github.com/nihui/dain-ncnn-vulkan/releases",
        downloadable=True,
        download_sources=(("gh", "nihui/dain-ncnn-vulkan|windows.zip"),),
        model_flag="-m",
        model_style="dir",
        params=(
            Param("model", "choice", "best", "-m", choices=(("best", "DAIN Best"),)),
            _p_multiplier(),
            _p_tile(),
            _p_gpu(),
            _p_jobs(),
        ),
    ),
    Engine(
        eid="ifrnet-ncnn-vulkan",
        name="IFRNet-ncnn-vulkan",
        algos=("IFRNet",),
        category="interp",
        exe_names=("ifrnet-ncnn-vulkan.exe", "ifrnet-ncnn-vulkan"),
        homepage="https://github.com/nihui/ifrnet-ncnn-vulkan/releases",
        downloadable=True,
        download_sources=(("gh", "nihui/ifrnet-ncnn-vulkan|windows.zip"),),
        model_flag="-m",
        model_style="dir",
        params=(
            Param(
                "model",
                "choice",
                "IFRNet_Vimeo90K",
                "-m",
                choices=(
                    ("IFRNet_Vimeo90K", "IFRNet Vimeo90K"),
                    ("IFRNet_GoPro", "IFRNet GoPro"),
                    ("IFRNet", "IFRNet"),
                ),
            ),
            _p_multiplier(),
            Param("uhd", "bool", False, "-u", style="switch"),
            _p_gpu(),
            _p_jobs(),
            _p_tta(),
            Param("tta_temporal", "bool", False, "-z", style="switch"),
        ),
    ),
)

ENGINE_BY_ID: dict[str, Engine] = {e.eid: e for e in ENGINES}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
ANIM_EXTS = {".gif"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".wmv"}


# ==========================================================================
# 定位与检测
# ==========================================================================
def engine_dir(eid: str, create: bool = True) -> Path:
    """``tools/<eid>/`` —— 该引擎的专属存放文件夹。"""
    directory = tools_dir() / eid
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def ensure_all_dirs() -> list[Path]:
    """为全部引擎建好 ``tools/<eid>/`` 空目录（首次启动时调用）。"""
    made = []
    for e in ENGINES:
        try:
            made.append(engine_dir(e.eid, create=True))
        except OSError as exc:
            log.warning("cannot create tools dir for %s: %s", e.eid, exc)
    return made


def _iter_search_roots(eng: Engine):
    """引擎目录 + 兼容目录，各自展开 2 层子目录（release zip 常多套一层）。"""
    roots = [engine_dir(eng.eid, create=False)]
    roots += [tools_dir() / d for d in eng.legacy_dirs]
    for root in roots:
        if not root.is_dir():
            continue
        yield root
        try:
            for lv1 in root.iterdir():
                if not lv1.is_dir():
                    continue
                yield lv1
                for lv2 in lv1.iterdir():
                    if lv2.is_dir():
                        yield lv2
        except OSError:
            continue


def find_engine(eid: str) -> str | None:
    """返回引擎可执行文件的绝对路径；找不到返回 ``None``。

    顺序：``tools/<eid>``（含 2 层子目录）→ 兼容目录 → 系统 ``PATH``。
    """
    eng = ENGINE_BY_ID.get(eid)
    if eng is None:
        return None
    for folder in _iter_search_roots(eng):
        for exe in eng.exe_names:
            cand = folder / exe
            if cand.is_file():
                return str(cand)
    for exe in eng.exe_names:
        found = shutil.which(exe) or shutil.which(Path(exe).stem)
        if found:
            return found
    return None


def engine_root(eid: str) -> Path | None:
    """可执行文件所在目录（模型子目录通常与它同级）。"""
    exe = find_engine(eid)
    return Path(exe).parent if exe else None


def resolve_model_path(eng: Engine, value: str) -> str:
    """把模型子目录名解析成绝对路径（找不到就原样返回，交给引擎自己判断）。"""
    root = engine_root(eng.eid)
    if root:
        cand = root / value
        if cand.is_dir():
            return str(cand)
        # 有些分发把模型放在 models/ 下
        cand2 = root / "models" / value
        if cand2.is_dir():
            return str(cand2)
    return value


def is_installed(eid: str) -> bool:
    return find_engine(eid) is not None


def installed_engines(category: str = "") -> list[Engine]:
    """已安装（可执行文件存在）的引擎；``category`` 为空表示全部。"""
    return [e for e in ENGINES if (not category or e.category == category) and is_installed(e.eid)]


def detect_all() -> dict[str, dict]:
    """扫描全部引擎，返回 ``{eid: {installed, exe, dir}}``（供关于页展示）。"""
    result: dict[str, dict] = {}
    for e in ENGINES:
        exe = find_engine(e.eid)
        result[e.eid] = {
            "installed": bool(exe),
            "exe": exe or "",
            "dir": str(engine_dir(e.eid, create=False)),
        }
    return result


def default_values(eid: str) -> dict:
    """引擎参数的默认值字典。"""
    eng = ENGINE_BY_ID.get(eid)
    return {p.key: p.default for p in eng.params} if eng else {}


def effective_scale(eid: str, values: dict) -> float:
    """当前设置下的实际放大倍率（用于日志 / 结果展示）。"""
    eng = ENGINE_BY_ID.get(eid)
    if not eng:
        return 1.0
    for key in ("scale", "zoom"):
        if key in values:
            try:
                return float(values[key])
            except (TypeError, ValueError):
                pass  # 静默原因：参数非数值时回退默认缩放 1.0
    return 1.0


# ==========================================================================
# 命令行构造
# ==========================================================================
def build_command(eid: str, src: str, dst: str, values: dict) -> tuple[list, str]:
    """按 schema 拼出完整命令行。返回 ``(cmd, error)``。"""
    eng = ENGINE_BY_ID.get(eid)
    if eng is None:
        return [], f"未知引擎: {eid}"
    if not eng.cli:
        return [], "该引擎没有命令行接口，无法在本软件内直接调用"
    exe = find_engine(eid)
    if not exe:
        return [], f"未找到 {eng.name}，请在 tools/{eng.eid} 中放入引擎文件"

    cmd = [exe, "-i", src, "-o", dst]
    for p in eng.params:
        if not p.flag:
            continue
        val = values.get(p.key, p.default)
        if p.kind == "bool":
            if p.style == "switch":
                if val:
                    cmd.append(p.flag)
            else:
                cmd += [p.flag, "1" if val else "0"]
            continue
        if p.key == "model" and eng.model_style == "dir":
            cmd += [p.flag, resolve_model_path(eng, str(val))]
            continue
        if p.key == "gpu":
            if val in ("auto", "", None):
                continue
            cmd += [p.flag, "-1" if val == "cpu" else str(val)]
            continue
        if val in ("", None):
            continue
        if p.kind == "float":
            cmd += [p.flag, f"{float(val):g}"]
        else:
            cmd += [p.flag, str(val)]

    # 模型名风格（Real-ESRGAN）额外补一个模型目录
    if eng.model_style == "name" and eng.models_root:
        root = engine_root(eng.eid)
        if root and (root / eng.models_root).is_dir():
            cmd += ["-m", str(root / eng.models_root)]

    return cmd, ""


# 进度解析：引擎常在 stdout/stderr 打印百分比或 "当前/总数"
_PROG_PCT = re.compile(r"(\d{1,3})\s*%")
_PROG_FRAC = re.compile(r"(\d+)\s*/\s*(\d+)")


def _run(
    cmd: list, timeout: int = 3600, progress_cb: Callable[[int], None] | None = None
) -> tuple[bool, str]:
    """执行引擎子进程。

    v0.7.7 修复3：当传入 ``progress_cb`` 时改为流式读取输出并解析进度，
    让队列进度条能跟随任务推进；不传则保持原 blocking 行为（兼容 upscaler）。
    """
    log.info("engine cmd: %s", " ".join(str(c) for c in cmd))
    if progress_cb is not None:
        return _run_stream(cmd, timeout, progress_cb)
    try:
        proc = run_silent(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"处理超时（超过 {timeout} 秒）"
    except OSError as exc:
        return False, f"启动失败: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        err = err[-3:] if err else ["未知错误"]
        return False, " · ".join(err)[:400]
    return True, ""


def _run_stream(cmd: list, timeout: int, progress_cb: Callable[[int], None]) -> tuple[bool, str]:
    """流式版 _run：边跑边解析进度，进度条不再卡在 0。"""
    try:
        proc = popen_silent(
            [str(c) for c in cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        return False, f"启动失败: {exc}"
    buf: list[str] = []
    last = -1
    start = time.monotonic()
    try:
        for line in proc.stdout:  # 逐行读取，自动处理 EOF
            if time.monotonic() - start > timeout:
                proc.kill()
                proc.wait()
                return False, f"处理超时（超过 {timeout} 秒）"
            buf.append(line)
            pct = _parse_progress(line)
            if pct is not None and 0 <= pct <= 99 and pct != last:
                last = pct
                progress_cb(pct)
    except Exception as exc:  # 读取异常视为失败
        proc.kill()
        proc.wait()
        return False, f"启动失败: {exc}"
    rc = proc.wait()
    out = "".join(buf)
    if rc != 0:
        err = out.strip().splitlines()
        err = err[-3:] if err else ["未知错误"]
        return False, " · ".join(err)[:400]
    return True, ""


def _parse_progress(line: str) -> int | None:
    m = _PROG_PCT.search(line)
    if m:
        return max(0, min(99, int(m.group(1))))
    fr = _PROG_FRAC.search(line)
    if fr:
        cur, tot = int(fr.group(1)), int(fr.group(2))
        if tot:
            return max(0, min(99, int(cur / tot * 100)))
    return None


# ==========================================================================
# 执行管线
# ==========================================================================
def run_image(
    eid: str, src: str, dst: str, values: dict, progress_cb: Callable[[int], None] | None = None
) -> tuple[bool, str]:
    """单张图片（或整个图片目录）走一次引擎调用。"""
    cmd, err = build_command(eid, src, dst, values)
    if err:
        return False, err
    # 输出格式（ncnn 系列支持 -f）
    ext = Path(dst).suffix.lower().lstrip(".")
    eng = ENGINE_BY_ID[eid]
    if ext in ("jpg", "jpeg", "png", "webp") and eng.eid.endswith("ncnn-vulkan"):
        cmd += ["-f", "jpg" if ext == "jpeg" else ext]
    if progress_cb is not None:
        progress_cb(3)  # 起步进度，避免进度条一直停在 0
    return _run(cmd, progress_cb=progress_cb)


def _probe_fps(ffmpeg: str, src: str) -> float:
    ffprobe = shutil.which("ffprobe") or str(Path(ffmpeg).parent / "ffprobe.exe")
    if not ffprobe or not Path(ffprobe).is_file():
        return 25.0
    try:
        proc = run_silent(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=r_frame_rate",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                src,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        val = proc.stdout.strip()
        if "/" in val:
            a, b = val.split("/")
            return float(a) / float(b) if float(b) else 25.0
        if val:
            return float(val)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # 静默原因：读取引擎参数失败回退默认 25.0
    return 25.0


def _recombine(ffmpeg: str, frames_out: Path, src: str, dst: str, fps: float) -> tuple[bool, str]:
    out_ext = Path(dst).suffix.lower().lstrip(".")
    if out_ext == "gif":
        return _run(
            [
                ffmpeg,
                "-y",
                "-framerate",
                f"{fps:g}",
                "-i",
                str(frames_out / "%06d.png"),
                "-vf",
                "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                dst,
            ],
            timeout=900,
        )
    return _run(
        [
            ffmpeg,
            "-y",
            "-framerate",
            f"{fps:g}",
            "-i",
            str(frames_out / "%06d.png"),
            "-i",
            src,
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            dst,
        ],
        timeout=1800,
    )


def run_frames(
    eid: str, src: str, dst: str, values: dict, progress_cb: Callable[[int], None] | None = None
) -> tuple[bool, str]:
    """GIF / 视频：抽帧 → 引擎整目录处理 → 重新合成。

    超分引擎保持原帧率；插帧引擎按倍率提高目标帧数与输出帧率。
    """
    from .ffmpeg import find_ffmpeg

    eng = ENGINE_BY_ID.get(eid)
    if eng is None:
        return False, f"未知引擎: {eid}"
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, "需要 ffmpeg 来处理视频 / GIF（请先安装或下载 ffmpeg）"
    if not find_engine(eid):
        return False, f"未找到 {eng.name}，请在 tools/{eng.eid} 中放入引擎文件"

    tmp = tempfile.mkdtemp(prefix="ms_eng_")
    frames_in = Path(tmp) / "in"
    frames_out = Path(tmp) / "out"
    frames_in.mkdir(parents=True, exist_ok=True)
    frames_out.mkdir(parents=True, exist_ok=True)
    try:
        if progress_cb is not None:
            progress_cb(2)
        ok, msg = _run([ffmpeg, "-y", "-i", src, str(frames_in / "%06d.png")], timeout=1800)
        if not ok:
            return False, f"抽帧失败: {msg}"
        in_frames = sorted(frames_in.glob("*.png"))
        if not in_frames:
            return False, "未从源文件抽取到任何帧"

        fps = _probe_fps(ffmpeg, src) or 25.0
        cmd, err = build_command(eid, str(frames_in), str(frames_out), values)
        if err:
            return False, err

        if eng.is_interp:
            try:
                mult = int(values.get("multiplier", 2))
            except (TypeError, ValueError):
                mult = 2
            mult = max(2, mult)
            target = max(2, (len(in_frames) - 1) * mult + 1)
            cmd += [eng.interp_count_flag, str(target)]
            fps = fps * mult

        if progress_cb is not None:
            progress_cb(10)  # 抽帧完成，进入引擎处理阶段
        ok, msg = _run(cmd, timeout=7200, progress_cb=progress_cb)
        if not ok:
            return False, ("插帧失败: " if eng.is_interp else "放大失败: ") + msg

        out_frames = sorted(frames_out.glob("*.png"))
        if not out_frames:
            return False, "处理后未生成帧文件"
        for i, f in enumerate(out_frames, 1):
            target_path = frames_out / f"{i:06d}.png"
            if target_path != f:
                os.replace(f, target_path)

        if progress_cb is not None:
            progress_cb(92)  # 帧处理完成，进入合成阶段
        ok, msg = _recombine(ffmpeg, frames_out, src, dst, fps)
        if not ok:
            return False, f"合成失败: {msg}"
        return True, ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def process_media(
    eid: str, src: str, dst: str, values: dict, progress_cb: Callable[[int], None] | None = None
) -> tuple[bool, str]:
    """统一入口：按输入类型分派到图片或帧管线。

    v0.7.7 修复3：支持 ``progress_cb`` 流式进度，让队列进度条跟随推进。
    """
    eng = ENGINE_BY_ID.get(eid)
    if eng is None:
        return False, f"未知引擎: {eid}"
    ext = Path(src).suffix.lower()
    if ext in IMAGE_EXTS:
        if eng.is_interp:
            return False, "插帧引擎只能处理视频 / GIF，不能处理静态图片"
        return run_image(eid, src, dst, values, progress_cb=progress_cb)
    if ext in ANIM_EXTS or ext in VIDEO_EXTS:
        return run_frames(eid, src, dst, values, progress_cb=progress_cb)
    return False, f"不支持的输入格式: {ext}"
