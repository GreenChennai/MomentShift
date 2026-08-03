"""图片 / 视频放大引擎封装（MomentShift）。

职责边界：
- 做：调用开源的 realesrgan-ncnn-vulkan 二进制（基于 Vulkan，跨平台，可用 GPU
  或 CPU）做放大；GIF / 视频走「抽帧 → 整批放大 → 重新合成」管线。
- 不做：不打包引擎二进制与模型（按需一键下载到 tools/realesrgan/）；不负责 UI。

依赖：core/platform、core/logger、core/engines；被依赖：gui/upscale_interface。

设计说明：
- 引擎二进制与 ncnn 模型不随安装包装（会严重膨胀），统一放在 tools/realesrgan/
  目录，按需求一键下载。
- Real-ESRGAN 的 Windows 包已自带四个标准 ncnn 模型，一次下载即得引擎与默认模型，
  正好满足「模型按需下载」的诉求。
- 静态图片由二进制直接放大；GIF / 视频走帧管线：ffmpeg 抽帧 → 二进制整批放大 →
  ffmpeg 重新合成（保留音轨）。
- 纯标准库网络请求 + Qt worker 封装，无额外 pip 依赖。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .config import tools_dir
from .logger import get_logger
from .platform import run_silent
from .qt_compat import QObject, QRunnable, Signal

log = get_logger("upscaler")


# --- 路径定位 ---
def realesrgan_dir() -> Path:
    """返回放大引擎的统一目录（``tools/realesrgan``），不存在时自动创建。"""
    directory = tools_dir() / "realesrgan"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def engine_exe() -> Path:
    """返回 realesrgan-ncnn-vulkan 可执行文件的绝对路径。"""
    return realesrgan_dir() / "realesrgan-ncnn-vulkan.exe"


def models_dir() -> Path:
    """返回存放 ncnn ``.bin`` / ``.param`` 模型文件的目录，不存在时自动创建。"""
    directory = realesrgan_dir() / "models"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def find_upscaler() -> str | None:
    """定位 realesrgan-ncnn-vulkan 二进制。

    优先用受管理的 ``tools/realesrgan`` 目录，其次退回系统 ``PATH``。
    找不到返回 ``None``。
    """
    p = engine_exe()
    if p.is_file():
        return str(p)
    return shutil.which("realesrgan-ncnn-vulkan") or shutil.which("realesrgan-ncnn-vulkan.exe")


# --- 模型 ---
# 引擎 zip 内自带的四个模型。``scale`` 是模型的原生输出倍率；二进制也接受最终
# ``-s`` 为 2/3/4（推理后再缩放），但默认用原生倍率以获得最佳画质。
MODELS: dict[str, dict] = {
    "realesrgan-x4plus": {
        "label": "Real-ESRGAN x4+",
        "scale": 4,
        "kind": "photo",
        "note": "通用照片 / 写实图像 (4x)",
    },
    "realesrgan-x4plus-anime": {
        "label": "Real-ESRGAN x4+ Anime",
        "scale": 4,
        "kind": "anime",
        "note": "动漫插画 (4x)",
    },
    "realesrnet-x4plus": {
        "label": "Real-ESRNet x4+",
        "scale": 4,
        "kind": "photo",
        "note": "去模糊 / 通用 (4x)",
    },
    "realesr-animevideov3": {
        "label": "AnimeVideo v3",
        "scale": 4,
        "kind": "video",
        "note": "动漫视频 (4x)",
    },
}

# 静态图 / 动图由二进制直接处理；其余格式一律走 ffmpeg 帧管线。
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
ANIM_EXTS = {".gif"}


def model_present(name: str) -> bool:
    """判断某模型的 ``.bin`` / ``.param`` 两个文件是否都已落盘。"""
    return (models_dir() / f"{name}.bin").is_file() and (models_dir() / f"{name}.param").is_file()


def available_models() -> list[str]:
    """返回既在 :data:`MODELS` 中定义、又已下载到本地的模型 id 列表。"""
    return [mid for mid in MODELS if model_present(mid)]


# --- 公开 API ---
def _run(cmd: list[str], timeout: int = 3600) -> tuple[bool, str]:
    log.info("upscaler cmd: %s", " ".join(cmd))
    try:
        proc = run_silent(
            cmd,
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


def upscale_image(
    input_path: str,
    output_path: str,
    model: str,
    scale: int = 4,
    tile: int = 0,
    gpu: str = "auto",
) -> tuple[bool, str]:
    """放大单张图片，或一次性放大整个图片目录。

    Args:
        input_path: 源图片路径，或存放待放大图片的目录。
        output_path: 输出图片路径；当 ``input_path`` 是目录时同样传目录。
        model: 模型 id，必须是 :data:`MODELS` 的键且已下载到本地。
        scale: 放大倍率，取值 2/3/4。
        tile: 分块大小，0 表示由引擎自动决定；显存不足时调小可避免 OOM。
        gpu: ``auto`` 自动选卡，``cpu`` 强制 CPU，其余按字符串当作 GPU 序号。
    Returns:
        ``(是否成功, 失败原因或空串)``
    """
    exe = find_upscaler()
    if not exe:
        return False, "未找到 realesrgan-ncnn-vulkan 引擎，请先下载"
    if not model_present(model):
        return False, f"模型缺失: {model}（请下载引擎以获取标准模型）"

    cmd = [
        exe,
        "-i",
        input_path,
        "-o",
        output_path,
        "-n",
        model,
        "-s",
        str(scale),
        "-m",
        str(models_dir()),
    ]
    if tile:
        cmd += ["-t", str(tile)]
    if gpu == "cpu":
        cmd += ["-g", "-1"]
    elif gpu not in ("auto", ""):
        cmd += ["-g", str(gpu)]

    ext = Path(output_path).suffix.lower().lstrip(".")
    if ext in ("jpg", "jpeg", "png", "webp"):
        cmd += ["-f", "jpg" if ext == "jpeg" else ext]

    return _run(cmd)


def _probe_fps(ffmpeg: str, input_path: str) -> float:
    """尽力用 ffprobe 探测源文件帧率，任何失败都回退 25.0。

    Notes:
        ffprobe 返回的是 ``30000/1001`` 这类分数形式，需要自行做除法换算。
    """
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
                input_path,
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


def upscale_frames(
    input_path: str,
    output_path: str,
    model: str,
    scale: int = 4,
    tile: int = 0,
    gpu: str = "auto",
) -> tuple[bool, str]:
    """走 ffmpeg 帧管线放大动图 GIF 或视频。

    流程：抽帧 → 用一次二进制调用整批放大 → 重新合成（有音轨则一并保留）。

    Args:
        input_path: 源 GIF / 视频路径。
        output_path: 输出路径，扩展名决定合成方式（``.gif`` 走调色板管线）。
        model: 模型 id，须已下载到本地。
        scale: 放大倍率，取值 2/3/4。
        tile: 分块大小，0 表示自动。
        gpu: ``auto`` / ``cpu`` / GPU 序号。
    Returns:
        ``(是否成功, 失败原因或空串)``
    Notes:
        整批放大而不是逐帧调用，是因为每次启动引擎都要重新加载模型与初始化
        Vulkan，逐帧调用的开销会淹没实际推理时间。
    """
    from .ffmpeg import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, "需要 ffmpeg 来处理视频 / GIF（请先安装或下载 ffmpeg）"
    exe = find_upscaler()
    if not exe:
        return False, "未找到 realesrgan-ncnn-vulkan 引擎，请先下载"
    if not model_present(model):
        return False, f"模型缺失: {model}（请下载引擎以获取标准模型）"

    out_ext = Path(output_path).suffix.lower().lstrip(".")
    tmp = tempfile.mkdtemp(prefix="ms_up_")
    frames_in = Path(tmp) / "in"
    frames_out = Path(tmp) / "out"
    frames_in.mkdir(parents=True, exist_ok=True)
    frames_out.mkdir(parents=True, exist_ok=True)
    try:
        # 1) 抽帧
        ok, msg = _run(
            [ffmpeg, "-y", "-i", input_path, str(frames_in / "%06d.png")],
            timeout=600,
        )
        if not ok:
            return False, f"抽帧失败: {msg}"
        in_frames = sorted(frames_in.glob("*.png"))
        if not in_frames:
            return False, "未从源文件抽取到任何帧"

        # 2) 一次调用整批放大整个目录
        ok, msg = upscale_image(str(frames_in), str(frames_out), model, scale, tile, gpu)
        if not ok:
            return False, f"放大失败: {msg}"

        # 3) 重排帧文件名：引擎输出的序号可能不连续，而 ffmpeg 的 %06d 输入
        #    要求严格连续，否则会在第一个缺口处提前结束合成
        out_frames = sorted(frames_out.glob("*.png"))
        if not out_frames:
            return False, "放大后未生成帧文件"
        for i, f in enumerate(out_frames, 1):
            dst = frames_out / f"{i:06d}.png"
            if dst != f:
                os.replace(f, dst)

        fps = _probe_fps(ffmpeg, input_path) or 25.0

        # 4) 重新合成
        if out_ext == "gif":
            ok, msg = _run(
                [
                    ffmpeg,
                    "-y",
                    "-framerate",
                    f"{fps:g}",
                    "-i",
                    str(frames_out / "%06d.png"),
                    "-vf",
                    "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                    output_path,
                ],
                timeout=600,
            )
        else:
            cmd = [
                ffmpeg,
                "-y",
                "-framerate",
                f"{fps:g}",
                "-i",
                str(frames_out / "%06d.png"),
                "-i",
                input_path,
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
                output_path,
            ]
            ok, msg = _run(cmd, timeout=900)
        if not ok:
            return False, f"合成失败: {msg}"
        return True, ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def upscale_media(
    input_path: str,
    output_path: str,
    model: str,
    scale: int = 4,
    tile: int = 0,
    gpu: str = "auto",
) -> tuple[bool, str]:
    """放大任意受支持的输入（静态图 / GIF / 视频），按扩展名自动选管线。

    Returns:
        ``(是否成功, 失败原因或空串)``；扩展名不受支持时返回 ``(False, 原因)``。
    """
    ext = Path(input_path).suffix.lower()
    if ext in IMAGE_EXTS:
        return upscale_image(input_path, output_path, model, scale, tile, gpu)
    if ext in ANIM_EXTS or ext in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}:
        return upscale_frames(input_path, output_path, model, scale, tile, gpu)
    return False, f"不支持的输入格式: {ext}"


# --- 引擎下载（二进制 + 标准 ncnn 模型打包在同一个 zip 内）---
ENGINE_REPO = "xinntao/Real-ESRGAN"
ENGINE_ASSET = "realesrgan-ncnn-vulkan-20220424-windows.zip"
ENGINE_FALLBACK = (
    "https://github.com/xinntao/Real-ESRGAN/releases/download/"
    "v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip"
)
ENGINE_PAGE = "https://github.com/xinntao/Real-ESRGAN/releases"


class DownloadSignals(QObject):
    """引擎下载 worker 向 GUI 线程回传状态的信号载体。

    线程约定：``run()`` 在 worker 线程执行，信号跨线程投递到 GUI 线程。
    信号：
    - ``started()`` —— 下载开始时发出。
    - ``finished(bool, str)`` —— 下载结束时发出，参数为 ``(是否成功, 提示语)``。
    """

    started = Signal()
    finished = Signal(bool, str)


def _github_latest_asset_url(repo: str, asset_substr: str) -> str | None:
    """查询 GitHub 最新 release 中名字含 ``asset_substr`` 的附件下载地址。

    Args:
        repo: ``owner/name`` 形式的仓库标识。
        asset_substr: 附件名需要包含的子串（大小写不敏感）。
    Returns:
        命中的下载地址；网络失败、限流或无匹配时返回 ``None``，由调用方回退到
        固定地址。
    """
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "MomentShift"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for a in data.get("assets", []):
            name = (a.get("name") or "").lower()
            if asset_substr.lower() in name:
                return a.get("browser_download_url")
    except (OSError, ValueError, KeyError) as exc:
        # 网络不通 / GitHub 限流 / 返回体不是预期 JSON，都只降级到固定回退地址，
        # 不该让「解析最新版本」的失败阻断整个下载流程
        log.warning("解析 %s 的最新 release 失败，回退固定地址：%s", repo, exc)
    return None


def download_upscaler(dest_dir: str) -> tuple[bool, str]:
    """下载 realesrgan-ncnn-vulkan 引擎与自带模型到 ``dest_dir``。

    Args:
        dest_dir: 解压目标目录，不存在时自动创建。
    Returns:
        ``(是否成功, 提示语或失败原因)``
    Notes:
        zip 就地解压，使 ``dest_dir/realesrgan-ncnn-vulkan.exe`` 与
        ``dest_dir/models/*.bin`` 正好落在 :func:`find_upscaler` 与
        :func:`model_present` 期望的位置，无需再搬运文件。
    """
    os.makedirs(dest_dir, exist_ok=True)
    url = _github_latest_asset_url(ENGINE_REPO, ENGINE_ASSET) or ENGINE_FALLBACK
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MomentShift"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(dest_dir)
        log.info("引擎已解压到 %s", dest_dir)
        return True, "引擎与模型已下载"
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        # 下载中断、磁盘写失败、zip 损坏都归一为「失败 + 原因」交给界面提示
        log.warning("下载放大引擎失败：%s", exc)
        return False, str(exc)


class UpscalerDownloadWorker(QRunnable):
    """在 UI 线程之外执行 :func:`download_upscaler` 的下载 worker。

    典型用法::

        worker = UpscalerDownloadWorker(str(realesrgan_dir()))
        worker.signals.finished.connect(on_done)
        QThreadPool.globalInstance().start(worker)

    线程约定：``run()`` 在线程池的 worker 线程执行，结果只经 :attr:`signals`
    回传，禁止在其中直接碰 Qt 控件。
    """

    def __init__(self, dest_dir: str):
        super().__init__()
        self.setAutoDelete(True)
        self.dest_dir = dest_dir
        self.signals = DownloadSignals()

    def run(self) -> None:
        self.signals.started.emit()
        ok, msg = download_upscaler(self.dest_dir)
        self.signals.finished.emit(ok, msg)
